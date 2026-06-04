"""The `earthlens datasets …` command group.

Federated queries over every backend's bundled catalog. Commands are
added by later tasks (`where`, `search`, `list`, `show`, `facets`); this
module owns the sub-application object they attach to.
"""

from __future__ import annotations

import typer

#: Typer sub-application mounted at `earthlens datasets`.
datasets_app = typer.Typer(
    no_args_is_help=True,
    help="Find and inspect datasets across all earthlens providers.",
)


@datasets_app.callback()
def datasets() -> None:
    """Find and inspect datasets across all earthlens providers."""
