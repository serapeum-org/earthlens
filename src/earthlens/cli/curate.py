"""Curation probes — extract a dataset's band/asset schema from a live sample.

The companion to :mod:`earthlens.cli.refresh`. Where `refresh` regenerates the
informational `available_*` index, `probe` produces the *seed* for the curated,
load-bearing rows: it fetches one sample record from a provider and records the
per-band / per-asset metadata (media type, common name, dtype, nodata) a
maintainer reviews before pasting into the catalog. This is the CLI port of the
`tools/*/probe_*.py` scripts.

Like `refresh`, only providers with a usable sample source have a prober
wired up; others report `unsupported`. Adding one is a single entry in
:data:`_PROBERS`. The heavier credentialed / GRIB-`.idx` / full-basin
sampling probes (ecmwf cdsapi, nwp, tropycal, chc) stay in `tools/` —
their CLI seed would need credentials or a slow whole-archive read.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from earthlens.cli.adapter import BackendInfo, load_catalog
from earthlens.cli.refresh import _TIMEOUT, _get_json


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


#: NASA CMR search endpoints (public, anonymous).
_CMR_COLLECTIONS_URL = "https://cmr.earthdata.nasa.gov/search/collections.umm_json"
_CMR_VARIABLES_URL = "https://cmr.earthdata.nasa.gov/search/variables.umm_json"


def _earthdata_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
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
    collections = _get_json(_CMR_COLLECTIONS_URL, params=params).get("items", [])
    if not collections:
        raise ValueError(f"no CMR collection for {short_name!r}")
    variable_ids = (
        collections[0].get("meta", {}).get("associations", {}).get("variables", [])
    )
    if not variable_ids:
        return {}
    body = _get_json(
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


def _infer_dtype(value: str | None) -> str:
    """Infer a coarse dtype (`int` / `float` / `str`) from a sample value."""
    if value is None or value == "":
        return "str"
    try:
        int(value)
        return "int"
    except ValueError:
        pass
    try:
        float(value)
        return "float"
    except ValueError:
        return "str"


def _firms_csv_lines(code: str) -> list[str]:
    """Return a tiny FIRMS area-CSV sample's lines (needs `FIRMS_MAP_KEY`)."""
    key = os.environ.get("FIRMS_MAP_KEY", "")
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{code}/world/1"
    response = requests.get(url, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.text.splitlines()


def _firms_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a FIRMS sensor's live CSV column schema (needs `FIRMS_MAP_KEY`).

    Samples one day of the sensor's global area CSV and records each column
    and its inferred dtype — the seed for the catalog `columns:` map.

    Args:
        catalog: The loaded FIRMS `Catalog` (resolves a key's sensor `code`).
        dataset: A curated key or a FIRMS sensor code.

    Returns:
        Mapping of column name to `{dtype}`.
    """
    record = catalog.datasets.get(dataset)
    code = getattr(record, "code", None) or dataset
    lines = _firms_csv_lines(code)
    if not lines:
        return {}
    header = lines[0].split(",")
    first_row = lines[1].split(",") if len(lines) > 1 else []
    schema: dict[str, dict[str, Any]] = {}
    for index, column in enumerate(header):
        value = first_row[index] if index < len(first_row) else None
        schema[column.strip()] = {"dtype": _infer_dtype(value)}
    return schema


def _hdx_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an HDX dataset's resources (files) via public CKAN package_show.

    HDX datasets are file bundles rather than band/variable rasters, so the
    "schema" is the resource list — each downloadable file and its format.

    Args:
        catalog: The loaded HDX `Catalog` (resolves a key's `hdx_id`).
        dataset: A curated key or a CKAN dataset name.

    Returns:
        Mapping of resource (file) name to `{format}`.
    """
    record = catalog.datasets.get(dataset)
    hdx_id = getattr(record, "hdx_id", None) or dataset
    body = _get_json(
        "https://data.humdata.org/api/3/action/package_show", params={"id": hdx_id}
    )
    resources = (body.get("result") or {}).get("resources", [])
    schema: dict[str, dict[str, Any]] = {}
    for resource in resources:
        name = resource.get("name")
        if name:
            schema[str(name)] = {"format": resource.get("format")}
    return schema


def _cmems_describe_dataset(dataset_id: str) -> Any:
    """Return the live Copernicus Marine catalogue for one dataset (SDK)."""
    import copernicusmarine

    return copernicusmarine.describe(dataset_id=dataset_id, disable_progress_bar=True)


def _cmems_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a CMEMS dataset's variables (public `copernicusmarine.describe`).

    Walks the nested catalogue
    (`products[].datasets[].versions[].parts[].services[].variables[]`) and
    records each variable's standard name and units.

    Args:
        catalog: The loaded CMEMS `Catalog` (unused; the SDK is the source).
        dataset: The CMEMS dataset id.

    Returns:
        Mapping of variable short name to `{standard_name, units}`.
    """
    result = _cmems_describe_dataset(dataset)
    schema: dict[str, dict[str, Any]] = {}
    for product in getattr(result, "products", []) or []:
        for entry in getattr(product, "datasets", []) or []:
            for version in getattr(entry, "versions", []) or []:
                for part in getattr(version, "parts", []) or []:
                    for service in getattr(part, "services", []) or []:
                        for variable in getattr(service, "variables", []) or []:
                            name = getattr(variable, "short_name", None)
                            if name:
                                schema[str(name)] = {
                                    "standard_name": getattr(
                                        variable, "standard_name", None
                                    ),
                                    "units": getattr(variable, "units", None),
                                }
    return schema


#: EUMETSAT public browse collections endpoint (no credentials).
_EUMETSAT_BROWSE_URL = "https://api.eumetsat.int/data/browse/collections"


def _eumetsat_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an EUMETSAT collection's public browse metadata (no auth).

    Args:
        catalog: The loaded EUMETSAT `Catalog` (resolves a key's
            `collection_id`).
        dataset: A curated key or an `EO:EUM:DAT:…` collection id.

    Returns:
        A single-entry mapping `{collection_id: {title, abstract, date,
        updated}}`.
    """
    from urllib.parse import quote

    record = catalog.datasets.get(dataset)
    collection_id = getattr(record, "collection_id", None) or dataset
    body = _get_json(
        f"{_EUMETSAT_BROWSE_URL}/{quote(collection_id, safe='')}",
        params={"format": "json"},
    )
    props = (body.get("collection") or {}).get("properties") or {}
    return {
        collection_id: {
            "title": props.get("title"),
            "abstract": (props.get("abstract") or "")[:200],
            "date": props.get("date"),
            "updated": props.get("updated"),
        }
    }


def _worldpop_resolve(catalog: Any, dataset: str) -> tuple[str, str]:
    """Resolve `dataset` to a `(product_alias, sub_alias)` pair.

    Accepts a product alias (uses its first sub-alias) or a sub-alias id
    (finds its parent product).

    Raises:
        ValueError: If `dataset` matches no product or sub-alias.
    """
    record = catalog.datasets.get(dataset)
    if record is not None:
        subs = getattr(record, "subaliases", None) or []
        if subs:
            return dataset, getattr(subs[0], "id", dataset)
    for alias, row in catalog.datasets.items():
        for sub in getattr(row, "subaliases", None) or []:
            if getattr(sub, "id", None) == dataset:
                return alias, dataset
    raise ValueError(f"no WorldPop product or sub-alias matches {dataset!r}")


def _worldpop_records(alias: str, sub_alias: str, iso3: str) -> list[dict[str, Any]]:
    """Return the live WorldPop records for one `(alias, sub_alias, iso3)`."""
    from earthlens.worldpop.rest import rest_records

    return rest_records(alias, sub_alias, iso3)


def _worldpop_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a WorldPop sub-alias's live REST record shape (public).

    Samples one country's records and records each record field's dtype —
    the seed for the catalog's sub-alias maps.

    Args:
        catalog: The loaded WorldPop `Catalog` (resolves the product alias).
        dataset: A product alias or a sub-alias id.

    Returns:
        Mapping of record field name to `{dtype}` (`popyears` carries the
        sampled year spread).
    """
    alias, sub_alias = _worldpop_resolve(catalog, dataset)
    records = _worldpop_records(alias, sub_alias, "USA")
    if not records:
        return {}
    schema: dict[str, dict[str, Any]] = {
        field: {"dtype": type(value).__name__} for field, value in records[0].items()
    }
    schema["popyears"] = {
        "dtype": "list",
        "values": sorted({str(r.get("popyear")) for r in records if r.get("popyear")}),
    }
    return schema


#: A tiny bbox (Times Square block: W, S, E, N) for the Overture probe.
_OVERTURE_BBOX = (-73.9876, 40.7561, -73.9851, 40.7577)


def _overture_columns(overture_type: str) -> dict[str, str]:
    """Return `{column: dtype}` for a tiny Overture bbox fetch (public SDK)."""
    from overturemaps import core

    frame = core.geodataframe(overture_type, bbox=_OVERTURE_BBOX)
    return {str(name): str(dtype) for name, dtype in frame.dtypes.items()}


def _overture_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an Overture feature type's column schema (public `overturemaps`).

    Fetches a tiny bbox for the type and records each column's dtype.

    Args:
        catalog: The loaded Overture `Catalog` (resolves a theme key's
            `default_type`).
        dataset: A curated theme key or an Overture feature type.

    Returns:
        Mapping of column name to `{dtype}`.
    """
    record = catalog.datasets.get(dataset)
    overture_type = getattr(record, "default_type", None) or dataset
    return {
        column: {"dtype": dtype}
        for column, dtype in _overture_columns(overture_type).items()
    }


def _s3_sample_keys(bucket: str, prefix: str, region: str | None) -> list[str]:
    """Return up to five object keys under `prefix` (unsigned `boto3`)."""
    from earthlens.s3.auth import S3Auth, S3Credentials

    client = S3Auth(S3Credentials(region=region)).client()
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=5)
    return [item["Key"] for item in response.get("Contents", [])]


def _s3_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an AWS Open-Data dataset's bucket layout (unsigned `boto3`).

    Lists a few object keys under the dataset's bucket — the seed for
    confirming a dataset's on-disk key layout.

    Args:
        catalog: The loaded S3 `Catalog` (resolves a key's bucket/prefix).
        dataset: A registered dataset name.

    Returns:
        Mapping of object key to `{}`.

    Raises:
        ValueError: If `dataset` is not a registered S3 dataset.
    """
    record = catalog.datasets.get(dataset)
    if record is None:
        raise ValueError(f"unknown S3 dataset {dataset!r}")
    keys = _s3_sample_keys(
        record.bucket,
        getattr(record, "prefix", "") or "",
        getattr(record, "region", None),
    )
    return {key: {} for key in keys}


def _ghsl_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Report a GHSL product's curated (epoch, resolution) matrix (offline).

    GHSL has no per-dataset sample endpoint; its availability is the curated
    `releases` matrix, so this enumerates each release's epoch x resolution
    blocks (and the source CRS each resolution implies) straight from the
    bundled catalog.

    Args:
        catalog: The loaded GHSL `Catalog`.
        dataset: A curated product code / alias.

    Returns:
        Mapping of `"{epoch}@{resolution}"` to `{release, crs}`.

    Raises:
        ValueError: If `dataset` is not a curated GHSL product.
    """
    record = catalog.datasets.get(dataset)
    if record is None:
        raise ValueError(f"unknown GHSL product {dataset!r}")
    schema: dict[str, dict[str, Any]] = {}
    for release, blocks in (getattr(record, "releases", None) or {}).items():
        for block in blocks:
            crs = ", ".join(sorted(block.source_crs()))
            for epoch in block.epochs:
                for resolution in block.resolutions:
                    schema[f"{epoch}@{resolution}"] = {"release": release, "crs": crs}
    return schema


#: Provider id -> a callable taking the loaded catalog and a dataset id and
#: returning its per-entry schema.
_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    "stac": _stac_probe,
    "openeo": _openeo_probe,
    "gee": _gee_probe,
    "sentinel_hub": _sentinel_hub_probe,
    "cmems": _cmems_probe,
    "earthdata": _earthdata_probe,
    "hdx": _hdx_probe,
    "firms": _firms_probe,
    "eumetsat": _eumetsat_probe,
    "worldpop": _worldpop_probe,
    "overture": _overture_probe,
    "s3": _s3_probe,
    "ghsl": _ghsl_probe,
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
