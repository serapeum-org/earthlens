"""Bridge between the provider backends and the federated query commands.

Every backend ships a pydantic catalog (`earthlens.<pkg>.catalog.Catalog`)
but they diverge in small ways the CLI must absorb so a single loop can
scan them all:

* the curated rows usually live in `Catalog.datasets`, but a few backends
  expose them under a different field (`parameters`, `stations`, …);
* the human-readable label is `title` on some records, `description` /
  `name` / `site_name` on others, and absent on a few;
* a backend's optional SDK may be missing, so importing it can fail.

This module reflectively loads each backend's `Catalog` (the catalog
loaders are pure pydantic/YAML, independent of the heavy provider SDKs),
normalises the divergences, and isolates per-backend failures into
:class:`LoadError` records rather than letting one broken backend crash a
federated scan. The richer, cached table built on top of these helpers
lives in :mod:`earthlens.cli.table`.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from earthlens.earthlens import EarthLens

#: Record fields tried, in order, when a backend stores its curated rows
#: under a field other than `datasets` (openaq/usgs_water expose
#: `parameters`, radar `stations`, …). `datasets` is always tried first.
#: Defensive: every backend currently populates `datasets`, so the later
#: fallbacks are a safety net for a future divergent backend, not a path
#: any shipped catalog takes today.
_ROW_FIELDS = ("datasets", "parameters", "stations", "sensors", "models")

#: Attributes tried, in order, to derive a human-readable label for a
#: catalog record. The first non-empty string wins; an empty string is
#: returned when none is present (callers fall back to the dataset id).
_TITLE_FIELDS = ("title", "long_name", "description", "name", "site_name")


@dataclass(frozen=True)
class BackendInfo:
    """Static description of one provider backend.

    Attributes:
        provider: Canonical provider id — the backend's subpackage name
            (e.g. `"chc"`, `"s3"`, `"usgs_water"`). Stable and unique.
        module: The backend's package module (e.g. `"earthlens.chc"`).
        extra: The pip extra that installs the backend's SDK (e.g.
            `"gee"`), or `""` for public/SDK-free backends.
        aliases: Every registry key that resolves to this backend,
            sorted (includes the canonical-ish facade keys and aliases,
            e.g. `("amazon-s3",)` for s3, `("chc", "chirps")` for chc).
    """

    provider: str
    module: str
    extra: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class LoadError:
    """A backend whose catalog could not be loaded.

    Attributes:
        provider: The canonical provider id that failed.
        error: A one-line human-readable reason (typically the missing
            SDK or a catalog parse error).
    """

    provider: str
    error: str


@dataclass
class RawRow:
    """One catalog entry, normalised across backends.

    Attributes:
        provider: Canonical provider id the row belongs to.
        dataset_id: The backend's own key for the dataset (asset id, CDS
            name, sensor code, …).
        title: Human-readable label, or `""` when the record has none.
        record: The backend's pydantic dataset record, untouched, for the
            detail (`show`) command and facet extraction.
    """

    provider: str
    dataset_id: str
    title: str
    record: Any = field(repr=False)


def list_backends() -> list[BackendInfo]:
    """Enumerate the distinct provider backends from the facade registry.

    Reads :attr:`EarthLens.DataSources` and collapses its many keys (the
    facade exposes aliases such as `"chirps"` / `"google-earth-engine"`)
    down to one :class:`BackendInfo` per backend module, so a federated
    scan visits each catalog exactly once.

    Returns:
        The backends sorted by canonical provider id.

    Examples:
        - Every backend has a unique provider id and module:

            ```python
            >>> from earthlens.cli.adapter import list_backends
            >>> backends = list_backends()
            >>> "chc" in {b.provider for b in backends}
            True
            >>> next(b for b in backends if b.provider == "chc").module
            'earthlens.chc'

            ```
    """
    by_module: dict[str, dict[str, Any]] = {}
    for key, module, extra in EarthLens.DataSources.entries():
        entry = by_module.setdefault(module, {"extra": "", "aliases": set()})
        entry["aliases"].add(key)
        if extra and not entry["extra"]:
            entry["extra"] = extra

    backends = [
        BackendInfo(
            provider=module.rsplit(".", 1)[-1],
            module=module,
            extra=entry["extra"],
            aliases=tuple(sorted(entry["aliases"])),
        )
        for module, entry in by_module.items()
    ]
    return sorted(backends, key=lambda b: b.provider)


def known_provider_keys() -> set[str]:
    """Return every accepted provider selector — canonical ids and aliases.

    Used to validate a `--provider` selection before a scan.

    Returns:
        The union of canonical provider ids and all their registry aliases.

    Examples:
        - Both the canonical id and its aliases are accepted:

            ```python
            >>> from earthlens.cli.adapter import known_provider_keys
            >>> keys = known_provider_keys()
            >>> {"chc", "chirps", "amazon-s3"} <= keys
            True

            ```
    """
    keys: set[str] = set()
    for info in list_backends():
        keys.add(info.provider)
        keys.update(info.aliases)
    return keys


def load_catalog(info: BackendInfo) -> Any:
    """Load and construct a backend's `Catalog` by reflection.

    Imports `<module>.catalog` and instantiates its `Catalog` (radar's
    `StationCatalog` is also exported as `Catalog`), which auto-loads the
    bundled YAML on construction.

    Args:
        info: The backend to load.

    Returns:
        The constructed `Catalog` (an `AbstractCatalog` subclass).

    Raises:
        ImportError: If the backend's catalog module cannot be imported
            (e.g. its optional SDK is missing and the package `__init__`
            pulls it in).
        Exception: Any error the catalog loader raises while parsing its
            bundled YAML.
    """
    module = importlib.import_module(f"{info.module}.catalog")
    catalog_cls = module.Catalog
    return catalog_cls()


def _row_mapping(catalog: Any) -> dict[str, Any]:
    """Return the curated rows of a catalog, tolerating field-name drift."""
    for attr in _ROW_FIELDS:
        rows = getattr(catalog, attr, None)
        if isinstance(rows, dict) and rows:
            return rows
    # Fall back to the dict-like surface (iterates `datasets`); empty is fine.
    return dict(getattr(catalog, "datasets", {}) or {})


def record_title(record: Any) -> str:
    """Derive a human-readable label for a catalog record.

    Args:
        record: A backend's pydantic dataset record.

    Returns:
        The first non-empty of the record's `title` / `long_name` /
        `description` / `name` / `site_name`, else `""`.

    Examples:
        - The first populated label field wins, trimmed:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.cli.adapter import record_title
            >>> record_title(SimpleNamespace(title="ERA5 hourly single levels"))
            'ERA5 hourly single levels'

            ```
        - `title` is skipped when blank, falling through to `description`:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.cli.adapter import record_title
            >>> record_title(SimpleNamespace(title="  ", description=" Sea ice "))
            'Sea ice'

            ```
        - A record with no label field yields the empty string:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.cli.adapter import record_title
            >>> record_title(SimpleNamespace(bucket="era5-pds"))
            ''

            ```
    """
    for attr in _TITLE_FIELDS:
        value = getattr(record, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def iter_catalog_rows(info: BackendInfo, catalog: Any) -> Iterator[RawRow]:
    """Yield the normalised rows of one already-loaded catalog.

    Args:
        info: The backend the catalog belongs to.
        catalog: The constructed `Catalog` (see :func:`load_catalog`).

    Yields:
        One :class:`RawRow` per curated dataset, in catalog order.
    """
    for dataset_id, record in _row_mapping(catalog).items():
        yield RawRow(
            provider=info.provider,
            dataset_id=str(dataset_id),
            title=record_title(record),
            record=record,
        )


def load_all_rows(
    providers: list[str] | None = None,
) -> tuple[list[RawRow], list[LoadError]]:
    """Load and normalise every backend's catalog, isolating failures.

    Iterates the backends (optionally filtered to `providers`), loads each
    catalog, and flattens them into one list of :class:`RawRow`. A backend
    that fails to load (missing SDK, parse error) is recorded as a
    :class:`LoadError` and skipped — one broken backend never aborts the
    scan.

    Args:
        providers: Restrict to these canonical provider ids (or any of a
            backend's registry aliases). `None` scans every backend.

    Returns:
        A `(rows, errors)` pair: the normalised rows across all loaded
        backends, and the backends that could not be loaded.
    """
    wanted = set(providers) if providers else None
    rows: list[RawRow] = []
    errors: list[LoadError] = []
    for info in list_backends():
        if wanted is not None and not (
            info.provider in wanted or wanted.intersection(info.aliases)
        ):
            continue
        try:
            catalog = load_catalog(info)
        except Exception as exc:  # noqa: BLE001 — isolate one backend's failure
            errors.append(LoadError(provider=info.provider, error=str(exc)))
            continue
        rows.extend(iter_catalog_rows(info, catalog))
    return rows, errors
