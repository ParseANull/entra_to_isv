from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from .config import ConfigError, load_config
from .sync_service import sync_users

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


if __name__ == "__main__":
    app()
