"""Catalog-tooling handlers for the Earthdata (NASA CMR) backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._imagery_cli`). Every listing / probe read is
public against NASA CMR (UMM-JSON); the deep prober samples a real granule and
so needs `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` via `earthaccess`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from earthlens.cli.toolkit import (
    HTTP_TIMEOUT,
    curated_attr_ids,
    get_json,
    index_writer,
)

#: NASA CMR search endpoints (public, anonymous; UMM-JSON).
_CMR_COLLECTIONS_URL = "https://cmr.earthdata.nasa.gov/search/collections.umm_json"
_CMR_VARIABLES_URL = "https://cmr.earthdata.nasa.gov/search/variables.umm_json"

#: Cap on `CMR-Search-After` pages followed per provider — a guard against a
#: misbehaving endpoint paginating forever.
_MAX_PAGES = 50

#: Curated-id resolver over the catalog's `short_name` column (the `audit` axis).
curated_ids = curated_attr_ids("short_name")

#: Persist a live fetch back into the bundled `available_datasets` index.
writer = index_writer("available_datasets")


def _cmr_page(provider: str, search_after: str | None) -> tuple[list[str], str | None]:
    """Fetch one CMR collections page for `provider`.

    Args:
        provider: A CMR provider code (e.g. `"GES_DISC"`).
        search_after: The `CMR-Search-After` cursor from the previous page,
            or `None` for the first page.

    Returns:
        `(short_names, next_search_after)` — the page's collection short
        names and the cursor for the next page (`None` when exhausted).
    """
    headers = {"CMR-Search-After": search_after} if search_after else {}
    params: dict[str, str | int] = {"provider": provider, "page_size": 2000}
    response = requests.get(
        _CMR_COLLECTIONS_URL,
        params=params,
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    names = [
        short
        for item in response.json().get("items", [])
        if (short := item.get("umm", {}).get("ShortName"))
    ]
    return names, response.headers.get("CMR-Search-After")


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List collection short names per CMR provider, live (public, anonymous).

    Walks `CMR-Search-After` pagination for each provider in the catalog's
    registry (bounded by `_MAX_PAGES` pages per provider).

    Args:
        catalog: The loaded Earthdata `Catalog` (exposes `providers`).

    Returns:
        A mapping of CMR provider code to its sorted collection short names.
    """
    grouped: dict[str, list[str]] = {}
    for code in sorted(catalog.providers):
        names: set[str] = set()
        search_after: str | None = None
        for _ in range(_MAX_PAGES):
            page, search_after = _cmr_page(code, search_after)
            names.update(str(name) for name in page)
            if not search_after:
                break
        grouped[code] = sorted(names)
    return grouped


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an Earthdata collection's UMM-Var variables (public CMR).

    Resolves the dataset to its CMR collection, reads the collection's
    associated variable concept-ids (`meta.associations.variables`), and
    fetches their UMM-Var records. Many collections register no variables —
    then the schema is empty, which is accurate.

    Args:
        catalog: The loaded Earthdata `Catalog` (resolves a key's short_name
            / provider).
        dataset: A curated key or a CMR collection short name.

    Returns:
        Mapping of variable name to `{long_name, units, data_type}`.

    Raises:
        ValueError: If no CMR collection matches the dataset.
    """
    record = catalog.datasets.get(dataset)
    short_name = getattr(record, "short_name", None) or dataset
    params: dict[str, Any] = {"short_name": short_name, "page_size": 1}
    provider = getattr(record, "provider", None)
    if provider:
        params["provider"] = provider
    collections = get_json(_CMR_COLLECTIONS_URL, params=params).get("items", [])
    if not collections:
        raise ValueError(f"no CMR collection for {short_name!r}")
    variable_ids = (
        collections[0].get("meta", {}).get("associations", {}).get("variables", [])
    )
    if not variable_ids:
        return {}
    body = get_json(
        _CMR_VARIABLES_URL, params={"concept_id": variable_ids, "page_size": 2000}
    )
    schema: dict[str, dict[str, Any]] = {}
    for item in body.get("items", []):
        umm = item.get("umm", {})
        name = umm.get("Name")
        if name:
            schema[str(name)] = {
                "long_name": umm.get("LongName"),
                "units": umm.get("Units"),
                "data_type": umm.get("DataType"),
            }
    return schema


#: File-extension -> coarse catalog `format` label.
_FORMAT_BY_EXT: dict[str, str] = {
    ".nc": "netcdf4",
    ".nc4": "netcdf4",
    ".h5": "hdf5",
    ".he5": "hdf-eos5",
    ".hdf": "hdf-eos2",
    ".tif": "cog",
    ".tiff": "cog",
    ".csv": "csv",
    ".json": "geojson",
    ".geojson": "geojson",
    ".gpkg": "geopackage",
    ".zip": "zip",
}
#: Short-name substrings that imply a point/profile (vector) product.
_VECTOR_HINTS = ("GEDI", "ATL0", "ATL1", "GLAH")
#: Substrings that imply a plain tabular product.
_TABULAR_HINTS = ("CSV", "_TABLE", "FLUXNET")


def _format_from_extension(filename: str) -> str:
    """Infer a coarse catalog `format` label from a granule filename."""
    suffix = Path(filename.split("?", 1)[0]).suffix.lower()
    return _FORMAT_BY_EXT.get(suffix, "")


def _infer_output_kind(short_name: str, fmt: str = "", title: str = "") -> str:
    """Seed an Earthdata row's `output_kind` from its name / format / title.

    Favours `raster` (the bulk of Earthdata holdings); point/profile
    products map to `vector` and plain tables to `tabular`. A seed — vet
    by hand.

    Args:
        short_name: CMR collection short name.
        fmt: Coarse format label (e.g. from `_format_from_extension`).
        title: Collection title, if available.

    Returns:
        One of `"raster"`, `"vector"`, `"tabular"`.
    """
    haystack = f"{short_name} {title}".upper()
    if fmt in {"csv", "geojson", "geopackage"} or any(
        hint in haystack for hint in _TABULAR_HINTS
    ):
        return "vector" if fmt in {"geojson", "geopackage"} else "tabular"
    if any(hint in short_name.upper() for hint in _VECTOR_HINTS):
        return "vector"
    return "raster"


def _deep_sample(
    short_name: str, version: str, provider: str
) -> dict[str, dict[str, Any]]:
    """Search one recent granule and record its format / output_kind (creds)."""
    import datetime as dt

    import earthaccess

    earthaccess.login(strategy="environment")
    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(days=30)
    granules = earthaccess.search_data(
        short_name=short_name,
        version=version or None,
        provider=provider or None,
        temporal=(start.isoformat(), end.isoformat()),
        count=1,
    )
    if not granules:
        return {}
    links = getattr(granules[0], "data_links", list)() or [""]
    url = links[0]
    fmt = _format_from_extension(url) or "unknown"
    name = url.rsplit("/", 1)[-1] or short_name
    return {
        name: {
            "format": fmt,
            "output_kind": _infer_output_kind(short_name, fmt),
            "url": url,
        }
    }


def deep_prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe an Earthdata collection by sampling a real granule (creds).

    Unlike the light UMM-Var prober, this searches CMR for one recent
    granule and records its on-disk format + inferred output_kind. Needs
    `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`.
    """
    record = catalog.datasets.get(dataset)
    short_name = getattr(record, "short_name", None) or dataset
    return _deep_sample(
        short_name,
        str(getattr(record, "version", "") or ""),
        str(getattr(record, "provider", "") or ""),
    )


def _collection_umm(short_name: str, version: str) -> dict[str, Any]:
    """Return one CMR collection's UMM body (or `{}` when none matches)."""
    params: dict[str, Any] = {"short_name": short_name, "page_size": 1}
    if version:
        params["version"] = version
    items = get_json(_CMR_COLLECTIONS_URL, params=params).get("items", [])
    return items[0].get("umm", {}) if items else {}


def emitter(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed an Earthdata `datasets:` row from a CMR collection.

    Args:
        catalog: The loaded Earthdata `Catalog` (unused; CMR is the source).
        upstream_id: The collection short name.
        **opts: `version`, `cmr_provider`, `daac`, `cloud_hosted`.

    Returns:
        The seeded row.
    """
    version = str(opts.get("version") or "")
    provider = str(opts.get("cmr_provider") or "")
    umm = {} if opts.get("minimal") else _collection_umm(upstream_id, version)
    title = umm.get("EntryTitle", "")
    fmt = _format_from_extension(str(umm.get("ArchiveAndDistributionInformation", {})))
    return {
        "short_name": upstream_id,
        "version": version,
        "daac": str(opts.get("daac") or provider),
        "provider": provider,
        "cadence": "irregular",
        "format": fmt or "unknown",
        "output_kind": _infer_output_kind(upstream_id, fmt, title),
        "cloud_hosted": bool(opts.get("cloud_hosted")),
        "requires_harmony_for_subset": False,
        "supports_harmony": False,
    }
