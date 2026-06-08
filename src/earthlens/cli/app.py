"""Top-level Typer application that wires the earthlens CLI together.

Defines the root :data:`app` and mounts the `datasets` and `providers`
command groups. The console script declared in `pyproject.toml`
(`earthlens = "earthlens.cli:app"`) resolves to this module's
:data:`app`; :func:`main` is a thin wrapper for `python -m earthlens.cli`.
"""

from __future__ import annotations

import typer

from earthlens.cli.datasets import datasets_app
from earthlens.cli.providers import providers_app

#: The root Typer application — the `earthlens` console script.
app = typer.Typer(
    name="earthlens",
    help=(
        "Query earthlens' federated data-source catalogs from the shell. "
        "Use `earthlens datasets where <name>` to find which provider(s) "
        "expose a dataset."
    ),
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(datasets_app, name="datasets")
app.add_typer(providers_app, name="providers")


def main() -> None:
    """Invoke the root application (entry point for `python -m earthlens.cli`)."""
    app()


if __name__ == "__main__":
    main()
