"""Curation probes — extract a dataset's band/asset schema from a live sample.

The companion to :mod:`earthlens.cli.refresh`. Where `refresh` regenerates the
informational `available_*` index, `probe` produces the *seed* for the curated,
load-bearing rows: it fetches one sample record from a provider and records the
per-band / per-asset metadata (media type, common name, dtype, nodata) a
maintainer reviews before pasting into the catalog. This is the CLI port of the
`tools/*/probe_*.py` scripts.

Like `refresh`, only providers with a public, no-auth, no-SDK sample endpoint
have a prober wired up (currently STAC via plain `requests`); others report
`unsupported`. Adding one is a single entry in :data:`_PROBERS`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from earthlens.cli.adapter import BackendInfo, load_catalog
from earthlens.cli.refresh import _get_json


@dataclass
class ProbeResult:
    """The asset/band schema probed for one dataset.

    Attributes:
        provider: Canonical provider id.
        dataset: The dataset / collection probed.
        status: `"ok"`, `"unsupported"` (no prober), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        assets: Mapping of asset key -> `{media_type, common_name, dtype,
            nodata}` (empty unless `status == "ok"`).
    """

    provider: str
    dataset: str
    status: str
    detail: str = ""
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Project the result to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - The probed asset schema is nested under `assets`:

                ```python
                >>> from earthlens.cli.curate import ProbeResult
                >>> result = ProbeResult(
                ...     "stac", "sentinel-2-l2a", "ok",
                ...     assets={"B04": {"common_name": "red"}},
                ... )
                >>> result.to_dict()["assets"]["B04"]["common_name"]
                'red'

                ```
        """
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "status": self.status,
            "detail": self.detail,
            "assets": self.assets,
        }


def _asset_fields(asset: Any) -> dict[str, Any]:
    """Return a plain field dict for a raw STAC asset (or a pystac `Asset`)."""
    if isinstance(asset, dict):
        return asset
    fields: dict[str, Any] = dict(getattr(asset, "extra_fields", {}) or {})
    media_type = getattr(asset, "media_type", None)
    if media_type is not None:
        fields.setdefault("type", media_type)
    return fields


def _asset_schema(item: Any) -> dict[str, dict[str, Any]]:
    """Extract a per-asset `{media_type, common_name, dtype, nodata}` schema.

    Reads each asset's media type and the first `raster:bands` / `eo:bands`
    entry — the fields the catalog `Asset` model curates.

    Args:
        item: A raw STAC item dict (or a pystac `Item`) with an `assets` map.

    Returns:
        Mapping of asset key to its schema (fields `None` when absent).

    Examples:
        - Recover the band schema from a STAC item's assets:

            ```python
            >>> from earthlens.cli.curate import _asset_schema
            >>> item = {"assets": {"B04": {"type": "image/tiff",
            ...     "eo:bands": [{"common_name": "red"}],
            ...     "raster:bands": [{"data_type": "uint16", "nodata": 0}]}}}
            >>> _asset_schema(item)["B04"]["common_name"]
            'red'
            >>> _asset_schema(item)["B04"]["dtype"]
            'uint16'

            ```
        - An asset without band extensions yields `None` fields:

            ```python
            >>> from earthlens.cli.curate import _asset_schema
            >>> _asset_schema({"assets": {"d": {"type": "image/tiff"}}})["d"]["dtype"] is None
            True

            ```
    """
    assets = getattr(item, "assets", None)
    if assets is None and isinstance(item, dict):
        assets = item.get("assets", {})
    schema: dict[str, dict[str, Any]] = {}
    for key, asset in (assets or {}).items():
        fields = _asset_fields(asset)
        first_raster = (fields.get("raster:bands") or [{}])[0]
        first_eo = (fields.get("eo:bands") or [{}])[0]
        schema[key] = {
            "media_type": fields.get("type"),
            "common_name": first_eo.get("common_name"),
            "dtype": first_raster.get("data_type"),
            "nodata": first_raster.get("nodata"),
        }
    return schema


def _stac_endpoint_candidates(catalog: Any, dataset: str) -> list[tuple[Any, str]]:
    """Resolve `(endpoint, collection_id)` pairs to try for `dataset`.

    A curated catalog key resolves to its single endpoint + upstream id; a
    bare collection id is tried against every endpoint.
    """
    record = catalog.datasets.get(dataset)
    endpoint_name = getattr(record, "endpoint", None)
    collection_id = getattr(record, "collection_id", None)
    if endpoint_name in getattr(catalog, "endpoints", {}) and collection_id:
        return [(catalog.endpoints[endpoint_name], collection_id)]
    return [(endpoint, dataset) for endpoint in catalog.endpoints.values()]


def _stac_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Fetch one sample item for a STAC collection and extract its schema.

    Tries each candidate endpoint's `/collections/{id}/items?limit=1` until
    one yields an item.

    Args:
        catalog: The loaded STAC `Catalog`.
        dataset: A collection id (or curated catalog key).

    Returns:
        The per-asset schema from the sample item.

    Raises:
        ValueError: If no endpoint yields a sample item for `dataset`.
    """
    last_error: Exception | None = None
    for endpoint, collection_id in _stac_endpoint_candidates(catalog, dataset):
        url = endpoint.url.rstrip("/") + f"/collections/{collection_id}/items?limit=1"
        try:
            body = _get_json(url)
        except Exception as exc:  # noqa: BLE001 — try the next endpoint
            last_error = exc
            continue
        features = body.get("features") or []
        if features:
            return _asset_schema(features[0])
    suffix = f" (last error: {last_error})" if last_error else ""
    raise ValueError(f"no sample item found for {dataset!r}{suffix}")


def _bands_from_summaries(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a STAC doc's `summaries.eo:bands` (or `gee:bands`) list."""
    summaries = body.get("summaries", {}) or {}
    return summaries.get("eo:bands") or summaries.get("gee:bands") or []


def _openeo_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an openEO collection's band schema (public `/collections/{id}`).

    Args:
        catalog: The loaded openEO `Catalog` (unused; the endpoint is fixed).
        dataset: The collection id.

    Returns:
        Mapping of band name to `{common_name, dtype, gsd, unit}`. Falls back
        to the `cube:dimensions` band names (empty schema) when the
        collection carries no `eo:bands`.
    """
    url = f"https://openeo.dataspace.copernicus.eu/openeo/1.2/collections/{dataset}"
    body = _get_json(url)
    bands = _bands_from_summaries(body)
    if bands:
        return {
            str(band["name"]): {
                "common_name": band.get("common_name"),
                "dtype": band.get("data_type"),
                "gsd": band.get("gsd"),
                "unit": band.get("unit"),
            }
            for band in bands
            if band.get("name")
        }
    dimensions = body.get("cube:dimensions", {}) or {}
    names = next(
        (
            dim.get("values", [])
            for dim in dimensions.values()
            if dim.get("type") == "bands"
        ),
        [],
    )
    return {str(name): {} for name in names}


def _gee_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a GEE asset's band schema from its public EE STAC document.

    Args:
        catalog: The loaded GEE `Catalog` (unused; the STAC doc is the source).
        dataset: The Earth Engine asset id (e.g. `NASA/GDDP-CMIP6`).

    Returns:
        Mapping of band name to `{units, gsd, description}`.
    """
    provider = dataset.split("/", 1)[0]
    filename = dataset.replace("/", "_") + ".json"
    url = (
        f"https://storage.googleapis.com/earthengine-stac/catalog/{provider}/{filename}"
    )
    body = _get_json(url)
    schema: dict[str, dict[str, Any]] = {}
    for band in _bands_from_summaries(body):
        name = band.get("name")
        if not name:
            continue
        gsd = band.get("gsd")
        schema[str(name)] = {
            "units": band.get("gee:units"),
            "gsd": gsd[0] if isinstance(gsd, list) and gsd else gsd,
            "description": (band.get("description") or "").strip()[:60],
        }
    return schema


def _sentinel_hub_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a Sentinel Hub collection's bands from the SDK (offline, no auth).

    Args:
        catalog: The loaded Sentinel Hub `Catalog` (used to resolve a curated
            key to its `sh_collection` name).
        dataset: A curated key (e.g. `sentinel-2-l2a`) or an SDK collection
            name (e.g. `SENTINEL2_L2A`).

    Returns:
        Mapping of band name to `{units, output_types}`.

    Raises:
        KeyError: If `dataset` resolves to no known `DataCollection`.
    """
    from earthlens.sentinel_hub._helpers import import_sentinelhub

    sentinelhub = import_sentinelhub()
    record = catalog.datasets.get(dataset)
    name = getattr(record, "sh_collection", None) or dataset
    collection = sentinelhub.DataCollection[name]
    schema: dict[str, dict[str, Any]] = {}
    for band in getattr(collection, "bands", None) or []:
        units = getattr(band, "units", None) or ()
        types = getattr(band, "output_types", None) or ()
        schema[str(band.name)] = {
            "units": ", ".join(str(getattr(u, "value", u)) for u in units),
            "output_types": ", ".join(getattr(t, "__name__", str(t)) for t in types),
        }
    return schema


#: Provider id -> a callable taking the loaded catalog and a dataset id and
#: returning its per-entry schema.
_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    "stac": _stac_probe,
    "openeo": _openeo_probe,
    "gee": _gee_probe,
    "sentinel_hub": _sentinel_hub_probe,
}


def supported_providers() -> list[str]:
    """Return the provider ids that have a curation prober wired up.

    Returns:
        The sorted provider ids `probe` can sample.

    Examples:
        - STAC is wired up:

            ```python
            >>> from earthlens.cli.curate import supported_providers
            >>> "stac" in supported_providers()
            True

            ```
    """
    return sorted(_PROBERS)


def probe_dataset(info: BackendInfo, dataset: str) -> ProbeResult:
    """Probe one dataset's asset/band schema from a live sample record.

    A provider with no prober returns `"unsupported"`; any fetch / parse
    failure returns `"error"` — neither raises.

    Args:
        info: The backend the dataset belongs to.
        dataset: The dataset / collection id to probe.

    Returns:
        The :class:`ProbeResult`.
    """
    prober = _PROBERS.get(info.provider)
    if prober is None:
        return ProbeResult(
            provider=info.provider,
            dataset=dataset,
            status="unsupported",
            detail="no public sample endpoint wired up for probing",
        )
    try:
        catalog = load_catalog(info)
        assets = prober(catalog, dataset)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return ProbeResult(
            provider=info.provider, dataset=dataset, status="error", detail=str(exc)
        )
    return ProbeResult(
        provider=info.provider, dataset=dataset, status="ok", assets=assets
    )
