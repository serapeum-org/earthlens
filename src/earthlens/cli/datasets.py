"""The `earthlens datasets …` command group.

Federated queries over every backend's bundled catalog. This module owns
the sub-application object and the commands attached to it:

* `where` — which provider(s) expose a given dataset (the headline use case).
"""

from __future__ import annotations

import difflib
import json

import typer

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
from earthlens.cli.refresh import _TILE_REGENS, audit_one, refresh_one
from earthlens.cli.render import (
    COMPACT_COLUMNS,
    FULL_COLUMNS,
    audit_table,
    counts_table,
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
from earthlens.cli.stanza import emit_stanza
from earthlens.cli.table import FACET_NAMES, build_table
from earthlens.cli.validate import validate_one

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
    include_available: bool = typer.Option(
        False,
        "--include-available",
        help="Also search each backend's full upstream id index (slower).",
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
        help="Restrict to these providers (repeatable / comma-separated).",
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
        help="Restrict to these providers (repeatable / comma-separated).",
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
        except Exception as exc:  # noqa: BLE001 — surfaced, not raised
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
                f"({result['provider']}) -> {result['written']}"
            )
        else:
            err_console().print(
                f"[red]{result['status']}:[/red] {result['provider']} "
                f"{result.get('detail', 'no tile artefact for this provider')}"
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
                f"({outcome.provider}) -> {outcome.written}"
            )


@datasets_app.command()
def probe(
    provider: str = typer.Argument(..., help="Provider id (or alias)."),
    dataset: str = typer.Argument(
        ..., help="Dataset / collection id to sample for its band-asset schema."
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the schema as JSON (for piping)."
    ),
) -> None:
    """Sample one dataset LIVE and print its band/asset schema (curation seed).

    Like `refresh`, this goes to the **network**: it fetches one sample record
    from the provider and records each asset's media type and band metadata
    (common name, dtype, nodata) — the seed a maintainer reviews before adding
    the dataset to the curated catalog. Only providers with a public sample
    endpoint are supported (currently STAC); others report `unsupported`.
    """
    backends = _select_refresh_backends(provider)
    if len(backends) != 1:
        raise typer.BadParameter("probe takes exactly one provider")
    result = probe_dataset(backends[0], dataset)

    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    elif result.status == "ok":
        out_console().print(probe_table(result))
    else:
        err_console().print(f"[red]{result.status}:[/red] {result.detail}")

    if result.status != "ok":
        raise typer.Exit(code=1)


@datasets_app.command()
def curate(
    provider: str = typer.Argument(..., help="Provider id (or alias)."),
    upstream_id: str = typer.Argument(
        ..., help="Upstream id to seed a curated row from (short name / id / code)."
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
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the seeded row as JSON (for piping)."
    ),
) -> None:
    """Author a paste-ready curated `datasets:` row from one upstream id.

    The authoring companion to `probe`: it fetches one upstream id's
    metadata and prints a seeded `datasets:` YAML row (inferring
    `output_kind` / `format` / bands where it can) for you to vet and paste
    into the per-family catalog file. Print-only — it never edits a catalog.
    Only providers with a stanza emitter are supported (earthdata, hdx,
    usgs_water, eumetsat, gee); others report `unsupported`.
    """
    backends = _select_refresh_backends(provider)
    if len(backends) != 1:
        raise typer.BadParameter("curate takes exactly one provider")
    result = emit_stanza(
        backends[0],
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
    )

    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    elif result.status == "ok":
        out_console().print(
            f"[dim]# paste into the curated datasets: block "
            f"({result.provider})[/dim]"
        )
        typer.echo(result.to_yaml())
    else:
        err_console().print(f"[red]{result.status}:[/red] {result.detail}")

    if result.status != "ok":
        raise typer.Exit(code=1)


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
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the outcomes as JSON (for piping)."
    ),
) -> None:
    """Audit curated datasets against what each provider serves LIVE.

    Goes to the **network** (like `refresh`): flags `broken` curated datasets
    the provider no longer serves — the drift a `--strict` CI gate fails on —
    and, informationally, live ids missing from the bundled index. Providers
    without a public listing endpoint report `unsupported`.
    """
    selected = _select_refresh_backends(providers)
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
