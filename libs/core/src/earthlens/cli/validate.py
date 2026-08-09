"""Per-entry catalog validation — the verb for curated-enumeration providers.

Several providers (`nwp`, `s3`, `firms`, `radar`, `tropycal`, …) are
hand-curated selections with no discoverable upstream index, so the
index-diff `refresh` / `audit` verbs do not apply. What *does* apply is
checking each curated entry individually: is it internally coherent
(offline structural lint), or does it still resolve upstream (liveness)?
That is what `validate` does. This is the CLI home for the
`tools/*/audit_*` checks that are per-entry rather than index-diff.

Each provider plugs a validator into :data:`_VALIDATORS` returning
`(checked, issues)`; providers without one report `"unsupported"`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

from earthlens.cli.adapter import BackendInfo, load_catalog
from earthlens.cli.refresh import _TIMEOUT, _get_json


@dataclass
class ValidateResult:
    """The result of validating one provider's curated entries.

    Attributes:
        provider: Canonical provider id.
        status: `"ok"`, `"unsupported"` (no validator), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        checked: Number of curated entries inspected.
        issues: One message per entry that failed validation (empty = clean).
    """

    provider: str
    status: str
    detail: str = ""
    checked: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Project the result to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - A clean provider has an empty `issues` list:

                ```python
                >>> from earthlens.cli.validate import ValidateResult
                >>> ValidateResult("nwp", "ok", checked=32).to_dict()["issues"]
                []

                ```
        """
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "checked": self.checked,
            "issues": self.issues,
        }


def _validate_nwp(catalog: Any) -> tuple[int, list[str]]:
    """Offline structural lint of the curated NWP models.

    Mirrors `tools/nwp/audit_nwp_catalog.py`: flags `direct-https` models
    with no `url_template`, `herbie` models with no `model_family`, empty
    band maps, and cycle hours outside 0-23.

    Args:
        catalog: The loaded NWP `Catalog`.

    Returns:
        `(checked, issues)` — the model count and one message per problem.
    """
    issues: list[str] = []
    # Backends whose fetcher reads `model.url_template` directly. Adding
    # a new direct-fetch backend means adding it here so a missing
    # `url_template` is caught at lint time, not at fetch time.
    _DIRECT_URL_BACKENDS = ("direct-https", "eccc-msc")
    models = catalog.datasets
    for key, record in models.items():
        backend = getattr(record, "backend", None)
        if backend in _DIRECT_URL_BACKENDS and not getattr(
            record, "url_template", None
        ):
            issues.append(f"{key}: {backend} model has no url_template")
        if backend == "herbie" and not getattr(record, "model_family", None):
            issues.append(f"{key}: herbie model has no model_family")
        if not (getattr(record, "bands", None) or {}):
            issues.append(f"{key}: empty band map")
        for hour in getattr(record, "cycles_utc", None) or []:
            if isinstance(hour, int) and not 0 <= hour <= 23:
                issues.append(f"{key}: cycle hour {hour} out of range")
    return len(models), issues


def _lint(
    catalog: Any, check: Callable[[str, Any], list[str]]
) -> tuple[int, list[str]]:
    """Run a per-record `check(key, record) -> [issue, …]` over a catalog."""
    issues: list[str] = []
    for key, record in catalog.datasets.items():
        issues.extend(check(key, record))
    return len(catalog.datasets), issues


def _require(key: str, record: Any, fields: tuple[str, ...]) -> list[str]:
    """Return an issue per `field` that is empty/None on `record`."""
    return [
        f"{key}: missing {field}"
        for field in fields
        if not getattr(record, field, None)
    ]


def _validate_s3(catalog: Any) -> tuple[int, list[str]]:
    """Each S3 dataset needs a bucket and a format."""
    return _lint(catalog, lambda k, r: _require(k, r, ("bucket", "format")))


def _validate_ghsl(catalog: Any) -> tuple[int, list[str]]:
    """Each GHSL product needs a code and at least one release.

    `family` is a soft grouping that is legitimately empty for top-level
    products (e.g. GHS_POP is its own family), so it is not required.
    """
    return _lint(catalog, lambda k, r: _require(k, r, ("code", "releases")))


def _validate_overture(catalog: Any) -> tuple[int, list[str]]:
    """Each Overture theme needs types and a default_type drawn from them."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a theme missing types/default_type or whose default is unlisted."""
        issues = _require(key, record, ("types", "default_type"))
        types = getattr(record, "types", None) or []
        default = getattr(record, "default_type", None)
        if default and types and default not in types:
            issues.append(f"{key}: default_type {default!r} not in types")
        return issues

    return _lint(catalog, check)


def _validate_osm(catalog: Any) -> tuple[int, list[str]]:
    """Each OSM named query needs a protocol and that protocol's query field."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a query missing protocol/geometry, or its protocol's query field."""
        issues = _require(key, record, ("protocol", "geometry_types"))
        protocol = getattr(record, "protocol", None)
        if protocol == "overpass" and not getattr(record, "query_template", None):
            issues.append(f"{key}: overpass row missing query_template")
        if protocol == "ohsome" and not getattr(record, "ohsome_filter", None):
            issues.append(f"{key}: ohsome row missing ohsome_filter")
        if protocol == "pbf" and not getattr(record, "pyrosm_method", None):
            issues.append(f"{key}: pbf row missing pyrosm_method")
        return issues

    return _lint(catalog, check)


def _validate_fdsn(catalog: Any) -> tuple[int, list[str]]:
    """Each FDSN network needs an fdsn_id."""
    return _lint(catalog, lambda k, r: _require(k, r, ("fdsn_id",)))


def _validate_firms(catalog: Any) -> tuple[int, list[str]]:
    """Each FIRMS sensor needs a code and a non-empty columns map."""
    return _lint(catalog, lambda k, r: _require(k, r, ("code", "columns")))


def _validate_asf(catalog: Any) -> tuple[int, list[str]]:
    """Every ASF row's PLATFORM/DATASET/PRODUCT_TYPE must exist in `asf_search`.

    The catalog is hand-curated against the SDK's enum modules; if a
    constant is renamed or removed upstream, the row would silently
    break only at first live query. This validator imports the SDK and
    checks every row's `platform` / `dataset` / `product_type` member
    name still exists on the matching module.

    Args:
        catalog: The loaded ASF :class:`earthlens.asf.Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per
            row whose constants no longer resolve.
    """
    try:
        import asf_search as asf
    except ImportError:
        # Report zero checked rows — nothing was inspected — and surface
        # the install hint as the issue so JSON consumers can distinguish
        # "ran clean" from "skipped because the SDK was missing".
        return 0, [
            f"asf_search is not installed; install the `asf` extra to "
            f"validate {len(catalog.datasets)} curated row(s)"
        ]
    issues: list[str] = []
    products = catalog.datasets
    for key, row in products.items():
        if row.platform is not None and not hasattr(asf.PLATFORM, row.platform):
            issues.append(f"{key}: PLATFORM.{row.platform} not in asf_search")
        if row.dataset is not None and not hasattr(asf.DATASET, row.dataset):
            issues.append(f"{key}: DATASET.{row.dataset} not in asf_search")
        if not hasattr(asf.PRODUCT_TYPE, row.product_type):
            issues.append(f"{key}: PRODUCT_TYPE.{row.product_type} not in asf_search")
    return len(products), issues


def _validate_radar(catalog: Any) -> tuple[int, list[str]]:
    """Each radar station needs a name and in-range latitude / longitude."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a station missing a name or with out-of-range coordinates."""
        issues = _require(key, record, ("name",))
        lat = getattr(record, "latitude", None)
        lon = getattr(record, "longitude", None)
        if not (isinstance(lat, (int, float)) and -90 <= lat <= 90):
            issues.append(f"{key}: latitude {lat!r} out of range")
        if not (isinstance(lon, (int, float)) and -180 <= lon <= 180):
            issues.append(f"{key}: longitude {lon!r} out of range")
        return issues

    return _lint(catalog, check)


#: tropycal's basin universe and which sources serve each (no `jtwc` source;
#: `both` is HURDAT NA+EP, `all` is IBTrACS global). Ported from the retired
#: `tools/tropycal/audit_tropycal_catalog.py`.
_SDK_BASIN_SOURCES: dict[str, list[str]] = {
    "north_atlantic": ["ibtracs", "hurdat"],
    "east_pacific": ["ibtracs", "hurdat"],
    "both": ["hurdat"],
    "west_pacific": ["ibtracs"],
    "north_indian": ["ibtracs"],
    "south_indian": ["ibtracs"],
    "australia": ["ibtracs"],
    "south_pacific": ["ibtracs"],
    "south_atlantic": ["ibtracs"],
    "all": ["ibtracs"],
}


def _validate_tropycal(catalog: Any) -> tuple[int, list[str]]:
    """Each Tropycal basin needs a source, and must match the SDK universe.

    Beyond the per-row `sources` requirement, this diffs the catalog against
    tropycal's supported basin/source universe (the offline check the retired
    `audit_tropycal_catalog.py` ran): a curated basin tropycal no longer
    serves, a tropycal basin missing from the catalog, or a declared
    `(basin, source)` pair tropycal does not support.

    Args:
        catalog: The loaded Tropycal `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    checked, issues = _lint(catalog, lambda k, r: _require(k, r, ("sources",)))
    catalog_basins = set(catalog.datasets)
    sdk_basins = set(_SDK_BASIN_SOURCES)
    issues += [
        f"{code}: basin not in tropycal's supported universe"
        for code in sorted(catalog_basins - sdk_basins)
    ]
    issues += [
        f"{code}: tropycal basin missing from the catalog"
        for code in sorted(sdk_basins - catalog_basins)
    ]
    for code in sorted(catalog_basins & sdk_basins):
        declared = set(getattr(catalog.datasets[code], "sources", None) or [])
        for bad in sorted(declared - set(_SDK_BASIN_SOURCES[code])):
            issues.append(f"{code}: source {bad!r} not supported by tropycal")
    return checked, issues


def _validate_emdat(catalog: Any) -> tuple[int, list[str]]:
    """Each EM-DAT dataset needs the prose its row model does not already force.

    `long_name` and `licence` are required by the pydantic row model, and
    `hazard_vocabulary` has a default the loader then checks against the
    `hazard_vocabularies:` block — so a catalog missing any of them never loads
    far enough to reach validation. `description` and `citation` are the
    genuinely optional fields worth insisting on.
    """
    return _lint(catalog, lambda k, r: _require(k, r, ("description", "citation")))


def _validate_gdacs(catalog: Any) -> tuple[int, list[str]]:
    """Each GDACS hazard type needs a name and a description."""
    return _lint(catalog, lambda k, r: _require(k, r, ("name", "description")))


def _validate_nsi(catalog: Any) -> tuple[int, list[str]]:
    """Each NSI source needs a provider, endpoint, output kind, and field map."""
    return _lint(
        catalog,
        lambda k, r: _require(k, r, ("provider", "endpoint", "output_kind", "fields")),
    )


#: Drought transports whose output is a raster (vs USDM's vector polygons).
_DROUGHT_RASTER_TRANSPORTS = frozenset({"edo-wcs", "netcdf-url"})


def _check_drought_row(key: str, record: Any) -> list[str]:
    """Flag a drought row missing a core field or with a transport mismatch."""
    issues = _require(
        key, record, ("source", "endpoint", "output_kind", "cadence", "native_crs")
    )
    transport = getattr(record, "transport", None)
    output_kind = getattr(record, "output_kind", None)
    if transport == "usdm-geojson" and output_kind != "vector":
        issues.append(f"{key}: usdm-geojson transport must be output_kind=vector")
    if transport in _DROUGHT_RASTER_TRANSPORTS and output_kind != "raster":
        issues.append(f"{key}: {transport} transport must be output_kind=raster")
    if transport == "edo-wcs":
        issues.extend(_require(key, record, ("coverage", "timescale")))
    return issues


def _validate_drought(catalog: Any) -> tuple[int, list[str]]:
    """Each drought row needs its core fields; edo-wcs rows a coverage + timescale."""
    return _lint(catalog, _check_drought_row)


def _validate_argo(catalog: Any) -> tuple[int, list[str]]:
    """Each Argo dataset family needs a description and a non-empty parameters map."""
    return _lint(catalog, lambda k, r: _require(k, r, ("description", "parameters")))


def _validate_chc(catalog: Any) -> tuple[int, list[str]]:
    """Each CHC dataset needs FTP bases, a file pattern, and variables."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a dataset missing ftp_bases, variables, or a file pattern."""
        issues = _require(key, record, ("ftp_bases", "variables"))
        if not (
            getattr(record, "file_patterns", None)
            or getattr(record, "discrete_files", None)
        ):
            issues.append(f"{key}: no file_patterns or discrete_files")
        return issues

    return _lint(catalog, check)


def _validate_usgs_water(catalog: Any) -> tuple[int, list[str]]:
    """Each USGS Water parameter's `services` must be known service names.

    Mirrors `tools/usgs_water/refresh_usgs_catalog.py:validate`: every
    declared service must be in `earthlens.usgs_water.backend.SERVICES`.

    Args:
        catalog: The loaded USGS Water `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    from earthlens.usgs_water.backend import SERVICES

    def check(key: str, record: Any) -> list[str]:
        """Flag a parameter declaring a service that is not a known service name."""
        return [
            f"{key}: unknown service {service!r}"
            for service in getattr(record, "services", None) or []
            if service not in SERVICES
        ]

    return _lint(catalog, check)


def _validate_sentinel_hub(catalog: Any) -> tuple[int, list[str]]:
    """Each Sentinel Hub recipe's evalscript `.js` must be well-formed.

    Mirrors `tools/sentinel_hub/refresh_sh_catalog.py:validate-recipe`
    (offline): the bundled `.js` must exist, start with `//VERSION=3`, and
    a `"stats"` recipe must declare a `dataMask` band.

    Args:
        catalog: The loaded Sentinel Hub `Catalog` (exposes `recipes`).

    Returns:
        `(checked, issues)` — the recipe count and one message per problem.
    """
    from earthlens.sentinel_hub import read_evalscript

    recipes = getattr(catalog, "recipes", None) or {}
    issues: list[str] = []
    for key, recipe in recipes.items():
        script_name = getattr(recipe, "evalscript", None)
        if not script_name:
            continue
        try:
            script = read_evalscript(script_name)
        except FileNotFoundError as exc:
            issues.append(f"{key}: {exc}")
            continue
        if script.splitlines()[0].strip() != "//VERSION=3":
            issues.append(f"{key}: {script_name} does not start with //VERSION=3")
        if getattr(recipe, "kind", None) == "stats" and "dataMask" not in script:
            issues.append(f"{key}: {script_name} stats recipe has no dataMask band")
    return len(recipes), issues


def _validate_worldpop(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated WorldPop products.

    Reuses the backend's own `Catalog.health()` (mirrors
    `tools/worldpop/refresh_worldpop_catalog.py:validate_structure`):
    flags products with no sub-aliases, `demographic` products whose `kind`
    is not `"mixed"`, and sub-aliases with an unknown generation or an
    unparseable years spec.

    Args:
        catalog: The loaded WorldPop `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per offender.
    """
    issues = [
        f"{offender}: {check}"
        for check, offenders in catalog.health().items()
        for offender in offenders
    ]
    return len(catalog.datasets), issues


def _validate_nwm(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated NWM products and configurations.

    Mirrors the offline half of the retired `tools/nwm/audit_nwm_catalog.py`:
    every product needs an `s3_token` and a non-empty `variables` map, and
    every configuration's `products` must reference a curated product key.

    Args:
        catalog: The loaded NWM `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per problem.
    """
    products = catalog.datasets
    issues: list[str] = []
    for key, product in products.items():
        issues.extend(_require(key, product, ("s3_token", "variables")))
    for cfg_key, config in catalog.configurations.items():
        for product_key in getattr(config, "products", None) or []:
            if product_key not in products:
                issues.append(f"{cfg_key}: references unknown product {product_key!r}")
    return len(products), issues


def _validate_goes(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated GOES ABI products.

    Every product needs a `product_group` and a non-empty `domains` list
    whose entries are all known domain keys, a `default_domain` drawn from
    that list, and — for a band-split product — a non-empty `bands` list.

    Args:
        catalog: The loaded GOES `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per problem.
    """
    known_domains = set(catalog.domains)
    issues: list[str] = []
    for key, product in catalog.datasets.items():
        issues.extend(_require(key, product, ("product_group", "domains")))
        domains = getattr(product, "domains", None) or []
        unknown = [d for d in domains if d not in known_domains]
        if unknown:
            issues.append(f"{key}: unknown domain(s) {unknown}")
        default = getattr(product, "default_domain", None)
        if domains and default not in domains:
            issues.append(f"{key}: default_domain {default!r} not in domains {domains}")
        if getattr(product, "band_split", False) and not getattr(
            product, "bands", None
        ):
            issues.append(f"{key}: band_split product needs a non-empty bands list")
    return len(catalog.datasets), issues


def _check_gbif_taxon(key: str, record: Any) -> list[str]:
    """Flag a GBIF taxon missing its key or carrying a non-positive one."""
    issues = _require(key, record, ("taxon_key",))
    taxon_key = getattr(record, "taxon_key", None)
    if taxon_key is not None and taxon_key <= 0:
        issues.append(f"{key}: taxon_key must be positive, got {taxon_key!r}")
    return issues


def _validate_gbif(catalog: Any) -> tuple[int, list[str]]:
    """Each curated GBIF taxon needs a positive integer backbone taxonKey."""
    return _lint(catalog, _check_gbif_taxon)


def _validate_obis(catalog: Any) -> tuple[int, list[str]]:
    """Each curated OBIS species needs a scientific name."""
    return _lint(catalog, lambda k, r: _require(k, r, ("scientific_name",)))


def _check_wdpa_country(key: str, record: Any) -> list[str]:
    """Flag a WDPA country missing a name or with a malformed ISO3 key."""
    issues = _require(key, record, ("name",))
    if not (len(key) == 3 and key.isalpha() and key.isupper()):
        issues.append(f"{key}: catalog key must be an upper-case ISO3 alpha-3 code")
    return issues


def _validate_wdpa(catalog: Any) -> tuple[int, list[str]]:
    """Each curated WDPA country needs a name and an ISO3 alpha-3 key."""
    return _lint(catalog, _check_wdpa_country)


def _check_iucn_country(key: str, record: Any) -> list[str]:
    """Flag an IUCN country missing a name or with a malformed ISO2 key."""
    issues = _require(key, record, ("name",))
    if not (len(key) == 2 and key.isalpha() and key.isupper()):
        issues.append(f"{key}: catalog key must be an upper-case ISO2 alpha-2 code")
    return issues


def _validate_iucn(catalog: Any) -> tuple[int, list[str]]:
    """Each curated IUCN country needs a name and an ISO2 alpha-2 key."""
    return _lint(catalog, _check_iucn_country)


def _validate_jaxa(catalog: Any) -> tuple[int, list[str]]:
    """Validate each JAXA row's protocol-specific identifier.

    The `Dataset` pydantic model already enforces the cross-field
    invariant (`jaxa-earth` needs `collection`, `gportal` needs
    `short_name`), so a clean catalog load reaches this validator with
    every row well-formed. This lint adds two cheap cross-row checks the
    model can't see:

    * a `jaxa-earth` row without a `default_band` is flagged — the
      backend's branch raises a hard error at fetch time, so catching
      it offline is friendlier;
    * any curated `short_name` / `collection` that doesn't appear in
      the YAML's `available_datasets:` index is flagged — that index is
      rewritten by `earthlens datasets refresh jaxa --write`, so drift
      surfaces here without a network round-trip.
    """
    available = set(catalog.available_datasets or ())
    issues: list[str] = []
    for key, row in catalog.datasets.items():
        if row.protocol == "jaxa-earth":
            if not row.default_band:
                issues.append(
                    f"{key}: jaxa-earth row missing `default_band` (the branch "
                    "will reject fetches without an explicit bands= override)"
                )
            if available and row.collection not in available:
                issues.append(
                    f"{key}: collection {row.collection!r} not in the bundled "
                    "`available_datasets:` index — refresh may have drifted"
                )
        else:
            if available and row.short_name not in available:
                issues.append(
                    f"{key}: short_name {row.short_name!r} not in the bundled "
                    "`available_datasets:` index — refresh may have drifted"
                )
    return len(catalog.datasets), issues


def _validate_erddap(catalog: Any) -> tuple[int, list[str]]:
    """Lint each ERDDAP row beyond the model's load-time checks.

    The `Dataset` model already enforces `server_url` / `dataset_id` /
    `protocol` at load (`extra="forbid"`, `protocol` is a `Literal`), and
    the loader rejects a curated key absent from `available_datasets:`.
    These cross-row lints add what the model can't see:

    * a server_url that is not an `http(s)` URL (the griddap path builds a
      URL from it and the tabledap path hands it to erddapy);
    * a griddap row with empty `dim_names` — `build_griddap_url` would have
      no axes to subset, producing a malformed request;
    * a `flux_variables` entry that is not one of the row's default
      `variables` — a likely typo, since the flux marker would then never
      apply to the row's default request.
    """
    issues: list[str] = []
    for key, row in catalog.datasets.items():
        if not row.server_url.startswith(("http://", "https://")):
            issues.append(f"{key}: server_url {row.server_url!r} is not an http(s) URL")
        if row.protocol == "griddap" and not row.dim_names:
            issues.append(
                f"{key}: griddap row has empty `dim_names` (no axes to subset)"
            )
        unknown_flux = [v for v in row.flux_variables if v not in row.variables]
        if unknown_flux:
            issues.append(
                f"{key}: flux_variables {unknown_flux} not in the row's default "
                f"variables {row.variables} (likely a typo)"
            )
    return len(catalog.datasets), issues


def _validate_bathymetry(catalog: Any) -> tuple[int, list[str]]:
    """Each bathymetry DEM row needs an endpoint, coverage id, and band.

    The `Dataset` model already enforces those required fields, so a clean
    load reaches this validator well-formed; the lint additionally flags any
    curated id missing from the bundled `available_datasets:` index (the
    `_index.yaml`), which a hand-edit could desync.
    """
    available = set(catalog.available_datasets or ())
    issues: list[str] = []
    for key, row in catalog.datasets.items():
        issues.extend(_require(key, row, ("endpoint", "dataset_id", "variable")))
        if available and key not in available:
            issues.append(f"{key}: id not in the bundled `available_datasets:` index")
    return len(catalog.datasets), issues


def _validate_pvgis(catalog: Any) -> tuple[int, list[str]]:
    """Each PVGIS product needs a tool, an endpoint, and non-empty columns."""
    return _lint(catalog, lambda k, r: _require(k, r, ("tool", "endpoint", "columns")))


def _validate_nrel(catalog: Any) -> tuple[int, list[str]]:
    """Each NREL product needs a source, a CSV endpoint, and non-empty columns."""
    return _lint(
        catalog, lambda k, r: _require(k, r, ("source", "endpoint", "columns"))
    )


def _glaciers_row_issues(key: str, record: Any) -> list[str]:
    """Lint one glaciers row: common fields + per-source request detail.

    Args:
        key: The dataset id.
        record: The `earthlens.glaciers.Dataset` row.

    Returns:
        One issue string per missing field — the common `source` /
        `output_kind` / `long_name` / `citation`, plus `table` / `archive_url`
        for a `wgms` row and `wfs_url` / `wfs_typename` for a `glims` row.
    """
    issues = _require(key, record, ("source", "output_kind", "long_name", "citation"))
    source = getattr(record, "source", None)
    if source == "wgms":
        issues += _require(key, record, ("table", "archive_url"))
    elif source == "glims":
        issues += _require(key, record, ("wfs_url", "wfs_typename"))
    return issues


def _validate_glaciers(catalog: Any) -> tuple[int, list[str]]:
    """Each glaciers row needs a source + output kind + the per-source detail."""
    return _lint(catalog, _glaciers_row_issues)


def _soilgrids_row_issues(key: str, record: Any) -> list[str]:
    """Lint one soilgrids property: WCS endpoint, depths, quantiles + `mean`.

    Args:
        key: The property id.
        record: The `earthlens.soilgrids.Property` row.

    Returns:
        One issue string per problem — a missing `endpoint` / `depths` /
        `quantiles`, an endpoint that is not an ISRIC WCS URL, or a
        `quantiles` list that omits the default `mean` layer.
    """
    issues = _require(key, record, ("endpoint", "depths", "quantiles"))
    endpoint = getattr(record, "endpoint", "") or ""
    # Compare the parsed host exactly, not a substring — a substring check would
    # accept a spoofed host like `maps.isric.org.example.com`.
    if endpoint and urlsplit(endpoint).hostname != "maps.isric.org":
        issues.append(f"{key}: endpoint host is not maps.isric.org")
    quantiles = getattr(record, "quantiles", None) or []
    if quantiles and "mean" not in quantiles:
        issues.append(f"{key}: quantiles missing the default 'mean' layer")
    return issues


def _validate_soilgrids(catalog: Any) -> tuple[int, list[str]]:
    """Each soilgrids property needs a WCS endpoint, depths, and quantiles."""
    return _lint(catalog, _soilgrids_row_issues)


def _validate_mswep(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated MSWEP / MSWX products.

    Each product needs an analysis `path_template`, a `default_version` that
    is registered, and non-empty `versions` / `variants` / `resolutions` /
    `variables` blocks. A product with forecast variants (MSWX's `Mid` /
    `Long`) must also declare a `forecast_path_template`.

    Args:
        catalog: The loaded MSWEP `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per problem.
    """
    products = catalog.datasets
    issues: list[str] = []
    for key, product in products.items():
        issues.extend(_require(key, product, ("path_template", "default_version")))
        for block in ("versions", "variants", "resolutions", "variables"):
            if not getattr(product, block, None):
                issues.append(f"{key}: empty {block}")
        versions = getattr(product, "versions", None) or {}
        default = getattr(product, "default_version", None)
        if default and default not in versions:
            issues.append(f"{key}: default_version {default!r} not in versions")
        variants = getattr(product, "variants", None) or {}
        has_forecast = any(getattr(v, "is_forecast", False) for v in variants.values())
        if has_forecast and not getattr(product, "forecast_path_template", ""):
            issues.append(f"{key}: has forecast variants but no forecast_path_template")
    return len(products), issues


def _caravan_archive_issues(label: str, archive: Any) -> list[str]:
    """Flag one archive file that could not be fetched or verified.

    Args:
        label: `"<extension>/<version>[<format>]"`, for the message.
        archive: One `ArchiveFile` row.

    Returns:
        list[str]: One entry per problem found.
    """
    issues = []
    if not archive.record:
        issues.append(f"{label}: no pinned Zenodo record")
    if not archive.md5:
        issues.append(f"{label}: no md5 to verify the download")
    if archive.size <= 0:
        issues.append(f"{label}: size must be positive")
    if archive.archive_format not in {"zip", "tar.gz"}:
        issues.append(f"{label}: unreadable archive_format {archive.archive_format!r}")
    return issues


def _caravan_release_issues(
    label: str, release: Any, column_sets: set[str]
) -> list[str]:
    """Flag one release whose declared shape is self-inconsistent.

    Args:
        label: `"<extension>/<version>"`, for the message.
        release: One `Version` row.
        column_sets: The column-set names the backend knows how to read.

    Returns:
        list[str]: One entry per problem found.
    """
    issues = []
    if release.column_set not in column_sets:
        issues.append(f"{label}: unknown column_set {release.column_set!r}")
    if not release.files:
        issues.append(f"{label}: no archive files declared")
    for fmt, archive in release.files.items():
        issues.extend(_caravan_archive_issues(f"{label}[{fmt}]", archive))
    period = release.data_period
    if period is not None and period[0] > period[1]:
        issues.append(f"{label}: data_period {period} is inverted")
    return issues


def _caravan_row_issues(key: str, record: Any, column_sets: set[str]) -> list[str]:
    """Flag one extension whose releases are unpinned or self-inconsistent.

    Args:
        key: The extension key.
        record: One `Extension` row.
        column_sets: The column-set names the backend knows how to read.

    Returns:
        list[str]: One entry per problem found.
    """
    versions = getattr(record, "versions", None) or {}
    if not versions:
        return [f"{key}: no versions declared"]
    issues = []
    if getattr(record, "default_version", "") not in versions:
        issues.append(
            f"{key}: default_version {record.default_version!r} is not among "
            f"{sorted(versions)}"
        )
    if not getattr(record, "license", ""):
        issues.append(f"{key}: no license recorded")
    if not getattr(record, "sources", None):
        issues.append(f"{key}: no source datasets recorded")
    for name, release in versions.items():
        issues.extend(_caravan_release_issues(f"{key}/{name}", release, column_sets))
    return issues


def _validate_caravan(catalog: Any) -> tuple[int, list[str]]:
    """Each Caravan extension must pin a fetchable, self-consistent release.

    The catalog's whole job is reproducibility: a row pinning no record, or
    naming an archive format the fetcher cannot read, or claiming a column set
    that does not exist, fails at request time rather than here.

    Args:
        catalog: The loaded Caravan `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    from earthlens.caravan.catalog import ColumnSet

    column_sets = set(ColumnSet.__args__)  # type: ignore[attr-defined]
    return _lint(catalog, lambda k, r: _caravan_row_issues(k, r, column_sets))


#: Provider id -> a callable taking the loaded catalog and returning
#: `(checked, issues)`. Providers without one report `"unsupported"`.
_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    "caravan": _validate_caravan,
    "nwp": _validate_nwp,
    "nwm": _validate_nwm,
    "mswep": _validate_mswep,
    "goes": _validate_goes,
    "s3": _validate_s3,
    "ghsl": _validate_ghsl,
    "overture": _validate_overture,
    "osm": _validate_osm,
    "fdsn": _validate_fdsn,
    "firms": _validate_firms,
    "asf": _validate_asf,
    "radar": _validate_radar,
    "tropycal": _validate_tropycal,
    "gdacs": _validate_gdacs,
    "nsi": _validate_nsi,
    "drought": _validate_drought,
    "argo": _validate_argo,
    "chc": _validate_chc,
    "usgs_water": _validate_usgs_water,
    "sentinel_hub": _validate_sentinel_hub,
    "worldpop": _validate_worldpop,
    "gbif": _validate_gbif,
    "obis": _validate_obis,
    "wdpa": _validate_wdpa,
    "iucn": _validate_iucn,
    "jaxa": _validate_jaxa,
    "emdat": _validate_emdat,
    "erddap": _validate_erddap,
    "bathymetry": _validate_bathymetry,
    "pvgis": _validate_pvgis,
    "nrel": _validate_nrel,
    "glaciers": _validate_glaciers,
    "soilgrids": _validate_soilgrids,
}


# --------------------------------------------------------------------------- #
# Live reachability validators (the `--live` half) — confirm a curated entry
# still resolves upstream. A superset of the offline lint; opt-in because it
# goes to the network / SDK. Each live source sits behind a mockable helper.
# --------------------------------------------------------------------------- #
def _s3_live_keys(bucket: str, prefix: str, region: str | None) -> list[str]:
    """Return one object key under `prefix` (unsigned `boto3`)."""
    from earthlens.base.s3 import S3Auth, S3Credentials

    client = S3Auth(S3Credentials(region=region)).client()
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return [item["Key"] for item in response.get("Contents", [])]


def _live_s3(catalog: Any) -> tuple[int, list[str]]:
    """Confirm every S3 dataset's bucket still serves an object (unsigned)."""
    issues: list[str] = []
    for key, record in catalog.datasets.items():
        try:
            keys = _s3_live_keys(
                record.bucket,
                getattr(record, "prefix", "") or "",
                getattr(record, "region", None),
            )
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}: bucket error ({exc})")
            continue
        if not keys:
            issues.append(f"{key}: no objects under s3://{record.bucket}")
    return len(catalog.datasets), issues


#: A tiny bbox (Times Square block) for the live Overture fetch.
_OVERTURE_BBOX = (-73.9876, 40.7561, -73.9851, 40.7577)


def _overture_live_sample(overture_type: str) -> tuple[int, bool]:
    """Fetch a tiny bbox; return `(row_count, has_sources_column)`."""
    from overturemaps.core import geodataframe

    frame = geodataframe(overture_type, bbox=_OVERTURE_BBOX)
    return len(frame), "sources" in frame.columns


def _live_overture(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each Overture type resolves live and carries a `sources` column."""
    issues: list[str] = []
    for key, record in catalog.datasets.items():
        overture_type = getattr(record, "default_type", None) or key
        try:
            _rows, has_sources = _overture_live_sample(overture_type)
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}/{overture_type}: fetch failed ({exc})")
            continue
        if not has_sources:
            issues.append(f"{key}/{overture_type}: no 'sources' column")
    return len(catalog.datasets), issues


def _http_head(url: str) -> int:
    """Return the HTTP status of a HEAD request (following redirects)."""
    return requests.head(url, timeout=_TIMEOUT, allow_redirects=True).status_code


def _live_ghsl(catalog: Any) -> tuple[int, list[str]]:
    """HEAD one whole-globe artefact per GHSL product/release.

    Skips releases whose every resolution ships only as tiles (real-tile
    sampling stays in `tools/ghsl/refresh_ghsl_catalog.py`).
    """
    from earthlens.ghsl._helpers import RES_TO_TOKEN, ghsl_url

    issues: list[str] = []
    for code, product in catalog.datasets.items():
        # Tabular products (resolution "table") have no raster artefact URL —
        # their live check is the maintainer table-zip path in tools/ghsl.
        if getattr(product, "kind", "raster") == "tabular":
            continue
        for release, blocks in (getattr(product, "releases", None) or {}).items():
            block = blocks[0]
            whole_globe = [
                r
                for r in block.resolutions
                if r not in block.tiled() and r in RES_TO_TOKEN
            ]
            if not whole_globe:
                continue
            try:
                url = ghsl_url(
                    product.family or code,
                    code,
                    block.epochs[0],
                    release,
                    whole_globe[0],
                    version=block.version,
                    region=block.region,
                    nested=block.nested,
                )
                status = _http_head(url)
            except Exception as exc:  # noqa: BLE001 — reported as drift
                issues.append(f"{code} ({release}): {exc}")
                continue
            if status != 200:
                issues.append(f"{code} ({release}): HTTP {status} for {url}")
    return len(catalog.datasets), issues


#: CDSE openEO processes endpoint (public; pairs with the collections one).
_OPENEO_PROCESSES_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2/processes"


def _openeo_live_lists() -> tuple[set[str], set[str]]:
    """Return the live `(collection_ids, process_ids)` sets (public CDSE)."""
    from earthlens.cli.refresh import _OPENEO_COLLECTIONS_URL

    collections = {
        c["id"]
        for c in _get_json(_OPENEO_COLLECTIONS_URL).get("collections", [])
        if c.get("id")
    }
    processes = {
        p["id"]
        for p in _get_json(_OPENEO_PROCESSES_URL).get("processes", [])
        if p.get("id")
    }
    return collections, processes


def _live_openeo(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each openEO recipe's base collection + processes exist live."""
    recipes = getattr(catalog, "recipes", None) or {}
    if not recipes:
        return 0, []
    collections, processes = _openeo_live_lists()
    issues: list[str] = []
    for key, recipe in recipes.items():
        base = getattr(recipe, "base_collection", None)
        if base and base not in collections:
            issues.append(f"{key}: base_collection {base!r} not served live")
        for process in getattr(recipe, "processes", None) or []:
            if process not in processes:
                issues.append(f"{key}: process {process!r} not served live")
    return len(recipes), issues


def _radar_feed_stations(region: str = "us-east-1") -> set[str]:
    """Return the station ids currently present in the NEXRAD chunk feed.

    Lists the top-level `{STATION}/` prefixes in the unsigned
    `unidata-nexrad-level2-chunks` bucket (the real-time feed
    `earthlens.radar` fetches from). Ported from the retired
    `tools/radar/audit_radar_catalog.py`.

    Args:
        region: AWS region of the bucket.

    Returns:
        The set of station-id prefixes currently in the feed.
    """
    from earthlens.radar.backend import BUCKET, _s3_client

    client = _s3_client(region)
    stations: set[str] = set()
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": BUCKET, "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        stations.update(
            prefix["Prefix"].rstrip("/")
            for prefix in response.get("CommonPrefixes", [])
        )
        token = response.get("NextContinuationToken")
        if not response.get("IsTruncated"):
            break
    return stations


def _live_radar(catalog: Any) -> tuple[int, list[str]]:
    """Confirm the real-time NEXRAD chunk feed is reachable and lines up.

    Flags a hard failure (feed served nothing → unreachable / outage) or an
    id-format mismatch (the feed is non-empty but no catalogued station is in
    it). Per-station idleness is expected — the feed is a rolling ~1-2 h
    buffer — so it is not flagged.
    """
    catalogued = set(catalog.datasets)
    feed = _radar_feed_stations()
    if not feed:
        return len(catalogued), [
            "NEXRAD chunk feed served no stations (unreachable / outage?)"
        ]
    if not (catalogued & feed):
        return len(catalogued), [
            "no catalogued station is in the live feed "
            "(id format may not match the feed prefixes)"
        ]
    return len(catalogued), []


def _nwp_latest_cycle(model: Any, hours_ago: int = 6) -> dt.datetime | None:
    """Return a model's most recent expected run datetime (or None).

    Ported from the retired `tools/nwp/refresh_nwp_catalog.py`.

    Args:
        model: A curated NWP model record (duck-typed: `cycles_utc`).
        hours_ago: How far back to look for the latest published cycle.

    Returns:
        The most recent cycle datetime at or before `now - hours_ago`,
        or None when the model declares no cycle hours.
    """
    if not getattr(model, "cycles_utc", None):
        return None
    moment = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(
        hours=hours_ago
    )
    for day_offset in (0, 1):
        day = moment - dt.timedelta(days=day_offset)
        for hour in sorted(model.cycles_utc, reverse=True):
            candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= moment:
                return candidate
    return None


def _live_nwp(catalog: Any) -> tuple[int, list[str]]:
    """HEAD each direct-https NWP model's latest expected cycle URL.

    Folds the retired `refresh_nwp_catalog.py --live`: only `direct-https`
    models (e.g. DWD ICON) can be checked with a cheap HEAD — Herbie / ECMWF
    models would need their SDKs to resolve a cycle, so they are skipped.
    """
    issues: list[str] = []
    checked = 0
    for key, model in catalog.datasets.items():
        if getattr(model, "backend", None) != "direct-https":
            continue
        cycle = _nwp_latest_cycle(model)
        url_template = getattr(model, "url_template", None)
        bands = getattr(model, "bands", None)
        if cycle is None or not url_template or not bands:
            continue
        checked += 1
        var = next(iter(bands.values()))
        url = url_template.format(
            cycle=cycle, date=cycle, step=0, var=var, var_lc=str(var).lower()
        )
        try:
            status = _http_head(url)
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}: latest cycle unreachable ({type(exc).__name__})")
            continue
        if status != 200:
            issues.append(f"{key}: HTTP {status} for latest cycle {url}")
    return checked, issues


def _live_ecmwf(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each ECMWF dataset can build a constraint-valid minimal request.

    Folds the local gate of the retired `tools/ecmwf/probe_open_datasets.py`:
    for every curated dataset, build a minimal request from its public
    `constraints.json` and run the same `RequestValidator` the backend uses
    before a retrieve. Datasets that publish no constraints (so no request can
    be built) are skipped, not flagged. Stateless — no CDS credentials or
    queue submission (per-dataset live retrieval stays `probe ecmwf --deep`).
    """
    from earthlens.ecmwf.constraints import RequestValidator

    issues: list[str] = []
    checked = 0
    for key in catalog.datasets:
        try:
            request = catalog.minimal_valid_request(key)
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}: constraints fetch failed ({exc})")
            continue
        if set(request) <= {"data_format"}:
            continue  # no published constraints -> nothing to validate
        checked += 1
        try:
            RequestValidator(key, request).check()
        except ValueError as exc:
            issues.append(f"{key}: {str(exc).splitlines()[0][:90]}")
    return checked, issues


#: First-page object cap when sampling a configuration directory's tokens.
_NWM_SAMPLE_KEYS = 400


def _nwm_sample_tokens(client: Any, day: str, directory: str) -> set[str]:
    """Return the distinct product `{output}` tokens under one config directory.

    Samples the first :data:`_NWM_SAMPLE_KEYS` objects of
    `{day}/{directory}/` on `noaa-nwm-pds` and parses each file's `{output}`
    token from the `nwm.tHHz.<family>.<output>.<step>.<domain>.nc` layout.
    For an ensemble directory the token carries the member suffix
    (`channel_rt_1`); :func:`_nwm_token_present` handles that.

    Args:
        client: An unsigned S3 client (see `refresh._nwm_unsigned_client`).
        day: An `nwm.YYYYMMDD` prefix.
        directory: A configuration directory name under `day`.

    Returns:
        The set of product `{output}` tokens seen in the sample.
    """
    from earthlens.nwm import BUCKET

    sample = client.list_objects_v2(
        Bucket=BUCKET, Prefix=f"{day}/{directory}/", MaxKeys=_NWM_SAMPLE_KEYS
    )
    tokens: set[str] = set()
    for entry in sample.get("Contents", []):
        parts = entry["Key"].split("/")[-1].split(".")
        if len(parts) >= 6:
            tokens.add(parts[3])
    return tokens


def _nwm_token_present(s3_token: str, tokens: set[str]) -> bool:
    """Whether a product's bare `s3_token` shows among sampled file tokens.

    Matches the bare token (a deterministic carrier's file token *is* the
    `s3_token`) or its ensemble form `{s3_token}_{member}` (an ensemble
    carrier rides the member on the token, e.g. `channel_rt_1`), so an
    ensemble-only carrier is honoured rather than mis-reported.

    Args:
        s3_token: The product's bare `{output}` token (e.g. `channel_rt`).
        tokens: The sampled file tokens for one configuration directory.

    Returns:
        `True` if the product's token (bare or member-suffixed) is present.

    Examples:
        - A deterministic carrier's bare token counts as present:
            ```python
            >>> from earthlens.cli.validate import _nwm_token_present
            >>> _nwm_token_present("channel_rt", {"channel_rt", "land"})
            True

            ```
        - An ensemble carrier's `{token}_{member}` file token counts too:
            ```python
            >>> from earthlens.cli.validate import _nwm_token_present
            >>> _nwm_token_present("channel_rt", {"channel_rt_1"})
            True

            ```
        - A token absent from the sample is not present:
            ```python
            >>> from earthlens.cli.validate import _nwm_token_present
            >>> _nwm_token_present("channel_rt", {"land", "reservoir"})
            False

            ```
    """
    prefix = f"{s3_token}_"
    return any(token == s3_token or token.startswith(prefix) for token in tokens)


def _nwm_config_directory(config: Any, key: str) -> str:
    """Return the live bucket directory for an NWM configuration.

    A deterministic configuration is published under its bare `key`
    directory; an ensemble configuration (`members > 0`) publishes each
    member under `{key}_mem<N>`, so member 1 (`{key}_mem1`) is the directory
    sampled for the product-token check.

    Args:
        config: The configuration row (duck-typed: reads `members`).
        key: The configuration key (its bare directory name).

    Returns:
        The configuration's live directory name (`{key}_mem1` for an
        ensemble, `key` otherwise).

    Examples:
        - A deterministic configuration maps to its bare directory:
            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.cli.validate import _nwm_config_directory
            >>> _nwm_config_directory(SimpleNamespace(members=0), "short_range")
            'short_range'

            ```
        - An ensemble configuration maps to its member-1 directory:
            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.cli.validate import _nwm_config_directory
            >>> _nwm_config_directory(SimpleNamespace(members=6), "medium_range")
            'medium_range_mem1'

            ```
    """
    return f"{key}_mem1" if getattr(config, "members", 0) else key


def _live_nwm(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each NWM product's `s3_token` appears live under a carrier config.

    Ports the product-token check of the retired
    `tools/nwm/audit_nwm_catalog.py`: every curated product's file token
    (`channel_rt`, `land`, ...) must show up among the live files of at least
    one configuration that publishes it. Carriers are tried deterministic-first
    and sampling stops at the first hit, so a clean run touches only as many
    configuration directories as needed (reusing the shared NWM bucket
    primitives from `earthlens.cli.refresh`).

    Args:
        catalog: The loaded NWM `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per token not
        found live under any carrier configuration.
    """
    from earthlens.cli.refresh import _nwm_latest_complete_day, _nwm_unsigned_client

    client = _nwm_unsigned_client()
    day = _nwm_latest_complete_day(client)
    issues: list[str] = []
    for key, product in catalog.datasets.items():
        carriers = sorted(
            (
                cfg_key
                for cfg_key, config in catalog.configurations.items()
                if key in (getattr(config, "products", None) or [])
            ),
            key=lambda k: catalog.configurations[k].members,
        )
        seen = False
        for cfg_key in carriers:
            directory = _nwm_config_directory(catalog.configurations[cfg_key], cfg_key)
            if _nwm_token_present(
                product.s3_token, _nwm_sample_tokens(client, day, directory)
            ):
                seen = True
                break
        if not seen:
            issues.append(
                f"{key}: s3_token {product.s3_token!r} not found live under "
                "any carrier configuration"
            )
    return len(catalog.datasets), issues


#: Provider id -> a live reachability validator (the `--live` half). May add
#: a provider not in :data:`_VALIDATORS` (e.g. openeo / ecmwf are live-only).
_LIVE_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    "s3": _live_s3,
    "nwm": _live_nwm,
    "overture": _live_overture,
    "ghsl": _live_ghsl,
    "openeo": _live_openeo,
    "radar": _live_radar,
    "nwp": _live_nwp,
    "ecmwf": _live_ecmwf,
}


def supported_providers(live: bool = False) -> list[str]:
    """Return the provider ids that have a validator wired up.

    Args:
        live: When `True`, include providers that only have a `--live`
            reachability validator (e.g. openeo).

    Returns:
        The sorted provider ids `validate` can check.

    Examples:
        - nwp is wired up:

            ```python
            >>> from earthlens.cli.validate import supported_providers
            >>> "nwp" in supported_providers()
            True

            ```
    """
    providers = set(_VALIDATORS)
    if live:
        providers |= set(_LIVE_VALIDATORS)
    return sorted(providers)


def validate_one(info: BackendInfo, live: bool = False) -> ValidateResult:
    """Validate one provider's curated entries.

    Always runs the offline structural lint when one exists; with
    `live=True` it additionally runs the live reachability validator (a
    network / SDK round-trip per entry). A provider with neither returns
    `"unsupported"`; any error returns `"error"` — neither raises.

    Args:
        info: The backend to validate.
        live: When `True`, also run the live reachability validator.

    Returns:
        The :class:`ValidateResult` for `info`.
    """
    offline = _VALIDATORS.get(info.provider)
    live_validator = _LIVE_VALIDATORS.get(info.provider) if live else None
    if offline is None and live_validator is None:
        detail = (
            "no validator wired up for this provider"
            if not live or info.provider not in _LIVE_VALIDATORS
            else "no live validator wired up for this provider"
        )
        return ValidateResult(
            provider=info.provider, status="unsupported", detail=detail
        )
    try:
        catalog = load_catalog(info)
        checked = 0
        issues: list[str] = []
        for validator in (offline, live_validator):
            if validator is not None:
                count, found = validator(catalog)
                checked += count
                issues.extend(found)
    except Exception as exc:  # noqa: BLE001 — validation failures are reported
        return ValidateResult(provider=info.provider, status="error", detail=str(exc))
    return ValidateResult(
        provider=info.provider, status="ok", checked=checked, issues=issues
    )
