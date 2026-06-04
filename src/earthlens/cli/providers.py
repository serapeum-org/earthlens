"""The `earthlens providers …` command group.

Inspect the backend registry itself (which providers exist, whether their
optional SDK is importable). The `list` command is added by a later task;
this module owns the sub-application object it attaches to.
"""

from __future__ import annotations

import typer

#: Typer sub-application mounted at `earthlens providers`.
providers_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect the earthlens provider backend registry.",
)


@providers_app.callback()
def providers() -> None:
    """Inspect the earthlens provider backend registry."""
