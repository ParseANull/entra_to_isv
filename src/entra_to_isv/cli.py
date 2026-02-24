from __future__ import annotations

import logging
from pathlib import Path
import json
import csv
from typing import Optional

import typer

from .config import ConfigError, load_config
from .sync_service import compare_account, plan_sync, sync_users, _needs_update, _compute_diffs

app = typer.Typer(add_completion=False, help="Sync Azure AD (Entra ID) users to IBM Security Verify")


def _configure_logging(verbose: bool) -> None:
    """Set up logging with appropriate level and format.
    
    We configure Python's logging system to output structured log lines with
    timestamps, levels, and logger names. Verbose mode gives you DEBUG level
    (all the gory details), otherwise INFO level (just the highlights).
    
    Args:
        verbose: If True, enable DEBUG logging. If False, stick with INFO.
            Evaluated to determine log level.
    """
    # level: int - Initialized based on verbose flag, either DEBUG or INFO.
    level = logging.DEBUG if verbose else logging.INFO
    # Configure root logger with level and format string.
    # Side effect: all subsequent logging uses this config.
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")


@app.command()
def sync(
    config_path: Path = typer.Option(
        Path("config/mapping.example.yaml"),
        "--config",
        "-c",
        help="Path to YAML config with mapping and sync options",
    ),
    env_file: Optional[Path] = typer.Option(
        Path(".env"),
        "--env-file",
        help="Path to .env file containing secrets (Graph, Verify)",
    ),
    dry_run: bool = typer.Option(False, help="Print planned actions without calling APIs"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
):
    """Run a single sync cycle from Azure AD to IBM Security Verify.
    
    This is the main CLI entrypoint that orchestrates the entire sync operation.
    We load config from files and environment variables, then hand off to the
    sync_users function to do the actual work.
    
    The command supports dry-run mode (see what would happen without doing it) and
    verbose logging (see all the gory details). Perfect for testing or debugging
    when things go sideways.
    
    Args:
        config_path: Path to YAML file with attribute mappings and sync options.
            Defaults to config/mapping.example.yaml (initialized as Path object).
        env_file: Path to .env file with secrets (tenant IDs, tokens, etc.).
            Defaults to .env in current directory. If you're in a container with
            env vars already set, you can skip this. (initialized as Optional[Path])
        dry_run: If True, we plan everything but don't actually create/update users.
            Great for testing or seeing what would change. (initialized as bool)
        verbose: If True, enable DEBUG level logging for maximum visibility.
            Use this when something's not working and you need to see everything.
            (initialized as bool)
    
    Exits:
        0: Success - sync completed without errors.
        2: Config error - missing required settings or bad file paths.
        1: Other runtime error (API failures, etc.) - handled by Typer.
    """
    # Configure logging first so we see what's happening.
    # verbose evaluated to determine log level.
    _configure_logging(verbose)

    # Load and validate configuration from YAML and environment.
    try:
        # config: AppConfig - Initialized by loading from files/env vars.
        # config_path and env_file evaluated and converted to strings.
        config = load_config(str(config_path), str(env_file) if env_file else None)
    except ConfigError as exc:
        # exc: ConfigError - Caught when required config is missing.
        # We print a friendly error message instead of a scary stack trace.
        typer.echo(f"Config error: {exc}")
        # Exit with code 2 (config error) - disposee exc after logging.
        raise typer.Exit(code=2) from exc

    # config now fully validated and ready to use.
    # dry_run evaluated to pass to sync function.
    # Side effect: sync_users executes the full sync operation or dry-run.
    sync_users(config, dry_run=dry_run)
    # If we get here, sync completed successfully. config disposed on exit.


@app.command()
def compare(
    upn: str = typer.Argument(..., help="Azure AD userPrincipalName to compare"),
    config_path: Path = typer.Option(
        Path("config/mapping.example.yaml"),
        "--config",
        "-c",
        help="Path to YAML config with mapping and sync options",
    ),
    env_file: Optional[Path] = typer.Option(
        Path(".env"),
        "--env-file",
        help="Path to .env file containing secrets (Graph, Verify)",
    ),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write result to a file"),
    fmt: str = typer.Option("json", "--format", "-f", help="Output format: json or csv"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
):
    """Compare a single account between Entra ID and Verify and show diffs.

    We look up one user by their UPN in both Azure AD and Verify, then map them
    through our attribute rules and surface any differences. This is our magnifying
    glass for diagnosing why a particular account might be out of sync.

    The result can be printed to the console (JSON by default) or written to a file
    in either JSON or CSV format - handy when you need to hand it off to someone
    who lives in spreadsheets.

    Args:
        upn: The Azure AD userPrincipalName to look up (e.g., alice@example.com).
        config_path: Path to YAML config with attribute mappings.
        env_file: Path to .env file with secrets.
        output: Optional file path to write the result. Prints to console if omitted.
        fmt: Output format - 'json' (default) or 'csv'.
        verbose: Enable debug logging for full diagnostic output.

    Exits:
        0: Comparison completed and output produced.
        2: Config error or unsupported output format.
    """
    # Configure logging first so we can see what's happening during the compare.
    # verbose evaluated to select the appropriate log level.
    _configure_logging(verbose)

    # Load config the same way the sync command does - credentials and mappings.
    try:
        # config: AppConfig - Initialized from YAML and env vars.
        config = load_config(str(config_path), str(env_file) if env_file else None)
    except ConfigError as exc:
        # exc: ConfigError - Caught when required settings are absent.
        typer.echo(f"Config error: {exc}")
        # Exit code 2 signals a config problem, not a runtime one.
        raise typer.Exit(code=2) from exc

    # res: Dict - Initialized by fetching and comparing the user across both systems.
    # compare_account does all the heavy lifting: Graph lookup, mapping, Verify lookup,
    # and diff calculation. We just need to render its result.
    res = compare_account(config, upn)

    # Decide how to present the result - file vs console, JSON vs CSV.
    if output:
        if fmt.lower() == "json":
            # Serialize the full comparison result as pretty-printed JSON.
            # We use indent=2 because humans read this, not machines.
            output.write_text(json.dumps(res, indent=2), encoding="utf-8")
            typer.echo(f"Wrote comparison to {output}")
        elif fmt.lower() == "csv":
            # Flatten diffs to rows: field, current, new.
            # We extract just the diffs dict so each row represents one changed field.
            # diffs: Dict - Initialized from result, empty dict if not present.
            diffs = res.get("diffs", {}) if isinstance(res, dict) else {}
            # f: file handle - Opened for writing CSV rows, auto-closed on exit.
            with output.open("w", newline="", encoding="utf-8") as f:
                # w: csv.writer - Initialized to write delimited rows to f.
                w = csv.writer(f)
                # Write the header row so readers know what each column means.
                w.writerow(["username", "field", "current", "new"])
                # Loop through each differing field and emit one row per diff.
                for k, v in diffs.items():
                    # k: str - field name evaluated on each iteration.
                    # v: Dict - change dict with 'current' and 'new' keys.
                    w.writerow([res.get("username"), k, v.get("current"), v.get("new")])
                # f disposed (closed) automatically when we leave the with block.
            typer.echo(f"Wrote comparison CSV to {output}")
        else:
            # fmt evaluated and found to be unsupported - tell the user nicely.
            typer.echo("Unsupported format. Use json or csv.")
            raise typer.Exit(code=2)
    else:
        # No output file specified - just pretty-print JSON to the console.
        # This is the quick-look mode: run it, read it, done.
        typer.echo(json.dumps(res, indent=2))


@app.command()
def repl(
    config_path: Path = typer.Option(
        Path("config/mapping.example.yaml"),
        "--config",
        "-c",
        help="Path to YAML config with mapping and sync options",
    ),
    env_file: Optional[Path] = typer.Option(
        Path(".env"),
        "--env-file",
        help="Path to .env file containing secrets (Graph, Verify)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
):
    """Interactive REPL for sync operations (sync, compare, what-if).

    We'll load configuration once and then present a simple menu so you can:
    - Synchronize Entra ID → Verify
    - Compare a single account between Entra ID and Verify
    - Run a what-if (dry-run) sync report
    """
    # Set up logging so we can see what's happening during interactive use.
    _configure_logging(verbose)

    # Load config once upfront so every menu action shares the same settings.
    # We don't want to re-read files on every iteration - that would be slow and wasteful.
    try:
        # config: AppConfig - Initialized once and reused for every REPL operation.
        config = load_config(str(config_path), str(env_file) if env_file else None)
    except ConfigError as exc:
        # exc: ConfigError - Caught when required settings are absent.
        typer.echo(f"Config error: {exc}")
        raise typer.Exit(code=2) from exc

    # Main event loop - we keep going until the user explicitly asks to quit.
    # This gives them a chance to run multiple operations without restarting.
    while True:
        # Print the menu on every iteration so users don't have to remember options.
        typer.echo("\nChoose an action:")
        typer.echo("  1) Synchronize Entra ID → Verify")
        typer.echo("  2) Compare an account (by UPN)")
        typer.echo("  3) What-if report (no changes)")
        typer.echo("  4) Exit")
        # choice: str - Initialized from user input, stripped of whitespace.
        choice = input("> ").strip()

        if choice == "1":
            # User wants to run a full sync - fire it up with real changes enabled.
            try:
                # Side effect: sync_users creates/updates users in Verify.
                sync_users(config, dry_run=False)
                typer.echo("Sync complete.")
            except Exception as exc:  # noqa: BLE001
                # exc: Exception - Caught to keep the REPL alive after errors.
                # We don't want a failed sync to kill the whole interactive session.
                typer.echo(f"Error during sync: {exc}")

        elif choice == "2":
            # User wants to inspect a single account - ask them which one.
            # upn: str - Initialized from user input, stripped of whitespace.
            upn = input("Enter userPrincipalName (UPN): ").strip()
            if not upn:
                # upn evaluated as falsy - nothing to look up, so skip.
                typer.echo("No UPN provided.")
                continue
            try:
                # result: Dict - Initialized by comparing the account across both systems.
                result = compare_account(config, upn)
            except Exception as exc:  # noqa: BLE001
                # exc: Exception - Caught to keep the REPL alive after errors.
                typer.echo(f"Error during compare: {exc}")
                continue

            # status: str - Extracted from result to determine what happened.
            status = result.get("status")
            typer.echo(f"Status: {status}")
            # Render the result in a human-friendly way based on what we found.
            if status == "not_found_in_azure":
                # User doesn't exist in Azure - nothing to sync.
                typer.echo("User not found in Azure AD.")
            elif status == "not_found_in_verify":
                # User exists in Azure but not yet in Verify - would be created.
                typer.echo("User not found in Verify. Would be created with mapped payload:")
                typer.echo(str(result.get("scim_mapped")))
            elif status == "found_both":
                # User exists in both - check if anything changed.
                # diffs: Dict - Initialized from result, contains changed fields.
                diffs = result.get("diffs", {})
                if diffs:
                    typer.echo("Differences detected (current → new):")
                    # Loop through each differing field and print the before/after.
                    for k, v in diffs.items():
                        # k: field name, v: Dict with 'current' and 'new' values.
                        typer.echo(f"  {k}: {v.get('current')} → {v.get('new')}")
                else:
                    # No diffs - user is already in sync. Nothing to do here.
                    typer.echo("No differences detected.")

        elif choice == "3":
            # User wants a what-if report - plan the sync without executing it.
            try:
                # to_create, to_update: Lists - Initialized with users needing action.
                # total: int - Initialized with total Azure user count.
                to_create, to_update, total = plan_sync(config)
            except Exception as exc:  # noqa: BLE001
                # exc: Exception - Caught to keep the REPL alive after planning errors.
                typer.echo(f"Error during planning: {exc}")
                continue

            # Print a summary of what the sync would do.
            typer.echo(f"Azure users fetched: {total}")
            typer.echo(f"Would create: {len(to_create)}")
            # Show up to 20 users to be created - enough for a quick glance.
            for _, mapped in to_create[:20]:
                # mapped: SCIM dict evaluated on each iteration.
                typer.echo(f"  + {mapped.get('userName')}")
            if len(to_create) > 20:
                # Too many to list - show a count of what we're hiding.
                typer.echo(f"  ... and {len(to_create) - 20} more")

            typer.echo(f"Would evaluate updates: {len(to_update)}")
            # updated: int - Initialized to zero, counts users with actual changes.
            updated = 0
            # Sample the first 50 update candidates to estimate how many need changes.
            # We cap at 50 because checking all users against Verify can be slow.
            for mapped, existing in to_update[:50]:
                # mapped, existing: Dicts evaluated on each iteration.
                if _needs_update(mapped, existing):
                    updated += 1  # updated incremented when a change is detected.
            typer.echo(f"Would update (estimated): ~{updated} (of first {min(len(to_update),50)})")

        elif choice == "4" or choice.lower() in {"q", "quit", "exit"}:
            # User wants out - say goodbye and break the loop.
            # choice evaluated against multiple exit signals for convenience.
            typer.echo("Goodbye!")
            break  # Loop exits here, REPL ends.
        else:
            # choice didn't match any known option - tell the user what's valid.
            typer.echo("Invalid selection. Please choose 1-4.")


@app.command()
def whatif(
    config_path: Path = typer.Option(
        Path("config/mapping.example.yaml"),
        "--config",
        "-c",
        help="Path to YAML config with mapping and sync options",
    ),
    env_file: Optional[Path] = typer.Option(
        Path(".env"),
        "--env-file",
        help="Path to .env file containing secrets (Graph, Verify)",
    ),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write plan to a file"),
    fmt: str = typer.Option("json", "--format", "-f", help="Output format: json or csv"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
):
    """Plan the synchronization and output steps without making changes.

    We do all the work of a real sync - fetching users from Azure, mapping them,
    checking what exists in Verify - but we stop just before applying changes.
    The result is a complete picture of what *would* happen, which is super useful
    before running a sync against a production environment for the first time.
    """
    # Set up logging so we can see the planning steps unfold.
    _configure_logging(verbose)

    # Load config exactly as the real sync would - same credentials, same mappings.
    try:
        # config: AppConfig - Initialized from YAML and env vars.
        config = load_config(str(config_path), str(env_file) if env_file else None)
    except ConfigError as exc:
        # exc: ConfigError - Caught when required settings are absent.
        typer.echo(f"Config error: {exc}")
        raise typer.Exit(code=2) from exc

    # Run the planning phase - this does real API calls but makes zero changes.
    # to_create, to_update: Lists - Initialized with users needing action.
    # total: int - Initialized with total Azure user count for context.
    to_create, to_update, total = plan_sync(config)

    # Build exportable data structures from the plan results.
    # create_payloads: List[Dict] - Initialized with SCIM dicts for users to create.
    # We extract just the mapped (SCIM) side since that's what would be POSTed.
    create_payloads = [mapped for _, mapped in to_create]

    # update_diffs: List[Dict] - Initialized empty, will hold per-user diff reports.
    # We only include users that actually have changes - no point listing no-ops.
    update_diffs = []
    for mapped, existing in to_update:
        # mapped: SCIM dict from Azure, existing: current SCIM dict from Verify.
        # diffs: Dict - Initialized by comparing mapped vs existing field by field.
        diffs = _compute_diffs(mapped, existing)
        if diffs:
            # Append a summary entry for this user with userName and the diffs.
            update_diffs.append({
                "userName": mapped.get("userName"),
                "diffs": diffs,
            })
            # update_diffs updated with new entry.

    # summary: Dict - Initialized with high-level counts for the plan header.
    # This gives anyone reading the report an instant overview before diving into details.
    summary = {
        "total": total,
        "to_create": len(create_payloads),
        "to_update": len(to_update),
        "updates_with_changes": len(update_diffs),
    }

    if output:
        if fmt.lower() == "json":
            # doc: Dict - Initialized as the full exportable plan document.
            doc = {"summary": summary, "create": create_payloads, "update": update_diffs}
            # Write the complete plan as pretty-printed JSON so it's human readable.
            output.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            typer.echo(f"Wrote what-if plan to {output}")
        elif fmt.lower() == "csv":
            # f: file handle - Opened for writing, auto-closed on exit.
            with output.open("w", newline="", encoding="utf-8") as f:
                # w: csv.writer - Initialized to write rows to f.
                w = csv.writer(f)
                # Header row so readers know the column layout.
                w.writerow(["action", "userName", "field", "current", "new"])
                # Write one row per user to create - field/current/new are blank for creates.
                for m in create_payloads:
                    # m: Dict - SCIM payload evaluated on each iteration.
                    w.writerow(["create", m.get("userName"), "", "", ""])
                # Write one row per changed field per user to update.
                for upd in update_diffs:
                    # upd: Dict - Per-user diff summary evaluated on each iteration.
                    # uname: str - Initialized with the user's identifier for this row.
                    uname = upd.get("userName")
                    for field, change in upd.get("diffs", {}).items():
                        # field: str, change: Dict with 'current' and 'new'.
                        w.writerow(["update", uname, field, change.get("current"), change.get("new")])
                # f disposed (closed) automatically when we leave the with block.
            typer.echo(f"Wrote what-if CSV to {output}")
        else:
            # fmt evaluated and found to be unsupported - let the user know.
            typer.echo("Unsupported format. Use json or csv.")
            raise typer.Exit(code=2)
    else:
        # No output file - print a concise console summary instead.
        typer.echo(f"Azure users fetched: {summary['total']}")
        typer.echo(f"Would create: {summary['to_create']}")
        # Show up to 20 new users so the console doesn't scroll forever.
        for m in create_payloads[:20]:
            # m: Dict - SCIM payload evaluated on each iteration.
            typer.echo(f"  + {m.get('userName')}")
        if summary['to_create'] > 20:
            # summary['to_create'] evaluated - tell user how many we're hiding.
            typer.echo(f"  ... and {summary['to_create'] - 20} more")
        typer.echo(f"Would update (with changes): {summary['updates_with_changes']}")


if __name__ == "__main__":
    app()
