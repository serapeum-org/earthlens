"""The `earthlens datasets …` command group.

Federated queries over every backend's bundled catalog. This module owns
the sub-application object and the commands attached to it:

* `where` — which provider(s) expose a given dataset (the headline use case).
"""

from __future__ import annotations

import typer

from earthlens.cli.adapter import known_provider_keys
from earthlens.cli.query import exact_first, match_rows
from earthlens.cli.render import (
    err_console,
    out_console,
    print_load_warnings,
    rows_table,
    rows_to_ids,
    rows_to_json,
)
from earthlens.cli.table import build_table

#: Typer sub-application mounted at `earthlens datasets`.
datasets_app = typer.Typer(
    no_args_is_help=True,
    help="Find and inspect datasets across all earthlens providers.",
)


@datasets_app.callback()
def datasets() -> None:
    """Find and inspect datasets across all earthlens providers."""


def _resolve_providers(values: list[str] | None) -> list[str] | None:
    """Split / validate a repeated, comma-tolerant `--provider` option.

    Args:
        values: Raw `--provider` values (each may itself be comma-separated).

    Returns:
        The flattened list of provider selectors, or `None` when nothing
        was given (meaning "scan every provider").

    Raises:
        typer.BadParameter: If any token is not a known provider id/alias.
    """
    if not values:
        return None
    tokens = [
        tok.strip() for value in values for tok in value.split(",") if tok.strip()
    ]
    if not tokens:
        return None
    unknown = sorted(t for t in tokens if t not in known_provider_keys())
    if unknown:
        raise typer.BadParameter(f"unknown provider(s): {', '.join(unknown)}")
    return tokens


def _emit_rows(rows, *, json_output: bool, ids_only: bool) -> None:
    """Print matched rows as JSON, ids, or a Rich table (shared by commands)."""
    if json_output and ids_only:
        raise typer.BadParameter("use only one of --json / --ids-only")
    if json_output:
        typer.echo(rows_to_json(rows))
        return
    if ids_only:
        typer.echo(rows_to_ids(rows))
        return
    if rows:
        out_console().print(rows_table(rows))
    providers = {row.provider for row in rows}
    err_console().print(
        f"[dim]{len(rows)} match(es) across {len(providers)} provider(s).[/dim]"
    )


@datasets_app.command()
def where(
    name: str = typer.Argument(
        ..., help="Dataset id or fragment to locate across providers."
    ),
    provider: list[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="Restrict to these providers (repeatable / comma-separated).",
    ),
    exact: bool = typer.Option(
        False, "--exact", help="Match the dataset id exactly (no substring search)."
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit a JSON array (for piping)."
    ),
    ids_only: bool = typer.Option(
        False, "--ids-only", help="Emit bare `provider<TAB>id` lines (for piping)."
    ),
) -> None:
    """Show which provider(s) expose a dataset.

    Scans every backend's catalog (or just `--provider` ones) for `name`:
    an exact dataset-id match wins, otherwise a case-insensitive substring
    match against the id and title. Results are ordered by the configurable
    provider precedence (see `EARTHLENS_PROVIDER_PRIORITY`). Exits non-zero
    when nothing matches, so it composes in shell pipelines.
    """
    providers = _resolve_providers(provider)
    catalog = build_table(providers=providers)
    matches = exact_first(match_rows(catalog.rows, name, exact=exact), name)

    print_load_warnings(catalog.errors)
    _emit_rows(matches, json_output=json_output, ids_only=ids_only)
    if not matches:
        if not (json_output or ids_only):
            err_console().print(f"[red]No dataset matches {name!r}.[/red]")
        raise typer.Exit(code=1)
