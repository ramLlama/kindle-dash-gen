"""Command-line interface for the Kindle dashboard generator."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import Config, load_config

app = typer.Typer(
    help="Generate a Kindle e-ink dashboard with NYC weather and subway info.",
    no_args_is_help=True,
)

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to the TOML config file."),
]


@app.callback()
def main(ctx: typer.Context, config: ConfigOption = Path("config.toml")) -> None:
    """Store the config path for subcommands to load on demand."""
    ctx.obj = config


def _load(ctx: typer.Context) -> Config:
    """Load the config for the path recorded on the context."""
    return load_config(ctx.obj)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


def run() -> None:
    """Console-script entry point."""
    app()
