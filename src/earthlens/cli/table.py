"""The normalised, cached catalog table that every query command reads.

Rather than have each command re-walk all 22 backends, the CLI builds one
in-memory table of :class:`CatalogRow` — `(provider, dataset_id, title,
cadence, resolution, license, record)` — once per process and filters it
with plain Python. The result is cached for the process lifetime so
successive commands in the same invocation are free, and provider-scoped
builds (`build_table(providers=["chc"])`) load only the backends asked for.

Backends load sequentially: importing a backend package pulls heavy,
shared third-party modules (geopandas, pyramids, the per-backend SDKs)
whose first import is serialized by Python's import lock, so a thread pool
buys nothing here and merely risks concurrent-import deadlocks.

This is the intake-esm "one searchable table" model: `where` / `search` /
`facets` are all `filter` / `groupby` / `unique` over :attr:`CatalogTable.rows`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from earthlens.cli.adapter import (
    BackendInfo,
    LoadError,
    iter_catalog_rows,
    list_backends,
    load_catalog,
)

#: Record attributes tried, in order, to fill each facet column. The first
#: that yields a non-empty token wins.
_CADENCE_FIELDS = ("cadence", "temporal_resolution", "frequency")
_RESOLUTION_FIELDS = ("spatial_resolution", "resolution")
_LICENSE_FIELDS = ("license", "license_id")

#: The facet columns the table exposes for filtering / `facets` discovery.
FACET_NAMES = ("provider", "cadence", "resolution", "license")


def _format_number(value: float) -> str:
    """Render a numeric facet token without a trailing `.0`."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _facet_token(value: Any) -> str:
    """Reduce a record attribute to a clean facet token (or `""`).

    Handles the shapes catalog records actually use: plain strings, the
    `interval`/`unit` cadence objects (gee), numeric resolutions, and
    lists of values (chc's `[0.05]`). Anything else collapses to `""`.

    Args:
        value: A record attribute value.

    Returns:
        A trimmed token string, or `""` when there is nothing useful.

    Examples:
        - Strings are trimmed; cadence objects collapse to a unit token;
          single-item lists unwrap:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.cli.table import _facet_token
            >>> _facet_token("  daily ")
            'daily'
            >>> _facet_token(SimpleNamespace(interval=1, unit="day"))
            'day'
            >>> _facet_token(SimpleNamespace(interval=16, unit="day"))
            '16 day'
            >>> _facet_token([0.05])
            '0.05'

            ```
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return _format_number(value)
    unit = getattr(value, "unit", None)
    if isinstance(unit, str) and unit:
        interval = getattr(value, "interval", None)
        if interval in (None, 1):
            return unit
        return f"{interval} {unit}"
    if isinstance(value, (list, tuple, set)):
        tokens = [t for t in (_facet_token(v) for v in value) if t]
        return ", ".join(dict.fromkeys(tokens))
    return ""


def _first_token(record: Any, attrs: tuple[str, ...]) -> str:
    """Return the first non-empty facet token among `attrs` on `record`."""
    for attr in attrs:
        token = _facet_token(getattr(record, attr, None))
        if token:
            return token
    return ""


@dataclass(frozen=True)
class CatalogRow:
    """One dataset, normalised across every backend for uniform querying.

    Attributes:
        provider: Canonical provider id (the backend subpackage name).
        dataset_id: The backend's own dataset key.
        title: Human-readable label, or `""` when the record has none.
        cadence: Temporal-cadence token (e.g. `"daily"`, `"1 day"`), or `""`.
        resolution: Spatial-resolution token (e.g. `"30"`, `"0.05"`), or `""`.
        license: License token (e.g. `"proprietary"`), or `""`.
        curated: `True` for a hand-curated catalog dataset (with metadata);
            `False` for an id surfaced only from the upstream
            `available_*` index under `--include-available` (id-only, no
            metadata).
        record: The backend's pydantic dataset record, kept for the
            `show` command. Excluded from equality / repr.

    Examples:
        - Read a row's facets and free-text search blob:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> row = CatalogRow(
            ...     "ecmwf", "reanalysis-era5-single-levels",
            ...     "ERA5 hourly single levels", "1 day", "0.25", "",
            ... )
            >>> row.facet("provider")
            'ecmwf'
            >>> row.facet("cadence")
            '1 day'
            >>> "era5" in row.search_text
            True

            ```
        - An unknown facet name returns the empty string:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> row = CatalogRow("s3", "era5", "ERA5 on AWS", "monthly", "", "")
            >>> row.facet("instrument")
            ''

            ```
    """

    provider: str
    dataset_id: str
    title: str
    cadence: str
    resolution: str
    license: str
    curated: bool = True
    record: Any = field(compare=False, repr=False, default=None)

    @property
    def search_text(self) -> str:
        """Lower-cased `provider dataset_id title` blob for free-text search."""
        return f"{self.provider} {self.dataset_id} {self.title}".lower()

    def facet(self, name: str) -> str:
        """Return this row's value for facet `name` (`""` if absent/unknown)."""
        return {
            "provider": self.provider,
            "cadence": self.cadence,
            "resolution": self.resolution,
            "license": self.license,
        }.get(name, "")


def _to_row(raw: Any) -> CatalogRow:
    """Build a :class:`CatalogRow` from an adapter :class:`~adapter.RawRow`."""
    record = raw.record
    return CatalogRow(
        provider=raw.provider,
        dataset_id=raw.dataset_id,
        title=raw.title,
        cadence=_first_token(record, _CADENCE_FIELDS),
        resolution=_first_token(record, _RESOLUTION_FIELDS),
        license=_first_token(record, _LICENSE_FIELDS),
        record=record,
    )


@dataclass(frozen=True)
class CatalogTable:
    """An immutable snapshot of every loaded backend's catalog.

    Attributes:
        rows: Every normalised dataset row, across all loaded backends.
        errors: Backends that could not be loaded (missing SDK, parse
            error) — surfaced to the user, never silently dropped.
        providers: Canonical ids of the backends that were scanned
            (whether or not they loaded), sorted.

    Examples:
        - Inspect the distinct facet values across a small table:

            ```python
            >>> from earthlens.cli.table import CatalogRow, CatalogTable
            >>> rows = (
            ...     CatalogRow("chc", "chirps-daily", "", "daily", "0.05", ""),
            ...     CatalogRow("gee", "ECMWF/ERA5/DAILY", "ERA5", "1 day", "", ""),
            ... )
            >>> table = CatalogTable(rows=rows, errors=(), providers=("chc", "gee"))
            >>> table.facet_values("provider")
            ['chc', 'gee']
            >>> table.facet_values("cadence")
            ['1 day', 'daily']

            ```
        - `present_facets` lists only the facets that carry any value
          (here `license` is empty everywhere, so it is dropped):

            ```python
            >>> from earthlens.cli.table import CatalogRow, CatalogTable
            >>> rows = (CatalogRow("chc", "chirps-daily", "", "daily", "0.05", ""),)
            >>> table = CatalogTable(rows=rows, errors=(), providers=("chc",))
            >>> table.present_facets()
            ['provider', 'cadence', 'resolution']

            ```
    """

    rows: tuple[CatalogRow, ...]
    errors: tuple[LoadError, ...]
    providers: tuple[str, ...]

    def facet_values(self, name: str) -> list[str]:
        """Sorted distinct non-empty values of facet `name` across all rows."""
        return sorted({row.facet(name) for row in self.rows} - {""})

    def present_facets(self) -> list[str]:
        """Facet names that have at least one non-empty value in the table."""
        return [name for name in FACET_NAMES if self.facet_values(name)]


def _matches(info: BackendInfo, wanted: set[str] | None) -> bool:
    """True when `info` is requested by `wanted` (by provider id or alias)."""
    if wanted is None:
        return True
    return info.provider in wanted or bool(wanted.intersection(info.aliases))


def _load_one(
    info: BackendInfo,
) -> tuple[list[CatalogRow], list[str], LoadError | None]:
    """Load one backend's catalog, capturing rows, the upstream index, failures.

    Args:
        info: The backend to load.

    Returns:
        A `(rows, available_ids, error)` triple. `available_ids` is the
        backend's full `available_datasets` index (used only under
        `--include-available`); `error` is non-None when the catalog could
        not be loaded (and `rows` / `available_ids` are then empty).
    """
    try:
        catalog = load_catalog(info)
    except Exception as exc:  # noqa: BLE001 — isolate one backend's failure
        return [], [], LoadError(provider=info.provider, error=str(exc))
    rows = [_to_row(raw) for raw in iter_catalog_rows(info, catalog)]
    available = [str(ident) for ident in getattr(catalog, "available_datasets", [])]
    return rows, available, None


def _available_rows(
    provider: str, available_ids: list[str], curated_ids: set[str]
) -> list[CatalogRow]:
    """Build id-only rows for upstream ids not already curated for `provider`."""
    return [
        CatalogRow(provider, ident, "", "", "", "", curated=False)
        for ident in available_ids
        if ident not in curated_ids
    ]


_CACHE: dict[tuple[tuple[str, ...] | None, bool], CatalogTable] = {}
_CACHE_LOCK = threading.Lock()


def build_table(
    providers: list[str] | None = None,
    *,
    refresh: bool = False,
    include_available: bool = False,
) -> CatalogTable:
    """Build (or return a cached) normalised table of every backend's catalog.

    Loads the requested backends and flattens them into one
    :class:`CatalogTable`. The result is cached per `(providers,
    include_available)` selection for the process lifetime; pass
    `refresh=True` to rebuild.

    Args:
        providers: Restrict to these canonical provider ids (or registry
            aliases). `None` scans every backend.
        refresh: Rebuild even if a cached table exists.
        include_available: Also fold in each backend's upstream
            `available_datasets` index as id-only rows (`curated=False`) —
            thousands of extra ids for earthdata / hdx, hence opt-in.

    Returns:
        The normalised, immutable catalog table.

    Examples:
        - The table federates every backend's datasets:

            ```python
            >>> from earthlens.cli.table import build_table
            >>> table = build_table(providers=["chc"])
            >>> table.providers
            ('chc',)
            >>> all(row.provider == "chc" for row in table.rows)
            True

            ```
    """
    key = (tuple(sorted(providers)) if providers else None, include_available)
    if not refresh:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is not None:
            return cached

    wanted = set(providers) if providers else None
    backends = [info for info in list_backends() if _matches(info, wanted)]

    rows: list[CatalogRow] = []
    errors: list[LoadError] = []
    available_by_provider: list[tuple[str, list[str]]] = []
    for info in backends:
        backend_rows, available_ids, error = _load_one(info)
        rows.extend(backend_rows)
        if error is not None:
            errors.append(error)
        elif include_available:
            available_by_provider.append((info.provider, available_ids))

    if include_available:
        curated_ids = {(row.provider, row.dataset_id) for row in rows}
        for provider, available_ids in available_by_provider:
            seen = {ident for prov, ident in curated_ids if prov == provider}
            rows.extend(_available_rows(provider, available_ids, seen))

    table = CatalogTable(
        rows=tuple(rows),
        errors=tuple(errors),
        providers=tuple(sorted(info.provider for info in backends)),
    )
    with _CACHE_LOCK:
        _CACHE[key] = table
    return table


def clear_table_cache() -> None:
    """Drop the process-lifetime table cache (used by tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()
