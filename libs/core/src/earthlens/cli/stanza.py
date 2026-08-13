"""Stanza emitters — author a curated `datasets:` row from one upstream id.

The authoring companion to :mod:`earthlens.cli.curate` (which probes a
dataset's schema). Where `probe` prints the per-band/asset *schema seed*,
`curate` (this module) prints a ready-to-paste curated `datasets:` row:
it fetches one upstream id's metadata and transcribes the fields the
catalog's pydantic row model curates, inferring `output_kind` / `format`
/ band metadata where it can. The output is a **seed** — the maintainer
vets it before pasting into the per-family catalog file. This is the CLI
port of the `tools/*/refresh_*.py` `add-*` subcommands.

Like `refresh` / `probe`, only providers whose row can be seeded from a
public (or env-credentialed) source have an emitter wired up; others
report `unsupported`. Adding one is a single entry in :data:`_EMITTERS`.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import yaml

from earthlens._cli_tooling import config_table, dispatch_table
from earthlens.cli._gee_categories import categorise_asset
from earthlens.cli.adapter import BackendInfo, load_catalog
from earthlens.cli.refresh import _get_json


@dataclass
class StanzaResult:
    """A curated `datasets:` row authored for one upstream id.

    Attributes:
        provider: Canonical provider id.
        key: The friendly catalog key the row is filed under.
        upstream_id: The upstream id the row was seeded from.
        status: `"ok"`, `"unsupported"` (no emitter), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        row: The seeded row fields (empty unless `status == "ok"`).
    """

    provider: str
    key: str
    upstream_id: str
    status: str
    detail: str = ""
    row: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Project the result to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - The seeded row is nested under `row`:

                ```python
                >>> from earthlens.cli.stanza import StanzaResult
                >>> StanzaResult(
                ...     "usgs_water", "discharge", "00060", "ok",
                ...     row={"code": "00060"},
                ... ).to_dict()["row"]["code"]
                '00060'

                ```
        """
        return {
            "provider": self.provider,
            "key": self.key,
            "upstream_id": self.upstream_id,
            "status": self.status,
            "detail": self.detail,
            "row": self.row,
        }

    def to_yaml(self) -> str:
        """Render the row as a paste-ready `datasets:` YAML fragment.

        Returns:
            The `datasets: {key: row}` block, or `""` when no row was seeded.

        Examples:
            - A seeded row renders under `datasets:`:

                ```python
                >>> from earthlens.cli.stanza import StanzaResult
                >>> print(StanzaResult(
                ...     "usgs_water", "discharge", "00060", "ok",
                ...     row={"code": "00060"},
                ... ).to_yaml().strip())
                datasets:
                  discharge:
                    code: '00060'

                ```
        """
        if not self.row:
            return ""
        return cast(
            "str",
            yaml.safe_dump(
                {"datasets": {self.key: self.row}},
                sort_keys=False,
                allow_unicode=True,
            ),
        )


# --------------------------------------------------------------------------- #
# earthdata — seed from a CMR collection (public umm_json).
# --------------------------------------------------------------------------- #
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
        fmt: Coarse format label (e.g. from :func:`_format_from_extension`).
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


#: NASA CMR collection search (public, anonymous; UMM-JSON).
_CMR_COLLECTIONS_URL = "https://cmr.earthdata.nasa.gov/search/collections.umm_json"


def _earthdata_collection_umm(short_name: str, version: str) -> dict[str, Any]:
    """Return one CMR collection's UMM body (or `{}` when none matches)."""
    params: dict[str, Any] = {"short_name": short_name, "page_size": 1}
    if version:
        params["version"] = version
    items = _get_json(_CMR_COLLECTIONS_URL, params=params).get("items", [])
    return items[0].get("umm", {}) if items else {}


def _emit_earthdata(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
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
    umm = {} if opts.get("minimal") else _earthdata_collection_umm(upstream_id, version)
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


# --------------------------------------------------------------------------- #
# usgs_water — a pure friendly-name -> parameter-code row (no fetch).
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# eumetsat — seed from the public browse endpoint (no credentials).
# --------------------------------------------------------------------------- #
_EUMETSAT_BROWSE_URL = "https://api.eumetsat.int/data/browse/collections"


def _eumetsat_detail(collection_id: str) -> dict[str, Any]:
    """Return one EUMETSAT collection's public browse metadata."""
    url = f"{_EUMETSAT_BROWSE_URL}/{quote(collection_id, safe='')}"
    return _get_json(url, params={"format": "json"})


def _emit_eumetsat(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed an EUMETSAT `datasets:` row from the public browse metadata.

    Args:
        catalog: The loaded EUMETSAT `Catalog` (unused).
        upstream_id: The `EO:EUM:DAT:…` collection id.
        **opts: `group` (Data Store group label).

    Returns:
        The seeded row; the maintainer fills `format` / `selectors` /
        `tailor_product_type` after vetting.
    """
    if not opts.get("minimal"):
        _eumetsat_detail(upstream_id)  # fail loud if the id is unreachable
    return {
        "collection_id": upstream_id,
        "group": str(opts.get("group") or "MSG"),
        "output_kind": "raster",
        "format": "",
        "selectors": [],
        "tailor_product_type": None,
    }


# --------------------------------------------------------------------------- #
# gee — seed from the public Earth Engine STAC document.
# --------------------------------------------------------------------------- #
_GEE_STAC_BASE = "https://storage.googleapis.com/earthengine-stac/catalog"
_GEE_GLOBAL_BBOX = [-180.0, -90.0, 180.0, 90.0]


def _gee_stac_doc(asset_id: str) -> dict[str, Any]:
    """Return an Earth Engine asset's public STAC document."""
    provider = asset_id.split("/", 1)[0]
    filename = asset_id.replace("/", "_") + ".json"
    return _get_json(f"{_GEE_STAC_BASE}/{provider}/{filename}")


def _gee_live_bands(asset_id: str) -> tuple[str, dict[str, dict[str, Any]]]:
    """Query Earth Engine live for an asset's `(ee_type, bands)` (needs creds).

    The credentialed fallback for assets the public STAC tree can't reach
    (mainly community `projects/...` ids). Authenticates the service
    account (`GEE_SERVICE_ACCOUNT` / `GEE_SERVICE_KEY`) and reads the band
    names off the asset's first image.

    Args:
        asset_id: The Earth Engine asset id.

    Returns:
        `(ee_type, {band: {}})` — the asset type (lowercased) and its bands.
    """
    import os

    import ee

    from earthlens.gee.auth import EarthEngineAuth

    EarthEngineAuth.initialize(
        os.environ.get("GEE_SERVICE_ACCOUNT", ""),
        os.environ.get("GEE_SERVICE_KEY", ""),
        os.environ.get("GEE_PROJECT"),
    )
    ee_type = (ee.data.getAsset(asset_id).get("type") or "IMAGE_COLLECTION").lower()
    image = (
        ee.Image(asset_id)
        if ee_type == "image"
        else ee.ImageCollection(asset_id).first()
    )
    bands = ee.Image(image).bandNames().getInfo() or []
    return ee_type, {str(band): {} for band in bands}


def _gsd_to_metres(gsd: Any) -> float | None:
    """Return a band `gsd` as a float (unwrapping a `[value]` list)."""
    value = gsd[0] if isinstance(gsd, list) and gsd else gsd
    return float(value) if isinstance(value, (int, float)) else None


def _emit_gee(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed a GEE `datasets:` row from the asset's public STAC document.

    Args:
        catalog: The loaded GEE `Catalog` (unused; STAC is the source).
        upstream_id: The Earth Engine asset id (e.g. `NASA/GDDP-CMIP6`).
        **opts: `minimal` emits a placeholder row with empty bands;
            `hydrate` reads the bands live from Earth Engine (needs creds —
            the fallback for assets the public STAC tree can't reach).

    Returns:
        The seeded row (title / ee_type / cadence / extent / bands).
    """
    if opts.get("minimal"):
        return {
            "title": upstream_id,
            "ee_type": "image_collection",
            "default_reducer": "median",
            "bands": {},
        }
    if opts.get("hydrate"):
        ee_type, live_bands = _gee_live_bands(upstream_id)
        return {
            "title": upstream_id,
            "ee_type": ee_type,
            "default_reducer": "median",
            "bands": live_bands,
        }
    doc = _gee_stac_doc(upstream_id)
    extent = doc.get("extent", {})
    temporal = (extent.get("temporal", {}).get("interval") or [[None, None]])[0]
    spatial_bbox = (extent.get("spatial", {}).get("bbox") or [None])[0]
    bands = doc.get("summaries", {}).get("eo:bands") or []
    resolutions = [r for r in (_gsd_to_metres(b.get("gsd")) for b in bands) if r]
    providers = doc.get("providers") or []
    interval = doc.get("gee:interval") or {}

    row: dict[str, Any] = {
        "title": (doc.get("title") or upstream_id).splitlines()[0],
        "ee_type": doc.get("gee:type", "image_collection"),
    }
    if providers:
        row["provider"] = providers[0].get("name", "") or ""
    if interval.get("interval") and interval.get("unit"):
        row["cadence"] = {"interval": interval["interval"], "unit": interval["unit"]}
    if resolutions:
        row["spatial_resolution"] = min(resolutions)
    start = (temporal[0] or "")[:10] if temporal and temporal[0] else "1970-01-01"
    row["extent"] = {"start_date": start, "end_date": None}
    if spatial_bbox and list(spatial_bbox) != _GEE_GLOBAL_BBOX:
        row["extent"]["bbox"] = list(spatial_bbox)
    row["default_reducer"] = "median"
    row["bands"] = {
        band["name"]: {
            "description": (
                band.get("description") or band.get("name", "")
            ).splitlines()[0],
            **({"units": band["gee:units"]} if band.get("gee:units") else {}),
            **(
                {"scale": band["gee:scale"]}
                if band.get("gee:scale") is not None
                else {}
            ),
        }
        for band in bands
        if band.get("name")
    }
    return row


def _emit_jaxa(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed a JAXA `datasets:` row from a STAC name or G-Portal numeric id.

    The upstream id's shape decides the protocol: a `JAXA.*` / `NASA.*`
    / `Copernicus.*` string seeds a `jaxa-earth` row (with the default
    band looked up via `je.ImageCollectionList()`); a 7-9 digit numeric
    string seeds a `gportal` row (with the description built from
    `gportal.datasets()`'s mission / level / product path).

    Args:
        catalog: The loaded JAXA `Catalog` (unused; the SDKs are the
            authoritative sources).
        upstream_id: The STAC collection name or G-Portal numeric id.
        **opts: Unused.

    Returns:
        A row dict matching the bundled YAML's `datasets:` shape.
    """
    del catalog
    if re.match(r"^\d{7,9}$", upstream_id):
        # G-Portal numeric id — walk the live tree to find its mission / path.
        import gportal  # type: ignore[import-not-found]

        tree = gportal.datasets()
        for mission, level, path in _walk_gportal(tree):
            if path == upstream_id:
                return {
                    "protocol": "gportal",
                    "short_name": upstream_id,
                    "description": f"{mission} / {level}",
                }
        return {
            "protocol": "gportal",
            "short_name": upstream_id,
            "description": "(unrecognised G-Portal id; verify upstream)",
        }
    # jaxa-earth STAC collection.
    from jaxa.earth import je  # type: ignore[import-not-found]

    ids, bands_per_id = je.ImageCollectionList().filter_name()
    bands: list[str] = []
    try:
        idx = list(ids).index(upstream_id)
        bands = list(bands_per_id[idx])
    except ValueError:
        pass
    row: dict[str, Any] = {
        "protocol": "jaxa-earth",
        "collection": upstream_id,
    }
    if bands:
        row["default_band"] = bands[0]
    return row


def _walk_gportal(node: Any, mission: str = "", level: str = ""):
    """Yield `(mission, level, leaf_id)` triples from a gportal.datasets() tree.

    The tree's top level is `mission -> level -> sensor -> [ids]`; the
    middle layers vary. Anything that's a list of leaf strings is treated
    as the product-id list, parented by whatever level we last saw.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            next_mission = mission or key
            next_level = level if mission else ""
            if mission and not level:
                next_level = key
            yield from _walk_gportal(value, next_mission, next_level)
    elif isinstance(node, list):
        for item in node:
            yield (mission, level, str(item))


def _erddap_info_rows(server_url: str, dataset_id: str) -> list[list[Any]]:
    """Return one ERDDAP dataset's `/info` table rows.

    Each row is `[Row Type, Variable Name, Attribute Name, Data Type,
    Value]` — the shape ERDDAP's `info/<id>/index.json` endpoint emits.

    Args:
        server_url: The ERDDAP base URL (a trailing slash is tolerated).
        dataset_id: The dataset id on that server.

    Returns:
        The `table.rows` list from the `/info` JSON.
    """
    base = server_url.rstrip("/")
    body = _get_json(f"{base}/info/{dataset_id}/index.json")
    return cast("list[list[Any]]", body["table"]["rows"])


def _erddap_global_attr(rows: list[list[Any]], name: str) -> str:
    """Return one `NC_GLOBAL` attribute value from `/info` rows (`""` if absent)."""
    for row in rows:
        if row[1] == "NC_GLOBAL" and row[2] == name:
            return str(row[4])
    return ""


def _emit_erddap(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed an ERDDAP `datasets:` row from a server's `/info` metadata.

    Reads the dataset's `/info/<id>/index.json`: the presence of grid
    `dimension` rows decides `protocol` (`griddap` if dimensioned, else
    `tabledap`), the `variable` rows give the default variable set, and the
    `NC_GLOBAL` `title` / `license` attributes fill the human metadata. The
    server is taken from `--server` if given, else discovered by trying each
    `server_url` the catalog already curates from.

    `--minimal` is ignored: the protocol and dimension order are only
    knowable from `/info`, and that call is a single cheap GET. The emitted
    `variables` list is **every** data variable the dataset exposes — the
    maintainer trims it to the headline set (e.g. drop `*_mask` / `*_qc`)
    before committing.

    Args:
        catalog: The loaded ERDDAP `Catalog` (its curated `server_url`s seed
            the server search).
        upstream_id: The ERDDAP dataset id to seed from.
        **opts: `server` (an explicit ERDDAP base URL to look the id up on).

    Returns:
        The seeded row: `server_url` / `dataset_id` / `protocol` /
        (`dim_names` for griddap) / `variables` / `title` / `license_note`.

    Raises:
        ValueError: If the id is not found on `--server` or any curated
            server.
    """
    server = opts.get("server")
    candidates = (
        [server]
        if server
        else sorted({row.server_url for row in catalog.datasets.values()})
    )
    rows: list[list[Any]] | None = None
    found_server = ""
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            rows = _erddap_info_rows(candidate, upstream_id)
            found_server = candidate
            break
        except Exception as exc:  # noqa: BLE001 — try the next server, report at end
            last_exc = exc
    if rows is None:
        raise ValueError(
            f"{upstream_id!r} not found on any known ERDDAP server "
            f"{candidates} (pass --server <url> to point elsewhere): {last_exc}"
        )
    dim_names = [row[1] for row in rows if row[0] == "dimension"]
    variables = [row[1] for row in rows if row[0] == "variable"]
    protocol = "griddap" if dim_names else "tabledap"
    row: dict[str, Any] = {
        "server_url": found_server,
        "dataset_id": upstream_id,
        "protocol": protocol,
    }
    if protocol == "griddap":
        row["dim_names"] = dim_names
    row["variables"] = variables
    row["title"] = _erddap_global_attr(rows, "title")
    row["license_note"] = _erddap_global_attr(rows, "license")
    return row


# --------------------------------------------------------------------------- #
# biodiversity cluster — gbif / obis / wdpa / iucn (no live emit).
# --------------------------------------------------------------------------- #
def _emit_gbif(
    catalog: Any, upstream_id: str, *, key: str, **opts: Any
) -> dict[str, Any]:
    """Seed a GBIF `taxa:` row from a backbone `taxonKey` (no network).

    The `Taxon` row carries `taxon_key` / `title` / `rank`; the maintainer
    passes the backbone key as `upstream_id` and the rank / title as opts.

    Args:
        catalog: The loaded GBIF `Catalog` (unused).
        upstream_id: The GBIF backbone `taxonKey` (digit string or integer).
        key: The friendly catalog key.
        **opts: `title`, `rank`.

    Returns:
        The seeded row.
    """
    return {
        "taxon_key": int(upstream_id),
        "title": str(opts.get("title") or key.replace("-", " ").title()),
        "rank": str(opts.get("rank") or ""),
    }


def _emit_obis(
    catalog: Any, upstream_id: str, *, key: str, **opts: Any
) -> dict[str, Any]:
    """Seed an OBIS `species:` row from a scientific name (no network).

    Args:
        catalog: The loaded OBIS `Catalog` (unused).
        upstream_id: The OBIS `scientificname` (e.g. `"Mola mola"`).
        key: The friendly catalog key.
        **opts: `title`.

    Returns:
        The seeded row.
    """
    return {
        "scientific_name": upstream_id,
        "title": str(opts.get("title") or key.replace("-", " ").title()),
    }


def _emit_wdpa(
    catalog: Any, upstream_id: str, *, key: str, **opts: Any
) -> dict[str, Any]:
    """Seed a WDPA `countries:` row from an ISO3 code (no network).

    Args:
        catalog: The loaded WDPA `Catalog` (unused).
        upstream_id: The ISO3 alpha-3 code (e.g. `"KEN"`).
        key: The friendly catalog key (typically the same alpha-3 code).
        **opts: `name`, `region`.

    Returns:
        The seeded row.
    """
    return {
        "name": str(opts.get("name") or key),
        "region": str(opts.get("region") or ""),
    }


def _emit_iucn(
    catalog: Any, upstream_id: str, *, key: str, **opts: Any
) -> dict[str, Any]:
    """Seed an IUCN `countries:` row from an ISO2 code (no network).

    Args:
        catalog: The loaded IUCN `Catalog` (unused).
        upstream_id: The ISO2 alpha-2 code (e.g. `"KE"`).
        key: The friendly catalog key (typically the same alpha-2 code).
        **opts: `name`, `region`.

    Returns:
        The seeded row.
    """
    return {
        "name": str(opts.get("name") or key),
        "region": str(opts.get("region") or ""),
    }


#: Provider id -> a callable taking the loaded catalog, the upstream id, and
#: per-provider keyword options, returning the seeded curated row.
# ecmwf — seed from the live CADS `form.json` (CDS / ADS / EWDS).

#: The three Copernicus store API roots, by `endpoint` slug.
_ECMWF_STORE_URLS = {
    "cds": "https://cds.climate.copernicus.eu/api",
    "ads": "https://ads.atmosphere.copernicus.eu/api",
    "ewds": "https://ewds.climate.copernicus.eu/api",
}


def _ecmwf_token() -> str | None:
    """Return the shared CDS Personal Access Token from `~/.cdsapirc`, if any."""
    rc = Path.home() / ".cdsapirc"
    if not rc.is_file():
        return None
    for line in rc.read_text().splitlines():
        if line.strip().startswith("key"):
            return line.partition(":")[2].strip() or None
    return None


def _ecmwf_endpoint_for(catalog: Any, dataset_id: str) -> str:
    """Resolve which store hosts `dataset_id` (per-store index, else prefix)."""
    store = getattr(catalog, "store_for", lambda _id: None)(dataset_id)
    if store:
        return str(store)
    if dataset_id.startswith("cams-"):
        return "ads"
    if dataset_id.startswith(("cems-", "efas-")):
        return "ewds"
    return "cds"


def _collect_variable_values(node: Any, out: list[str]) -> None:
    """Recurse a `variable` widget's (possibly grouped) values into `out`."""
    if isinstance(node, dict):
        for value in node.get("values", []) or []:
            if isinstance(value, str):
                out.append(value)
        for group in node.get("groups", []) or []:
            _collect_variable_values(group, out)


def _ecmwf_form_variables(form: list[Any]) -> list[str]:
    """Enumerate the `variable` widget's allowed values from a `form.json`."""
    out: list[str] = []
    for entry in form:
        if isinstance(entry, dict) and entry.get("name") == "variable":
            _collect_variable_values(entry.get("details", {}) or {}, out)
    return out


def _ecmwf_request_kind(form: list[Any], upstream_id: str = "") -> str:
    """Guess the `request_kind` from a dataset id + its `form.json` fields.

    The ESA-CCI satellite CDRs are all `satellite-*` ids and their forms carry
    no `grid` widget, so the id is the reliable signal for `satellite_cdr`;
    every other kind is inferred from the form's date / selector fields.
    """
    if upstream_id.startswith("satellite-"):
        return "satellite_cdr"
    if upstream_id.startswith("cems-fire"):
        # Both fire-danger datasets are `cems-fire-*`; the historical form has a
        # grid but the seasonal form keys on `leadtime_hour`, so the field
        # heuristic alone would seed seasonal fire as `form` — key on the id.
        return "fire"
    fields = {f.get("name") for f in form if isinstance(f, dict)}
    if "hyear" in fields:
        return "glofas_hindcast" if "hday" in fields else "seasonal_hindcast"
    # A real seasonal forecast keys on `leadtime_month`. Without it, a
    # year/month-only form is a monthly reanalysis / emission inventory /
    # radiative-forcing product, not a seasonal forecast — so require the lead
    # field rather than firing on `year` + no `day` (which over-caught ~15
    # non-seasonal families).
    if "leadtime_month" in fields:
        return "seasonal"
    if "date" in fields:
        return "cams_date"
    # `cams_inversion` (CAMS GHG inversion / EU air-quality reanalyses) is
    # CAMS-only: a `quantity` widget, or a `cams-*` id carrying a model/level
    # selector with no `day`. A `model` widget on a `projections-*` id is a
    # climate model, not a CAMS inversion — do not misread it.
    if "quantity" in fields or (
        upstream_id.startswith("cams-") and "model" in fields and "day" not in fields
    ):
        return "cams_inversion"
    if "grid" in fields and "leadtime_hour" not in fields:
        # CEMS fire-danger forms carry a `dataset_type` selector alongside the
        # grid; the satellite CDRs are matched by id above.
        return "fire" if "dataset_type" in fields else "satellite_cdr"
    return "form"


def _emit_ecmwf(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed an ECMWF `datasets:` row from the live CADS `form.json`.

    Resolves the dataset's store (CDS / ADS / EWDS) from the per-store index,
    fetches its `form.json`, guesses the `request_kind` from the date/selector
    fields, and enumerates every variable the `variable` widget exposes.
    `nc_variable` / `units` are placeholders (the form does not carry them) —
    confirm them from a live retrieve (`curate ecmwf --fill-empty`).

    Args:
        catalog: The loaded ECMWF `Catalog` (its per-store index resolves the
            endpoint).
        upstream_id: The Copernicus dataset id.
        **opts: Unused.

    Returns:
        The seeded row (`endpoint` / `request_kind` / `variables`).
    """
    endpoint = _ecmwf_endpoint_for(catalog, upstream_id)
    token = _ecmwf_token()
    headers = {"PRIVATE-TOKEN": token} if token else None
    url = f"{_ECMWF_STORE_URLS[endpoint]}/catalogue/v1/collections/{upstream_id}/form.json"
    raw: Any = _get_json(url, headers=headers)
    # CADS form.json is usually {"form": [...]} but is sometimes the bare
    # [...] list; `raw` is Any so both shapes narrow without a mypy conflict.
    fields = raw.get("form") or [] if isinstance(raw, dict) else raw
    variables = _ecmwf_form_variables(cast("list[Any]", fields)) or ["all"]
    return {
        "endpoint": endpoint,
        "request_kind": _ecmwf_request_kind(cast("list[Any]", fields), upstream_id),
        "variables": {
            v.replace("_", "-"): {
                "cds_variable": v,
                "nc_variable": v,
                "units": "unknown",
            }
            for v in variables
        },
    }


_EMITTERS: dict[str, Callable[..., dict[str, Any]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("emitter"),
    "ecmwf": _emit_ecmwf,
    "earthdata": _emit_earthdata,
    "eumetsat": _emit_eumetsat,
    "gee": _emit_gee,
    "jaxa": _emit_jaxa,
    "erddap": _emit_erddap,
    "gbif": _emit_gbif,
    "obis": _emit_obis,
    "wdpa": _emit_wdpa,
    "iucn": _emit_iucn,
}

#: Provider id -> the YAML block its curated rows live under.
_BIODIVERSITY_BLOCKS = {
    "gbif": "taxa",
    "obis": "species",
    "wdpa": "countries",
    "iucn": "countries",
}


def supported_providers() -> list[str]:
    """Return the provider ids that have a stanza emitter wired up.

    Returns:
        The sorted provider ids `curate` can author a row for.

    Examples:
        - Earthdata is wired up:

            ```python
            >>> from earthlens.cli.stanza import supported_providers
            >>> "earthdata" in supported_providers()
            True

            ```
    """
    return sorted(_EMITTERS)


def emit_stanza(
    info: BackendInfo,
    upstream_id: str,
    *,
    key: str | None = None,
    minimal: bool = False,
    **opts: Any,
) -> StanzaResult:
    """Author a curated `datasets:` row for one upstream id.

    A provider with no emitter returns `"unsupported"`; any fetch / parse
    failure returns `"error"` — neither raises.

    Args:
        info: The backend the dataset belongs to.
        upstream_id: The upstream id to seed the row from.
        key: The friendly catalog key (defaults to `upstream_id`).
        minimal: Emit a placeholder row without a live fetch where the
            emitter supports it (e.g. GEE's empty-bands stanza).
        **opts: Per-provider options (e.g. earthdata `version` /
            `cmr_provider`, usgs `name` / `units` / `services`).

    Returns:
        The :class:`StanzaResult`.
    """
    resolved_key = key or upstream_id
    emitter = _EMITTERS.get(info.provider)
    if emitter is None:
        return StanzaResult(
            provider=info.provider,
            key=resolved_key,
            upstream_id=upstream_id,
            status="unsupported",
            detail="no stanza emitter wired up for this provider",
        )
    try:
        catalog = load_catalog(info)
        row = emitter(catalog, upstream_id, key=resolved_key, minimal=minimal, **opts)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return StanzaResult(
            provider=info.provider,
            key=resolved_key,
            upstream_id=upstream_id,
            status="error",
            detail=str(exc),
        )
    return StanzaResult(
        provider=info.provider,
        key=resolved_key,
        upstream_id=upstream_id,
        status="ok",
        row=row,
    )


#: Provider id -> the YAML block its curated rows live under.
_STANZA_BLOCK: dict[str, str] = {
    # Discovered config first; in-core literals are the migration remainder.
    **config_table("stanza_block"),
    **_BIODIVERSITY_BLOCKS,
}


def _append_to_block(path: Path, block: str, key: str, row: dict[str, Any]) -> None:
    """Append a `{key: row}` entry under `block:` in `path`, preserving the rest.

    Splices the new entry in at the end of the existing `block:` block (or
    appends a fresh `block:` at end of file), leaving every other line —
    header comments, sibling blocks, the other rows — byte-for-byte intact.

    Args:
        path: The catalog YAML file to edit.
        block: The top-level block the row belongs under (`datasets` /
            `parameters`).
        key: The friendly catalog key for the new row.
        row: The seeded row fields.

    Raises:
        ValueError: If `key` is already curated in `path`.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    parsed = (yaml.safe_load(text) or {}) if text.strip() else {}
    if key in (parsed.get(block) or {}):
        raise ValueError(f"{key!r} is already curated in {path.name}")
    dumped = yaml.safe_dump({key: row}, sort_keys=False, allow_unicode=True)
    entry = "".join(
        ("  " + line if line.strip() else line)
        for line in dumped.splitlines(keepends=True)
    )
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"{block}:")), None
    )
    if start is None:
        prefix = text if not text or text.endswith("\n") else text + "\n"
        path.write_text(f"{prefix}{block}:\n{entry}", encoding="utf-8")
        return
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*:", lines[j]):
            end = j
            break
    lines.insert(end, entry)
    path.write_text("".join(lines), encoding="utf-8")


def write_stanza(info: BackendInfo, result: StanzaResult, target: str | None) -> str:
    """Insert a seeded row into the curated catalog file (the `--write` half).

    Appends `result.row` under the provider's block (`parameters:` for
    usgs_water, else `datasets:`). Sharded catalogs need a `target` file
    stem (the per-family file under `catalog/`); single-file catalogs
    ignore it.

    Args:
        info: The backend the row belongs to.
        result: The `ok` :class:`StanzaResult` to persist.
        target: The per-family file stem (sharded catalogs only).

    Returns:
        The path of the catalog file written.

    Raises:
        ValueError: If a sharded catalog is missing `target`, or the key is
            already curated.
    """
    base = importlib.import_module(f"{info.module}.catalog").CATALOG_PATH
    block = _STANZA_BLOCK.get(info.provider, "datasets")
    if base.is_dir():
        if not target and info.provider == "gee":
            target = categorise_asset(
                result.upstream_id, str(result.row.get("title", ""))
            )
        if not target and info.provider == "ecmwf":
            from earthlens.cli._ecmwf_categories import categorise_dataset

            target = categorise_dataset(result.upstream_id)
        if not target:
            raise ValueError(
                f"{info.provider} has a sharded catalog; pass --target <file-stem> "
                "(the per-family file under catalog/) to write the row"
            )
        path = base / f"{target}.yaml"
    else:
        path = base
    _append_to_block(path, block, result.key, result.row)
    return str(path)
