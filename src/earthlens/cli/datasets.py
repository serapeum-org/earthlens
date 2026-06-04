"""The `earthlens datasets …` command group.

Federated queries over every backend's bundled catalog. This module owns
the sub-application object and the commands attached to it:

* `where` — which provider(s) expose a given dataset (the headline use case).
"""

from __future__ import annotations

import difflib

import typer

from earthlens.cli.adapter import known_provider_keys
from earthlens.cli.query import (
    apply_filters,
    exact_first,
    facet_counts,
    free_text,
    match_rows,
    parse_filters,
    sort_rows,
)
from earthlens.cli.render import (
    COMPACT_COLUMNS,
    FULL_COLUMNS,
    counts_table,
    err_console,
    out_console,
    print_load_warnings,
    record_json,
    record_table,
    rows_table,
    rows_to_ids,
    rows_to_json,
)
from earthlens.cli.table import FACET_NAMES, build_table

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


@datasets_app.command()
def search(
    query: str = typer.Argument(
        "", help="Free-text query over provider / id / title (blank = all)."
    ),
    provider: list[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="Restrict to these providers (repeatable / comma-separated).",
    ),
    filter_: list[str] = typer.Option(
        None,
        "--filter",
        "-f",
        help="Narrow by facet (repeatable AND), e.g. --filter cadence=daily.",
    ),
    limit: int = typer.Option(
        0, "--limit", "-n", help="Cap the number of rows shown (0 = no cap)."
    ),
    count: bool = typer.Option(
        False, "--count", help="Print only the number of matches."
    ),
    facets_only: bool = typer.Option(
        False,
        "--facets-only",
        help="Print per-facet value counts instead of rows.",
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit a JSON array (for piping)."
    ),
    ids_only: bool = typer.Option(
        False, "--ids-only", help="Emit bare `provider<TAB>id` lines (for piping)."
    ),
) -> None:
    """Free-text + faceted search across every provider's catalog.

    Combines a free-text `query` (over provider / id / title) with repeatable
    `--filter facet=value` narrowing (logical AND over `provider`, `cadence`,
    `resolution`, `license`). `--count` prints just the total; `--facets-only`
    prints the distribution of matches per facet value (cheap to probe a big
    federation); `--limit` caps the rows shown.
    """
    providers = _resolve_providers(provider)
    try:
        filters = parse_filters(filter_ or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    catalog = build_table(providers=providers)
    rows = sort_rows(apply_filters(free_text(catalog.rows, query), filters))
    print_load_warnings(catalog.errors)

    if count:
        typer.echo(str(len(rows)))
        return
    if facets_only:
        counts = {facet: facet_counts(rows, facet) for facet in FACET_NAMES}
        out_console().print(counts_table({f: c for f, c in counts.items() if c}))
        return
    if limit and limit > 0:
        rows = rows[:limit]
    _emit_rows(rows, json_output=json_output, ids_only=ids_only)


@datasets_app.command("list")
def list_datasets(
    provider: list[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="Restrict to these providers (repeatable / comma-separated).",
    ),
    full: bool = typer.Option(
        False,
        "--full",
        "-l",
        help="Show title / cadence / resolution columns, not just ids.",
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit a JSON array (for piping)."
    ),
    ids_only: bool = typer.Option(
        False, "--ids-only", help="Emit bare `provider<TAB>id` lines (for piping)."
    ),
) -> None:
    """List datasets across providers (offline; reads the bundled catalogs).

    Names-only by default (`provider` / `dataset id`); `--full` adds the
    title, cadence and resolution columns. Scope to one or more backends
    with `--provider`. No network access is performed.
    """
    providers = _resolve_providers(provider)
    catalog = build_table(providers=providers)
    rows = sort_rows(catalog.rows)
    print_load_warnings(catalog.errors)

    if json_output:
        typer.echo(rows_to_json(rows))
        return
    if ids_only:
        typer.echo(rows_to_ids(rows))
        return
    out_console().print(
        rows_table(rows, columns=FULL_COLUMNS if full else COMPACT_COLUMNS)
    )
    err_console().print(
        f"[dim]{len(rows)} dataset(s) across "
        f"{len(catalog.providers)} provider(s).[/dim]"
    )


@datasets_app.command()
def show(
    provider: str = typer.Argument(..., help="Provider id (or alias)."),
    dataset: str = typer.Argument(..., help="Dataset id within that provider."),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the full record as JSON."
    ),
) -> None:
    """Show the full catalog record for one dataset.

    Dumps every field of the backend's pydantic record (title, extent,
    cadence, resolution, variables / bands, license, …) as a table or, with
    `--json`, as a JSON object. Suggests near matches when the dataset id is
    not found within the provider.
    """
    selectors = _resolve_providers([provider])
    catalog = build_table(providers=selectors)
    print_load_warnings(catalog.errors)

    wanted = dataset.strip().lower()
    match = next(
        (row for row in catalog.rows if row.dataset_id.lower() == wanted), None
    )
    if match is None:
        ids = [row.dataset_id for row in catalog.rows]
        close = difflib.get_close_matches(dataset, ids, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        err_console().print(
            f"[red]{provider!r} has no dataset {dataset!r}.[/red]{hint}"
        )
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(record_json(match))
        return
    out_console().print(record_table(match))
