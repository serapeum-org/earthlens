"""Curation probes — extract a dataset's band/asset schema from a live sample.

The companion to :mod:`earthlens.cli.refresh`. Where `refresh` regenerates the
informational `available_*` index, `probe` produces the *seed* for the curated,
load-bearing rows: it fetches one sample record from a provider and records the
per-band / per-asset metadata (media type, common name, dtype, nodata) a
maintainer reviews before pasting into the catalog. This is the CLI port of the
`tools/*/probe_*.py` scripts.

Like `refresh`, only providers with a usable sample source have a prober
wired up; others report `unsupported`. Adding one is a single entry in
:data:`_PROBERS`. The heavier **credentialed** samplers (real NetCDF /
granule / CDS retrieval / full NWP availability) live in :data:`_DEEP_PROBERS`
and are reached with `probe --deep` (cmems, earthdata, ecmwf, nwp); `--deep`
falls back to the light prober for providers without a deep sampler.
"""

from __future__ import annotations

import os
import re
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


def _ecmwf_constraints(dataset: str) -> list[dict[str, Any]]:
    """Return a CDS dataset's public `constraints.json` rows (no creds)."""
    from earthlens.ecmwf.constraints import fetch_constraints

    return fetch_constraints(dataset)


def _ecmwf_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an ECMWF/CDS dataset's variables from its public constraints.

    Reads `constraints.json` (public, no credentials — only data retrieval
    needs `~/.cdsapirc`) and unions the `variable` values across rows.

    Args:
        catalog: The loaded ECMWF `Catalog` (unused; the CDS dataset is the key).
        dataset: A CDS dataset id (e.g. `reanalysis-era5-single-levels`).

    Returns:
        Mapping of variable name to `{}` (the seed for the catalog `variables`).
    """
    variables = sorted(
        {
            variable
            for row in _ecmwf_constraints(dataset)
            for variable in (row.get("variable") or [])
        }
    )
    return {str(variable): {} for variable in variables}


def _chc_sample_files(ftp_base: str, limit: int = 10) -> list[str]:
    """Return a sample of filenames under a CHC FTP directory (anonymous)."""
    from ftplib import FTP  # nosec B402

    with FTP("data.chc.ucsb.edu", timeout=_TIMEOUT) as ftp:  # nosec B321
        ftp.login()
        ftp.cwd(ftp_base)
        return sorted(ftp.nlst())[:limit]


def _chc_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a CHC dataset's FTP directory for a sample of filenames.

    Args:
        catalog: The loaded CHC `Catalog` (resolves the dataset's `ftp_bases`).
        dataset: A curated CHC dataset key.

    Returns:
        Mapping of sample filename to `{}` (the seed for the file pattern).

    Raises:
        ValueError: If the dataset has no `ftp_bases`.
    """
    record = catalog.datasets.get(dataset)
    bases = list(getattr(record, "ftp_bases", {}).values()) if record else []
    if not bases:
        raise ValueError(f"no ftp_bases for {dataset!r}")
    return {name: {} for name in _chc_sample_files(bases[0])}


def _tropycal_fields(basin: str, source: str) -> dict[str, dict[str, Any]]:
    """Return a basin's `Storm.to_dataframe()` field schema (samples a season)."""
    import datetime as dt

    import tropycal.tracks as tracks

    track_dataset = tracks.TrackDataset(basin=basin, source=source)
    year = dt.datetime.now(dt.UTC).year - 1
    storm_ids = list(track_dataset.get_season(year).summary().get("id") or [])[:3]
    fields: dict[str, dict[str, Any]] = {}
    for storm_id in storm_ids:
        frame = track_dataset.get_storm(storm_id).to_dataframe(attrs_as_columns=True)
        for column in frame.columns:
            fields.setdefault(str(column), {"dtype": str(frame[column].dtype)})
    return fields


def _tropycal_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a Tropycal basin's live `to_dataframe()` field schema (SDK).

    Args:
        catalog: The loaded Tropycal `Catalog` (resolves the basin's sources).
        dataset: A basin code (e.g. `north_atlantic`).

    Returns:
        Mapping of field name to `{dtype}`.
    """
    record = catalog.datasets.get(dataset)
    sources = getattr(record, "sources", None) or ["hurdat"]
    return _tropycal_fields(dataset, sources[0])


#: ECCC models ship one whole GRIB per variable (no `.idx` byte-index), so the
#: idx-token check can't apply; template families whose URL also needs
#: domain / member / resolution aren't synthesised here either.
_NWP_NO_IDX_FAMILIES = {"gdps", "rdps", "hrdps"}
_NWP_NEEDS_EXTRA_ATTRS = {"hiresw", "href", "gefs"}


def _herbie_models_dir() -> Any:
    """Locate the installed `herbie/models` template directory.

    Raises:
        FileNotFoundError: When `herbie` is not importable (install
            `earthlens[nwp]`; the templates are read as data, no eccodes).
    """
    import pathlib
    import sys

    for entry in sys.path:
        candidate = pathlib.Path(entry) / "herbie" / "models"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "herbie is not installed; install `pip install earthlens[nwp]`"
    )


class _TemplateStub:
    """Minimal Herbie stand-in so a model template's f-strings resolve."""

    def __init__(self, date: Any, fxx: int, product: str) -> None:
        self.date = date
        self.fxx = fxx
        self.product = product

    def __getattr__(self, name: str) -> str:
        return ""


def _nwp_idx_url(models_dir: Any, model: Any, cycle: Any, step: int) -> str:
    """Format a model's `.idx` URL from its installed Herbie template.

    Reads Herbie's own template file as data (via `runpy`) rather than
    importing `herbie` (whose package init pulls the `cfgrib`/`eccodes`
    stack), then evaluates it against a stub to recover the AWS/NOMADS URL.
    """
    import runpy

    namespace = runpy.run_path(str(models_dir / f"{model.model_family}.py"))
    template_cls = namespace.get(model.model_family) or next(
        value
        for value in namespace.values()
        if isinstance(value, type) and hasattr(value, "template")
    )
    stub = _TemplateStub(cycle, step, getattr(model, "product", "") or "")
    template_cls.template(stub)
    if not getattr(model, "product", "") and getattr(stub, "PRODUCTS", None):
        stub = _TemplateStub(cycle, step, list(stub.PRODUCTS)[0])
        template_cls.template(stub)
    sources = stub.SOURCES
    base = sources.get("aws") or sources.get("nomads") or next(iter(sources.values()))
    return base + ".idx"


def _nwp_idx_body(model: Any) -> str:
    """Fetch the live `.idx` text for a model's most recent reachable cycle.

    Raises:
        ValueError: If no recent cycle's `.idx` is reachable.
    """
    import datetime as dt

    models_dir = _herbie_models_dir()
    step = 1 if (getattr(model, "horizon_h", 0) or 0) >= 1 else 0
    for days_back in (1, 2):
        cycle = (
            dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=days_back)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        url = _nwp_idx_url(models_dir, model, cycle, step)
        try:
            response = requests.get(url, timeout=_TIMEOUT)
        except Exception:  # noqa: BLE001 — try the previous day
            continue
        if response.status_code == 200:
            return response.text
    raise ValueError("no recent .idx is reachable for this model")


def _nwp_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an NWP model's bands against its live GRIB `.idx` (no eccodes).

    Reads Herbie's installed template to build the `.idx` URL for a recent
    cycle, fetches it, and reports which of the model's catalog band tokens
    are present — the live drift check `tools/nwp/probe_idx.py` does.

    Args:
        catalog: The loaded NWP `Catalog`.
        dataset: A curated model key.

    Returns:
        Mapping of band name to `{token, present}`.

    Raises:
        ValueError: For models with no `.idx` (ECCC) or whose template needs
            domain / member / resolution, or an unknown model key.
    """
    model = catalog.datasets.get(dataset)
    if model is None:
        raise ValueError(f"unknown NWP model {dataset!r}")
    family = getattr(model, "model_family", None)
    if family in _NWP_NO_IDX_FAMILIES:
        raise ValueError(f"{dataset}: ECCC per-variable files have no .idx to probe")
    if family in _NWP_NEEDS_EXTRA_ATTRS:
        raise ValueError(
            f"{dataset}: template needs domain/member/resolution; use the SDK"
        )
    body = _nwp_idx_body(model)
    bands = getattr(model, "bands", None) or {}
    return {
        str(band): {"token": token, "present": bool(re.search(re.escape(token), body))}
        for band, token in bands.items()
    }


# --------------------------------------------------------------------------- #
# Deep probers (the `--deep` half) — credentialed, data-downloading samplers
# that read the REAL on-disk schema, vs the light public metadata probers
# above. Each live call sits behind a mockable helper; the credentials come
# from the environment (copernicusmarine / earthaccess) or ~/.cdsapirc.
# --------------------------------------------------------------------------- #
def _cmems_deep_sample(dataset_id: str) -> dict[str, dict[str, Any]]:
    """Open a CMEMS dataset lazily and read its real NetCDF variables (creds)."""
    import copernicusmarine

    dataset = copernicusmarine.open_dataset(dataset_id=dataset_id)
    schema: dict[str, dict[str, Any]] = {}
    for name, variable in dataset.data_vars.items():
        attrs = variable.attrs
        schema[str(name)] = {
            "units": attrs.get("units"),
            "standard_name": attrs.get("standard_name"),
            "long_name": attrs.get("long_name"),
            "dtype": str(variable.dtype),
        }
    return schema


def _cmems_deep_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe a CMEMS dataset's true NetCDF variable schema (credentialed).

    Unlike the light `describe` prober, this opens the dataset (lazily, no
    full download) to read the variable names / units / dtype as they
    actually appear in the served NetCDF. Needs
    `COPERNICUSMARINE_SERVICE_USERNAME` / `_PASSWORD`.
    """
    return _cmems_deep_sample(dataset)


def _earthdata_deep_sample(
    short_name: str, version: str, provider: str
) -> dict[str, dict[str, Any]]:
    """Search one recent granule and record its format / output_kind (creds)."""
    import datetime as dt

    import earthaccess

    from earthlens.cli.stanza import _format_from_extension, _infer_output_kind

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


def _earthdata_deep_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe an Earthdata collection by sampling a real granule (creds).

    Unlike the light UMM-Var prober, this searches CMR for one recent
    granule and records its on-disk format + inferred output_kind. Needs
    `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`.
    """
    record = catalog.datasets.get(dataset)
    short_name = getattr(record, "short_name", None) or dataset
    return _earthdata_deep_sample(
        short_name,
        str(getattr(record, "version", "") or ""),
        str(getattr(record, "provider", "") or ""),
    )


def _ecmwf_deep_sample(dataset: str) -> dict[str, dict[str, Any]]:
    """Retrieve a tiny CDS NetCDF and read each variable's long_name/units.

    Builds a minimal request from the dataset's first `constraints.json`
    row, retrieves it via `cdsapi` (`~/.cdsapirc`), and reads the NetCDF.
    """
    import tempfile
    from pathlib import Path

    import cdsapi
    import netCDF4  # noqa: F401 — the reader below

    rows = _ecmwf_constraints(dataset)
    if not rows:
        return {}
    row = rows[0]

    def first(key: str, fallback: str) -> str:
        value = row.get(key)
        return value[0] if isinstance(value, list) and value else fallback

    request: dict[str, Any] = {
        "variable": first("variable", "2m_temperature"),
        "year": first("year", "2020"),
        "month": first("month", "01"),
        "day": first("day", "01"),
        "time": first("time", "00:00"),
        "data_format": "netcdf",
    }
    target = Path(tempfile.mkdtemp()) / "probe.nc"
    cdsapi.Client().retrieve(dataset, request, str(target))
    with netCDF4.Dataset(str(target)) as handle:  # noqa: F821 — imported above
        schema: dict[str, dict[str, Any]] = {}
        for name, variable in handle.variables.items():
            long_name = getattr(variable, "long_name", "")
            units = getattr(variable, "units", "")
            if long_name or units:
                schema[str(name)] = {"long_name": long_name, "units": units}
    return schema


def _ecmwf_deep_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe an ECMWF/CDS dataset by retrieving a tiny NetCDF (creds).

    Unlike the light constraints prober (variable *names* only), this
    actually retrieves a minimal slice via cdsapi to read each variable's
    real `long_name` / `units`. Needs `~/.cdsapirc`; the CDS queue can take
    minutes.
    """
    return _ecmwf_deep_sample(dataset)


def _nwp_recent_cycle(model: Any) -> Any:
    """Return the model's most recent run datetime (~8 h in the past)."""
    import datetime as dt

    moment = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=8)
    hours = sorted(getattr(model, "cycles_utc", None) or []) or [0]
    for day_offset in (0, 1):
        day = moment - dt.timedelta(days=day_offset)
        for hour in reversed(hours):
            candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= moment:
                return candidate
    return moment.replace(hour=hours[0], minute=0, second=0, microsecond=0)


def _nwp_availability(model: Any, cycle: Any, step: int) -> str:
    """Return a live 'is this fetchable now?' status, dispatching on backend.

    Ports `tools/nwp/probe_nwp_model.py`: a cheap availability check per
    centre (HTTP HEAD / unsigned-S3 head_object / ecmwf-opendata latest /
    Météo-France GetCapabilities / Herbie GRIB resolve). No bulk download.
    """
    backend = getattr(model, "backend", None)
    bands = getattr(model, "bands", None) or {}
    options = getattr(model, "request_options", None) or {}

    if backend == "direct-https":
        if not getattr(model, "url_template", None) or not bands:
            return "no url_template/bands"
        import requests

        var = next(iter(bands.values()))
        url = model.url_template.format(
            cycle=cycle, date=cycle, step=step, var=var, var_lc=var.lower()
        )
        try:
            code = requests.head(
                url, timeout=_TIMEOUT, allow_redirects=True
            ).status_code
            return f"HTTP {code} ({url})"
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            return f"unreachable: {type(exc).__name__} ({url})"

    if backend == "direct-boto3":
        bucket = options.get("bucket")
        key_template = options.get("key_template") or getattr(model, "url_template", "")
        if not (bucket and key_template and bands):
            return "no bucket/key_template/bands"
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config

        var = next(iter(bands.values()))
        key = key_template.format(
            cycle=cycle, date=cycle, step=step, var=var, var_lc=var.lower()
        )
        client = boto3.client(
            "s3",
            region_name=options.get("region", "eu-west-1"),
            config=Config(signature_version=UNSIGNED),
        )
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            return f"OK {head['ContentLength']} bytes (s3://{bucket}/{key})"
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            return f"unreachable: {type(exc).__name__} (s3://{bucket}/{key})"

    if backend == "ecmwf-opendata":
        try:
            from ecmwf.opendata import Client
        except ImportError:
            return "ecmwf-opendata not installed (pip install earthlens[nwp])"
        client = Client(source="aws", model=options.get("ecmwf_model", "ifs"))
        request = {"type": options.get("type", "fc"), "step": 0}
        if options.get("stream"):
            request["stream"] = options["stream"]
        try:
            return f"latest cycle {client.latest(**request):%Y-%m-%d %HZ}"
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            return f"unreachable: {type(exc).__name__}: {exc}"

    if backend == "meteofrance-api":
        import os

        import requests

        api_base, service = options.get("api_base"), options.get("coverage_service")
        if not (api_base and service):
            return "no api_base/coverage_service in request_options"
        key = os.environ.get("METEO_FRANCE_API_KEY") or os.environ.get("MF_API_KEY")
        if not key:
            return "needs METEO_FRANCE_API_KEY"
        url = f"{api_base}/wcs/{service}/GetCapabilities"
        try:
            code = requests.get(
                url,
                params={"service": "WCS", "version": "2.0.1"},
                headers={"apikey": key},
                timeout=_TIMEOUT,
            ).status_code
            return f"HTTP {code} ({url})"
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            return f"unreachable: {type(exc).__name__} ({url})"

    if backend == "herbie":
        import contextlib
        import io

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                from herbie import Herbie
        except Exception:  # noqa: BLE001 — optional SDK / eccodes binary
            return "herbie unavailable (needs the [nwp] extra + eccodes binary)"
        kwargs: dict[str, Any] = {"model": model.model_family, "fxx": step}
        if getattr(model, "product", None) is not None:
            kwargs["product"] = model.product
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                grib = Herbie(cycle, **kwargs).grib
            return f"resolved {grib}" if grib else "no GRIB at this cycle/step"
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            return f"unreachable: {type(exc).__name__}: {exc}"

    return f"no live availability probe for backend {backend!r}"


def _nwp_deep_sample(model: Any, step: int) -> dict[str, dict[str, Any]]:
    """Return a model's live availability for its most recent cycle."""
    cycle = _nwp_recent_cycle(model)
    backend = getattr(model, "backend", "?")
    return {
        f"{backend} @ {cycle:%Y-%m-%d %HZ}": {
            "status": _nwp_availability(model, cycle, step),
            "step": step,
        }
    }


def _nwp_deep_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe an NWP model's live availability (full dispatch, not .idx).

    Ports `tools/nwp/probe_nwp_model.py`: checks whether the model's most
    recent cycle is fetchable right now via its real backend (Herbie needs
    the eccodes binary; ecmwf-opendata / boto3 / meteofrance need their
    SDKs / keys). The light `probe nwp` only checks `.idx` band tokens.

    Raises:
        ValueError: If `dataset` is not a curated NWP model.
    """
    model = catalog.datasets.get(dataset)
    if model is None:
        raise ValueError(f"unknown NWP model {dataset!r}")
    step = 1 if (getattr(model, "horizon_h", 0) or 0) >= 1 else 0
    return _nwp_deep_sample(model, step)


#: Provider id -> a credentialed deep sampler (the `--deep` half of probe).
_DEEP_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    "cmems": _cmems_deep_probe,
    "earthdata": _earthdata_deep_probe,
    "ecmwf": _ecmwf_deep_probe,
    "nwp": _nwp_deep_probe,
}


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
    "ecmwf": _ecmwf_probe,
    "chc": _chc_probe,
    "tropycal": _tropycal_probe,
    "nwp": _nwp_probe,
}


def supported_providers(deep: bool = False) -> list[str]:
    """Return the provider ids that have a curation prober wired up.

    Args:
        deep: When `True`, include providers that only have a credentialed
            `--deep` sampler (none currently — every deep provider also has
            a light prober).

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
    providers = set(_PROBERS)
    if deep:
        providers |= set(_DEEP_PROBERS)
    return sorted(providers)


def probe_dataset(info: BackendInfo, dataset: str, deep: bool = False) -> ProbeResult:
    """Probe one dataset's asset/band schema from a live sample record.

    With `deep=True` it uses the credentialed heavy sampler (real NetCDF /
    granule / CDS retrieval) when the provider has one, falling back to the
    light public prober otherwise. A provider with neither returns
    `"unsupported"`; any fetch / parse failure returns `"error"` — neither
    raises.

    Args:
        info: The backend the dataset belongs to.
        dataset: The dataset / collection id to probe.
        deep: Use the credentialed deep sampler when available.

    Returns:
        The :class:`ProbeResult`.
    """
    prober = (_DEEP_PROBERS.get(info.provider) if deep else None) or _PROBERS.get(
        info.provider
    )
    if prober is None:
        return ProbeResult(
            provider=info.provider,
            dataset=dataset,
            status="unsupported",
            detail="no sample endpoint wired up for probing",
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
