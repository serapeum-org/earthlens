"""The `earthlens datasets …` command group.

Federated queries over every backend's bundled catalog. This module owns
the sub-application object and the commands attached to it:

* `where` — which provider(s) expose a given dataset (the headline use case).
"""

from __future__ import annotations

import difflib
import json

import typer

from earthlens._cli_tooling import dispatch_table
from earthlens.cli.adapter import BackendInfo, known_provider_keys, list_backends
from earthlens.cli.curate import probe_dataset
from earthlens.cli.query import (
    apply_filters,
    exact_first,
    facet_counts,
    free_text,
    match_rows,
    parse_filters,
    sort_rows,
)
from earthlens.cli.refresh import (
    _TILE_REGENS,
    audit_one,
    coverage_one,
    refresh_one,
)
from earthlens.cli.render import (
    COMPACT_COLUMNS,
    FULL_COLUMNS,
    audit_table,
    counts_table,
    coverage_table,
    err_console,
    kv_table,
    out_console,
    print_load_warnings,
    probe_table,
    record_json,
    record_table,
    refresh_table,
    rows_table,
    rows_to_ids,
    rows_to_json,
    validate_table,
)
from earthlens.cli.stanza import emit_stanza, write_stanza
from earthlens.cli.table import FACET_NAMES, build_table
from earthlens.cli.validate import validate_one

#: Typer sub-application mounted at `earthlens datasets`.
datasets_app = typer.Typer(
    no_args_is_help=True,
    help="Find and inspect datasets across all earthlens providers.",
)

#: Help strings reused across several commands (single source of truth).
_PROVIDER_ARG_HELP = "Provider id (or alias)."
_PROVIDER_OPT_HELP = "Restrict to these providers (repeatable / comma-separated)."
_JSON_ARRAY_HELP = "Emit a JSON array (for piping)."
_IDS_ONLY_HELP = "Emit bare `provider<TAB>id` lines (for piping)."


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
        help=_PROVIDER_OPT_HELP,
    ),
    exact: bool = typer.Option(
        False, "--exact", help="Match the dataset id exactly (no substring search)."
    ),
    include_available: bool = typer.Option(
        False,
        "--include-available",
        help="Also search each backend's full upstream id index (slower).",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help=_JSON_ARRAY_HELP),
    ids_only: bool = typer.Option(False, "--ids-only", help=_IDS_ONLY_HELP),
) -> None:
    """Show which provider(s) expose a dataset.

    Scans every backend's catalog (or just `--provider` ones) for `name`:
    an exact dataset-id match wins, otherwise a case-insensitive substring
    match against the id and title. Results are ordered by the configurable
    provider precedence (see `EARTHLENS_PROVIDER_PRIORITY`). Exits non-zero
    (with a did-you-mean hint) when nothing matches, so it composes in shell
    pipelines.
    """
    providers = _resolve_providers(provider)
    catalog = build_table(providers=providers, include_available=include_available)
    matches = exact_first(match_rows(catalog.rows, name, exact=exact), name)

    print_load_warnings(catalog.errors)
    _emit_rows(matches, json_output=json_output, ids_only=ids_only)
    if not matches:
        if not (json_output or ids_only):
            # Suggest from curated ids only — the upstream available index
            # can be tens of thousands of ids, too many to fuzzy-rank here.
            curated_ids = [row.dataset_id for row in catalog.rows if row.curated]
            close = difflib.get_close_matches(name, curated_ids, n=3)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            err_console().print(f"[red]No dataset matches {name!r}.[/red]{hint}")
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
        help=_PROVIDER_OPT_HELP,
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
        False,
        "--count",
        help="Print only the match count (a bare number, or {\"count\": N} with --json).",
    ),
    facets_only: bool = typer.Option(
        False,
        "--facets-only",
        help="Print per-facet value counts instead of rows (honours --json).",
    ),
    include_available: bool = typer.Option(
        False,
        "--include-available",
        help="Also search each backend's full upstream id index (slower).",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help=_JSON_ARRAY_HELP),
    ids_only: bool = typer.Option(False, "--ids-only", help=_IDS_ONLY_HELP),
) -> None:
    """Free-text + faceted search across every provider's catalog.

    Combines a free-text `query` (over provider / id / title) with repeatable
    `--filter facet=value` narrowing (logical AND over `provider`, `cadence`,
    `resolution`, `license`). `--count` prints just the total; `--facets-only`
    prints the distribution of matches per facet value (cheap to probe a big
    federation); `--limit` caps the rows shown. Both `--count` and
    `--facets-only` honour `--json` (a `{"count": N}` object and a
    `{facet: [{value, count}]}` object respectively) for piping.
    """
    providers = _resolve_providers(provider)
    try:
        filters = parse_filters(filter_ or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if ids_only and (count or facets_only):
        raise typer.BadParameter(
            "--ids-only cannot be combined with --count / --facets-only"
        )

    catalog = build_table(providers=providers, include_available=include_available)
    rows = sort_rows(apply_filters(free_text(catalog.rows, query), filters))
    print_load_warnings(catalog.errors)

    if count:
        typer.echo(json.dumps({"count": len(rows)}) if json_output else str(len(rows)))
        return
    if facets_only:
        counts = {facet: facet_counts(rows, facet) for facet in FACET_NAMES}
        present = {facet: pairs for facet, pairs in counts.items() if pairs}
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        facet: [{"value": v, "count": c} for v, c in pairs]
                        for facet, pairs in present.items()
                    },
                    indent=2,
                )
            )
            return
        out_console().print(counts_table(present))
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
        help=_PROVIDER_OPT_HELP,
    ),
    full: bool = typer.Option(
        False,
        "--full",
        "-l",
        help="Show title / cadence / resolution columns, not just ids.",
    ),
    include_available: bool = typer.Option(
        False,
        "--include-available",
        help="Also include each backend's full upstream id index (slower).",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help=_JSON_ARRAY_HELP),
    ids_only: bool = typer.Option(False, "--ids-only", help=_IDS_ONLY_HELP),
) -> None:
    """List datasets across providers (offline; reads the bundled catalogs).

    Names-only by default (`provider` / `dataset id`); `--full` adds the
    title, cadence and resolution columns. Scope to one or more backends
    with `--provider`. No network access is performed.
    """
    providers = _resolve_providers(provider)
    catalog = build_table(providers=providers, include_available=include_available)
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
    provider: str = typer.Argument(..., help=_PROVIDER_ARG_HELP),
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


@datasets_app.command()
def facets(
    values: str = typer.Option(
        None,
        "--values",
        help="Enumerate the distinct values of this facet across the results.",
    ),
    provider: list[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help=_PROVIDER_OPT_HELP,
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit JSON (for piping)."
    ),
) -> None:
    """Discover what you can filter on, and which values exist.

    With no `--values`, lists each facet (`provider`, `cadence`,
    `resolution`, `license`) with how many distinct values it has. With
    `--values <facet>`, enumerates that facet's distinct values and how
    many datasets carry each — the vocabulary to feed `search --filter`.
    """
    if values is not None and values not in FACET_NAMES:
        choices = ", ".join(FACET_NAMES)
        raise typer.BadParameter(f"unknown facet {values!r}; choose from {choices}")

    providers = _resolve_providers(provider)
    catalog = build_table(providers=providers)
    print_load_warnings(catalog.errors)

    if values is not None:
        counts = facet_counts(catalog.rows, values)
        if json_output:
            typer.echo(
                json.dumps([{"value": v, "count": c} for v, c in counts], indent=2)
            )
            return
        out_console().print(kv_table("VALUE", "COUNT", counts, justify_b="right"))
        return

    summary = [(facet, len(catalog.facet_values(facet))) for facet in FACET_NAMES]
    if json_output:
        typer.echo(
            json.dumps(
                [{"facet": f, "distinct_values": n} for f, n in summary], indent=2
            )
        )
        return
    out_console().print(
        kv_table("FACET", "DISTINCT VALUES", summary, justify_b="right")
    )


def _select_refresh_backends(selector: str) -> list[BackendInfo]:
    """Resolve the `refresh` argument to a list of backends.

    Args:
        selector: `"all"`, or one / a comma-separated list of provider ids
            or aliases.

    Returns:
        The matching backends (every backend for `"all"`).

    Raises:
        typer.BadParameter: If the selector is empty or names an unknown
            provider.
    """
    backends = list_backends()
    if selector.strip().lower() == "all":
        return backends
    tokens = [tok.strip() for tok in selector.split(",") if tok.strip()]
    if not tokens:
        raise typer.BadParameter("name one or more providers, or 'all'")
    unknown = sorted(t for t in tokens if t not in known_provider_keys())
    if unknown:
        raise typer.BadParameter(f"unknown provider(s): {', '.join(unknown)}")
    wanted = set(tokens)
    return [
        info
        for info in backends
        if info.provider in wanted or wanted.intersection(info.aliases)
    ]


def _refresh_tiles(selected: list[BackendInfo], *, json_output: bool) -> None:
    """Regenerate the bundled GIS tile artefact for each `--tiles` provider."""
    results: list[dict[str, object]] = []
    for info in selected:
        regen = _TILE_REGENS.get(info.provider)
        if regen is None:
            results.append({"provider": info.provider, "status": "unsupported"})
            continue
        try:
            path, count = regen()
            results.append(
                {
                    "provider": info.provider,
                    "status": "ok",
                    "tiles": count,
                    "written": path,
                }
            )
        # A failed regen is surfaced in the result row, never re-raised.
        except Exception as exc:  # noqa: BLE001
            results.append(
                {"provider": info.provider, "status": "error", "detail": str(exc)}
            )
    if json_output:
        typer.echo(json.dumps(results, indent=2))
        return
    for result in results:
        if result["status"] == "ok":
            out_console().print(
                f"[green]wrote {result['tiles']} tiles[/green] "
                f"({result['provider']}) -> {result['written']}",
                soft_wrap=True,
            )
        else:
            err_console().print(
                f"[red]{result['status']}:[/red] {result['provider']} "
                f"{result.get('detail', 'no tile artefact for this provider')}"
            )


def _report_refresh(outcomes, *, show_ids: bool) -> None:
    """Print the refresh table plus the optional new-id and written lines.

    Args:
        outcomes: The per-provider refresh outcomes to render.
        show_ids: When true, also list each provider's new upstream ids.
    """
    out_console().print(refresh_table(outcomes))
    if show_ids:
        for outcome in outcomes:
            if outcome.status == "ok" and outcome.new_ids:
                out_console().print(
                    f"[bold]new upstream ids ({outcome.provider}):[/bold]"
                )
                for ident in outcome.new_ids:
                    out_console().print(f"  {ident}")
    for outcome in outcomes:
        if outcome.written:
            out_console().print(
                f"[green]wrote {outcome.live_count} ids[/green] "
                f"({outcome.provider}) -> {outcome.written}",
                soft_wrap=True,
            )


@datasets_app.command()
def refresh(
    providers: str = typer.Argument(
        ...,
        help="Provider(s) to refresh: a name, a comma-separated list, or 'all'.",
    ),
    show_ids: bool = typer.Option(
        False,
        "--show-ids",
        help="Also list the new upstream ids, not just the counts.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        "--update-catalog",
        help="Rewrite the bundled available_* index from the live fetch "
        "(modifies the package's catalog files; for editable installs).",
    ),
    tiles: bool = typer.Option(
        False,
        "--tiles",
        help="Regenerate a bundled GIS tile artefact instead of the index "
        "(GHSL only: rebuilds tile_schema.geojson from the JRC shapefile).",
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the outcomes as JSON (for piping)."
    ),
) -> None:
    """Fetch each provider's LIVE upstream index and diff it against the bundle.

    Unlike every other command, `refresh` goes to the **network**: it calls
    a provider's public API to list its current datasets / collections and
    reports what is new or gone versus the bundled `available_datasets`.
    Only providers with a public, no-auth listing endpoint are supported
    (others report `unsupported`), so `refresh all` degrades gracefully.

    With `--write` it also rewrites the bundled `available_*` index in place
    from the live fetch — the maintainer "update the shipped catalog" step,
    meaningful in an editable / source checkout. Providers whose index is
    computed from the curated rows at load time (no on-disk block) report
    "live read only" instead of writing. `--tiles` instead regenerates a
    bundled GIS tile artefact (GHSL's `tile_schema.geojson`).
    """
    selected = _select_refresh_backends(providers)
    if tiles:
        _refresh_tiles(selected, json_output=json_output)
        return
    if not json_output:
        action = "Updating" if write else "Querying"
        err_console().print(
            f"[dim]{action} live upstream indexes for "
            f"{len(selected)} provider(s)...[/dim]"
        )
    outcomes = [refresh_one(info, write=write) for info in selected]

    if json_output:
        typer.echo(json.dumps([o.to_dict() for o in outcomes], indent=2))
        return

    _report_refresh(outcomes, show_ids=show_ids)


@datasets_app.command()
def probe(
    provider: str = typer.Argument(..., help=_PROVIDER_ARG_HELP),
    dataset: str = typer.Argument(
        ..., help="Dataset / collection id to sample for its band-asset schema."
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Use the credentialed heavy sampler (real NetCDF/granule/CDS "
        "retrieval) where available — cmems / earthdata / ecmwf; needs creds.",
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the schema as JSON (for piping)."
    ),
) -> None:
    """Sample one dataset LIVE and print its band/asset schema (curation seed).

    Like `refresh`, this goes to the **network**: it fetches one sample record
    from the provider and records each asset's media type and band metadata
    (common name, dtype, nodata) — the seed a maintainer reviews before adding
    the dataset to the curated catalog. `--deep` swaps the light public probe
    for a credentialed sampler that reads the *real* on-disk schema (cmems
    opens the NetCDF, earthdata samples a granule, ecmwf retrieves via cdsapi);
    it needs the provider's credentials and can be slow.
    """
    backends = _select_refresh_backends(provider)
    if len(backends) != 1:
        raise typer.BadParameter("probe takes exactly one provider")
    result = probe_dataset(backends[0], dataset, deep=deep)

    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    elif result.status == "ok":
        out_console().print(probe_table(result))
    else:
        err_console().print(f"[red]{result.status}:[/red] {result.detail}")

    if result.status != "ok":
        raise typer.Exit(code=1)


@datasets_app.command()
# curate's parameters are its CLI options: the many provider-specific seed
# flags (earthdata / usgs_water / erddap / ecmwf) are the command's public
# surface, so the high count is inherent to the interface. Sonar S107 here is
# a false positive for a Typer command and is resolved as won't-fix in the UI.
def curate(
    provider: str = typer.Argument(..., help=_PROVIDER_ARG_HELP),
    upstream_id: str = typer.Argument(
        "",
        help="Upstream id to seed a curated row from (short name / id / code). "
        "Omit only with gee --fill-empty.",
    ),
    key: str = typer.Option(
        "", "--key", help="Friendly catalog key for the row (default: the id)."
    ),
    minimal: bool = typer.Option(
        False, "--minimal", help="Emit a placeholder row without a live fetch."
    ),
    hydrate: bool = typer.Option(
        False,
        "--hydrate",
        help="gee: read bands live from Earth Engine (needs GEE creds).",
    ),
    fill_empty: bool = typer.Option(
        False,
        "--fill-empty",
        help="gee/ecmwf: bulk-hydrate EVERY placeholder curated row in place "
        "from a live read (needs --write + creds; ignores upstream_id).",
    ),
    all_: bool = typer.Option(
        False,
        "--all",
        help="ecmwf: bulk-seed every uncurated dataset into the shards "
        "(needs --write; ignores upstream_id).",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        help="ecmwf --all / gee|ecmwf --fill-empty: only process the first N "
        "rows (0=all).",
    ),
    timeout: int = typer.Option(
        180,
        "--timeout",
        help="ecmwf --fill-empty: seconds to wait for each dataset's live "
        "retrieve before skipping it (0 = no deadline).",
    ),
    version: str = typer.Option("", "--version", help="earthdata: collection version."),
    cmr_provider: str = typer.Option(
        "", "--cmr-provider", help="earthdata: CMR provider code (e.g. GES_DISC)."
    ),
    daac: str = typer.Option("", "--daac", help="earthdata: DAAC label."),
    cloud_hosted: bool = typer.Option(
        False, "--cloud-hosted", help="earthdata: mark the collection cloud-hosted."
    ),
    name: str = typer.Option("", "--name", help="usgs_water: human-readable name."),
    units: str = typer.Option("", "--units", help="usgs_water: reporting units."),
    group: str = typer.Option(
        "", "--group", help="usgs_water group / eumetsat Data Store group."
    ),
    service: list[str] = typer.Option(
        None, "--service", help="usgs_water: repeatable service the code serves."
    ),
    server: str = typer.Option(
        "",
        "--server",
        help="erddap: ERDDAP base URL to look the dataset up on (defaults to "
        "the servers the catalog already curates from).",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Insert the seeded row into the catalog file (else print only).",
    ),
    target: str = typer.Option(
        "",
        "--target",
        help="Per-family file stem under catalog/ to write into "
        "(sharded providers; defaults to --daac / --group).",
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the seeded row as JSON (for piping)."
    ),
) -> None:
    """Author a curated `datasets:` row from one upstream id.

    The authoring companion to `probe`: it fetches one upstream id's
    metadata and seeds a `datasets:` YAML row (inferring `output_kind` /
    `format` / bands where it can). By default it prints the row for you to
    vet and paste; with `--write` it appends the row into the catalog file
    (single-file providers like usgs_water write in place; sharded providers
    need `--target <file-stem>`, defaulting to `--daac` / `--group`). Only
    providers with a stanza emitter are supported (earthdata, hdx,
    usgs_water, eumetsat, gee, jaxa, erddap); others report `unsupported`.
    """
    backends = _select_refresh_backends(provider)
    if len(backends) != 1:
        raise typer.BadParameter("curate takes exactly one provider")
    info = backends[0]

    if _run_bulk_curation(
        info,
        all_=all_,
        fill_empty=fill_empty,
        write=write,
        limit=limit,
        timeout=timeout,
    ):
        return
    if not upstream_id:
        raise typer.BadParameter(
            "curate needs an upstream_id (unless gee --fill-empty)"
        )

    result = emit_stanza(
        info,
        upstream_id,
        key=key or None,
        minimal=minimal,
        hydrate=hydrate,
        version=version,
        cmr_provider=cmr_provider,
        daac=daac,
        cloud_hosted=cloud_hosted,
        name=name,
        units=units,
        group=group,
        services=service or None,
        server=server or None,
    )
    _report_stanza(
        info,
        result,
        write=write,
        target=target or daac or group or None,
        json_output=json_output,
    )


def _run_bulk_curation(
    info,
    *,
    all_: bool,
    fill_empty: bool,
    write: bool,
    limit: int,
    timeout: int,
) -> bool:
    """Dispatch the `--all` / `--fill-empty` bulk passes; return whether handled.

    Args:
        info: The single selected backend.
        all_: Run the bulk-seed pass (`curate ecmwf --all`).
        fill_empty: Run the bulk-hydrate pass (`curate gee/ecmwf --fill-empty`).
        write: Whether the mutating `--write` flag was given.
        limit: Cap on rows processed (0 = all).
        timeout: Per-dataset retrieve deadline for `--fill-empty` (0 = none).

    Returns:
        True when a bulk pass ran (the caller should return), else False.

    Raises:
        typer.BadParameter: If both `--all` and `--fill-empty` are given.
    """
    if all_ and fill_empty:
        raise typer.BadParameter(
            "--all (bulk-seed) and --fill-empty (bulk-hydrate) are separate "
            "passes — run them one at a time, seed first"
        )
    if all_:
        _curate_all(info, write=write, limit=limit or None)
        return True
    if fill_empty:
        _curate_fill_empty(info, write=write, limit=limit or None, timeout=timeout)
        return True
    return False


def _report_stanza(
    info, result, *, write: bool, target: str | None, json_output: bool
) -> None:
    """Emit a seeded stanza's result: error-exit, write in place, or print it.

    Args:
        info: The backend the stanza was seeded for.
        result: The `StanzaResult` returned by `emit_stanza`.
        write: When true, insert the row into the catalog file.
        target: Per-family shard stem to write into (sharded providers).
        json_output: When true, render as JSON rather than YAML / Rich text.

    Raises:
        typer.Exit: With code 1 when the seed did not succeed.
        typer.BadParameter: If `write_stanza` rejects the target.
    """
    if result.status != "ok":
        if json_output:
            typer.echo(json.dumps(result.to_dict(), indent=2))
        else:
            err_console().print(f"[red]{result.status}:[/red] {result.detail}")
        raise typer.Exit(code=1)

    if write:
        try:
            written = write_stanza(info, result, target)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        out_console().print(
            f"[green]wrote {result.key}[/green] ({result.provider}) -> {written}",
            soft_wrap=True,
        )
        return

    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        out_console().print(
            f"[dim]# paste into the curated datasets: block ({result.provider})[/dim]"
        )
        typer.echo(result.to_yaml())


def _curate_all(info, *, write: bool, limit: int | None) -> None:
    """Run `curate ecmwf --all`: bulk-seed every uncurated dataset into shards.

    Args:
        info: The backend (must be ecmwf).
        write: `--all` mutates the catalog shards, so `--write` is required.
        limit: Only seed the first N uncurated datasets (None = all).
    """
    seeders = dispatch_table("seeder")
    seed = seeders.get(info.provider)
    if seed is None:
        supported = ", ".join(sorted(seeders)) or "no providers"
        raise typer.BadParameter(f"--all is only supported for {supported}")
    if not write:
        raise typer.BadParameter("--all writes the catalog shards; pass --write")
    summary = seed(limit=limit)
    out_console().print(
        f"[green]seeded {summary['seeded']}[/green] / "
        f"{summary['candidates']} uncurated "
        f"(skipped {summary['skipped']})"
    )


def _curate_fill_empty(
    info, *, write: bool, limit: int | None, timeout: int = 180
) -> None:
    """Run `curate gee/ecmwf --fill-empty`: bulk-hydrate placeholder rows in place.

    Args:
        info: The backend (gee or ecmwf).
        write: `--fill-empty` mutates the catalog, so `--write` is required.
        limit: Only hydrate the first N placeholder rows (None = all).
        timeout: Per-dataset retrieve deadline in seconds (ecmwf only; 0 = none).
    """
    if not write:
        raise typer.BadParameter("--fill-empty rewrites the catalog; pass --write")
    hydrators = dispatch_table("hydrator")
    hydrate = hydrators.get(info.provider)
    if hydrate is None:
        supported = ", ".join(sorted(hydrators)) or "no providers"
        raise typer.BadParameter(f"--fill-empty is only supported for {supported}")
    summary = hydrate(limit=limit, timeout=timeout or None)

    timed_out = summary.get("timed_out") or 0
    unmatched = summary.get("unmatched") or 0
    tail = f", {timed_out} timed out" if timed_out else ""
    if unmatched:
        tail += f", {unmatched} retrieved with no confident match"
    out_console().print(
        f"[green]hydrated {summary['hydrated']}[/green] / "
        f"{summary['candidates']} placeholder rows "
        f"(skipped {summary['skipped']}{tail})"
    )


@datasets_app.command()
def audit(
    providers: str = typer.Argument(
        ...,
        help="Provider(s) to audit: a name, a comma-separated list, or 'all'.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit non-zero if any curated dataset is no longer served live.",
    ),
    coverage: bool = typer.Option(
        False,
        "--coverage",
        help="Classify the available universe by curation status "
        "(DONE/addressable/thin/table/missing) instead of drift. gee only.",
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the outcomes as JSON (for piping)."
    ),
) -> None:
    """Audit curated datasets against what each provider serves LIVE.

    Goes to the **network** (like `refresh`): flags `broken` curated datasets
    the provider no longer serves — the drift a `--strict` CI gate fails on —
    and, informationally, live ids missing from the bundled index. Providers
    without a public listing endpoint report `unsupported`. With `--coverage`
    it switches to a **curation-coverage** report instead: it buckets every id
    in the provider's available universe as already curated (DONE), worth
    curating (addressable), needing hand-modelling (thin), out of raster scope
    (table), or gone (missing), and lists the highest-value `addressable` ids
    to curate next (currently gee only).
    """
    selected = _select_refresh_backends(providers)
    if coverage:
        _audit_coverage(selected, json_output=json_output)
        return
    if not json_output:
        err_console().print(
            f"[dim]Auditing {len(selected)} provider(s) against live...[/dim]"
        )
    outcomes = [audit_one(info) for info in selected]

    if json_output:
        typer.echo(json.dumps([o.to_dict() for o in outcomes], indent=2))
    else:
        out_console().print(audit_table(outcomes))
        for outcome in outcomes:
            if outcome.status == "ok" and outcome.broken:
                out_console().print(
                    f"[red]broken in {outcome.provider}:[/red] "
                    f"{', '.join(outcome.broken)}"
                )

    if strict and any(o.broken for o in outcomes):
        raise typer.Exit(code=1)


def _audit_coverage(selected, *, json_output: bool) -> None:
    """Render the `audit --coverage` curation-coverage report.

    Args:
        selected: The backends to classify.
        json_output: Emit JSON instead of the rich table + TODO list.
    """
    if not json_output:
        err_console().print(
            f"[dim]Classifying {len(selected)} provider(s) for curation "
            "coverage...[/dim]"
        )
    outcomes = [coverage_one(info) for info in selected]
    if json_output:
        typer.echo(json.dumps([o.to_dict() for o in outcomes], indent=2))
        return
    out_console().print(coverage_table(outcomes))
    for outcome in outcomes:
        if outcome.status == "ok" and outcome.todo:
            shown = ", ".join(outcome.todo[:20])
            more = "" if len(outcome.todo) <= 20 else f" … (+{len(outcome.todo) - 20})"
            out_console().print(
                f"[yellow]curate next in {outcome.provider}:[/yellow] {shown}{more}"
            )


@datasets_app.command()
def validate(
    providers: str = typer.Argument(
        ...,
        help="Provider(s) to validate: a name, a comma-separated list, or 'all'.",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero if any curated entry has an issue."
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Also run the live reachability check (network/SDK; opt-in).",
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the results as JSON (for piping)."
    ),
) -> None:
    """Validate each curated entry of a provider (per-entry, not index-diff).

    For curated-enumeration providers (no discoverable upstream index, so
    `refresh`/`audit` do not apply), this checks each curated entry — an
    offline structural lint by default. With `--live` it additionally goes
    to the **network/SDK** to confirm each entry still resolves upstream
    (e.g. an S3 object is reachable, an Overture type still serves a
    `sources` column, a GHSL artefact HEADs 200, an openEO recipe's base
    collection / processes are live). Providers without a validator report
    `unsupported`. `--strict` exits non-zero if any entry fails (CI gating).
    """
    selected = _select_refresh_backends(providers)
    results = [validate_one(info, live=live) for info in selected]

    if json_output:
        typer.echo(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        out_console().print(validate_table(results))
        for result in results:
            for issue in result.issues:
                out_console().print(f"[red]{result.provider}:[/red] {issue}")

    if strict and any(r.issues for r in results):
        raise typer.Exit(code=1)
