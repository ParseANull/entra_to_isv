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
    """Compare a single account between Entra ID and Verify and show diffs."""
    _configure_logging(verbose)

    try:
        config = load_config(str(config_path), str(env_file) if env_file else None)
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}")
        raise typer.Exit(code=2) from exc

    res = compare_account(config, upn)
    if output:
        if fmt.lower() == "json":
            output.write_text(json.dumps(res, indent=2), encoding="utf-8")
            typer.echo(f"Wrote comparison to {output}")
        elif fmt.lower() == "csv":
            # Flatten diffs to rows: field,current,new
            diffs = res.get("diffs", {}) if isinstance(res, dict) else {}
            with output.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["username", "field", "current", "new"])
                for k, v in diffs.items():
                    w.writerow([res.get("username"), k, v.get("current"), v.get("new")])
            typer.echo(f"Wrote comparison CSV to {output}")
        else:
            typer.echo("Unsupported format. Use json or csv.")
            raise typer.Exit(code=2)
    else:
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
    _configure_logging(verbose)

    try:
        config = load_config(str(config_path), str(env_file) if env_file else None)
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}")
        raise typer.Exit(code=2) from exc

    while True:
        typer.echo("\nChoose an action:")
        typer.echo("  1) Synchronize Entra ID → Verify")
        typer.echo("  2) Compare an account (by UPN)")
        typer.echo("  3) What-if report (no changes)")
        typer.echo("  4) Exit")
        choice = input("> ").strip()

        if choice == "1":
            try:
                sync_users(config, dry_run=False)
                typer.echo("Sync complete.")
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"Error during sync: {exc}")

        elif choice == "2":
            upn = input("Enter userPrincipalName (UPN): ").strip()
            if not upn:
                typer.echo("No UPN provided.")
                continue
            try:
                result = compare_account(config, upn)
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"Error during compare: {exc}")
                continue

            status = result.get("status")
            typer.echo(f"Status: {status}")
            if status == "not_found_in_azure":
                typer.echo("User not found in Azure AD.")
            elif status == "not_found_in_verify":
                typer.echo("User not found in Verify. Would be created with mapped payload:")
                typer.echo(str(result.get("scim_mapped")))
            elif status == "found_both":
                diffs = result.get("diffs", {})
                if diffs:
                    typer.echo("Differences detected (current → new):")
                    for k, v in diffs.items():
                        typer.echo(f"  {k}: {v.get('current')} → {v.get('new')}")
                else:
                    typer.echo("No differences detected.")

        elif choice == "3":
            try:
                to_create, to_update, total = plan_sync(config)
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"Error during planning: {exc}")
                continue

            typer.echo(f"Azure users fetched: {total}")
            typer.echo(f"Would create: {len(to_create)}")
            for _, mapped in to_create[:20]:
                typer.echo(f"  + {mapped.get('userName')}")
            if len(to_create) > 20:
                typer.echo(f"  ... and {len(to_create) - 20} more")

            typer.echo(f"Would evaluate updates: {len(to_update)}")
            updated = 0
            for mapped, existing in to_update[:50]:
                if _needs_update(mapped, existing):
                    updated += 1
            typer.echo(f"Would update (estimated): ~{updated} (of first {min(len(to_update),50)})")

        elif choice == "4" or choice.lower() in {"q", "quit", "exit"}:
            typer.echo("Goodbye!")
            break
        else:
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
    """Plan the synchronization and output steps without making changes."""
    _configure_logging(verbose)

    try:
        config = load_config(str(config_path), str(env_file) if env_file else None)
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}")
        raise typer.Exit(code=2) from exc

    to_create, to_update, total = plan_sync(config)

    # Build exportable structures
    create_payloads = [mapped for _, mapped in to_create]
    update_diffs = []
    for mapped, existing in to_update:
        diffs = _compute_diffs(mapped, existing)
        if diffs:
            update_diffs.append({
                "userName": mapped.get("userName"),
                "diffs": diffs,
            })

    summary = {
        "total": total,
        "to_create": len(create_payloads),
        "to_update": len(to_update),
        "updates_with_changes": len(update_diffs),
    }

    if output:
        if fmt.lower() == "json":
            doc = {"summary": summary, "create": create_payloads, "update": update_diffs}
            output.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            typer.echo(f"Wrote what-if plan to {output}")
        elif fmt.lower() == "csv":
            with output.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["action", "userName", "field", "current", "new"])
                for m in create_payloads:
                    w.writerow(["create", m.get("userName"), "", "", ""])
                for upd in update_diffs:
                    uname = upd.get("userName")
                    for field, change in upd.get("diffs", {}).items():
                        w.writerow(["update", uname, field, change.get("current"), change.get("new")])
            typer.echo(f"Wrote what-if CSV to {output}")
        else:
            typer.echo("Unsupported format. Use json or csv.")
            raise typer.Exit(code=2)
    else:
        # Console summary
        typer.echo(f"Azure users fetched: {summary['total']}")
        typer.echo(f"Would create: {summary['to_create']}")
        for m in create_payloads[:20]:
            typer.echo(f"  + {m.get('userName')}")
        if summary['to_create'] > 20:
            typer.echo(f"  ... and {summary['to_create'] - 20} more")
        typer.echo(f"Would update (with changes): {summary['updates_with_changes']}")


if __name__ == "__main__":
    app()
