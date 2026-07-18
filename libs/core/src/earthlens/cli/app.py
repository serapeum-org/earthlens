"""Top-level Typer application that wires the earthlens CLI together.

Defines the root `app` and mounts the `datasets` and `providers`
command groups. The console script declared in `pyproject.toml`
(`earthlens = "earthlens.cli:main"`) resolves to `main`, which
runs `app` and rewrites a missing provider distribution into a
friendly install hint (see `_provider_backend_hint`).
"""

from __future__ import annotations

import typer

from earthlens.cli.datasets import datasets_app
from earthlens.cli.providers import providers_app

#: Top-level modules that `earthlens-core` itself ships under the
#: `earthlens` namespace. A missing `earthlens.<name>` where `<name>` is
#: one of these is a genuine core-install fault (re-raised untouched); a
#: miss on any other name is an uninstalled provider distribution — the
#: catalog-tooling subcommands (`datasets curate/refresh/validate/…`)
#: defer-import provider modules that live in the theme distributions,
#: which `earthlens-core` does not depend on.
_CORE_MODULES = frozenset(
    {
        "_backends",
        "aggregate",
        "base",
        "biodiversity",
        "cli",
        "core",
        "earthlens",
        "grids",
        "spatial",
    }
)

#: The root Typer application — mounted by the `earthlens` console script.
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


def _provider_backend_hint(exc: ModuleNotFoundError) -> str | None:
    """Return an install hint if `exc` is a missing provider distribution, else `None`.

    The catalog-tooling subcommands defer-import provider modules
    (`earthlens.ecmwf`, `earthlens.gee`, `earthlens.nwm`, …) that ship in
    the theme distributions rather than in `earthlens-core`. On a
    core-only install those imports fail at the top-level provider
    package, so Python reports the missing module as exactly
    `earthlens.<backend>`; this rewrites that miss into a message naming
    the package to install.

    Only a **top-level** `earthlens.<backend>` miss is rewritten. A deeper
    miss (`earthlens.<backend>.<submodule>`) means the distribution is
    installed but one of its own imports failed — a provider bug, not a
    missing install — so this returns `None` and the caller re-raises the
    real traceback. The hint recommends `pip install earthlens` (the meta
    package that bundles every provider) rather than a per-backend extra,
    because the SDK-free backends have no matching extra to name.

    Args:
        exc: The `ModuleNotFoundError` raised while running a command.

    Returns:
        A one-line install hint when the missing module is exactly a
        top-level provider package under the `earthlens` namespace, or
        `None` for a core module, a deeper (provider-internal) miss, or an
        unrelated third-party import — all of which the caller re-raises
        unchanged.
    """
    name = exc.name or ""
    parts = name.split(".")
    hint = None
    if len(parts) == 2 and parts[0] == "earthlens" and parts[1] not in _CORE_MODULES:
        backend = parts[1]
        hint = (
            f"This command needs the {backend!r} provider backend, which is not "
            f"installed. It ships in a provider distribution — install the full "
            f"package with `pip install earthlens` (it bundles every provider)."
        )
    return hint


def main() -> None:
    """Run the root application, rewriting a missing backend into an install hint.

    This is the `earthlens` console-script entry point. It invokes
    `app` and, when a catalog-tooling subcommand defer-imports a
    provider module that is not installed, prints a friendly
    `pip install earthlens[<backend>]` hint and exits non-zero instead of
    letting the raw `ModuleNotFoundError` traceback surface. Any other
    missing import (a core module or a backend SDK) propagates unchanged.
    """
    try:
        app()
    except ModuleNotFoundError as exc:
        hint = _provider_backend_hint(exc)
        if hint is None:
            raise
        typer.secho(hint, fg=typer.colors.RED, err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":  # pragma: no cover - module-run shim
    main()
