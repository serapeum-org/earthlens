"""Variable-catalog loader for the CDS-backed ECMWF data source.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
CADS catalog spanning all five stores. The catalog
ships as a directory of per-family YAML files under
`src/earthlens/ecmwf/catalog/` — one `<family>.yaml` per product
family (CDS: `era5.yaml`, `carra.yaml`, `cerra.yaml`, `cmip5.yaml`,
`cordex.yaml`, `seasonal.yaml`, `satellite.yaml`, `other.yaml`; ADS:
`ads.yaml`; EWDS: `ewds.yaml`, `efas.yaml`, `fire.yaml`) plus a single
`_index.yaml` carrying the schema header and the informational
per-store `available_datasets:` index. The loader unions every file's
`datasets:` block into one :class:`Catalog` at construction time (a
dataset key declared in two files is a load-time error), the same way
the GEE / CMEMS catalogs merge. Split out of
:mod:`earthlens.ecmwf.backend` so the request / download machinery and
the catalog file-IO live in separate modules.

The two consumed top-level sections each map to a typed field on
:class:`Catalog`:

* `available_datasets` (informational per-store index of dataset
  names across all five stores) → :attr:`Catalog.available_datasets`
* `datasets` (structural map of CDS datasets, each carrying a
  monthly variant and a per-variable map) → :attr:`Catalog.datasets`,
  with each value a :class:`Dataset`

The catalog has no flat per-variable view: variables are addressed
by the `(dataset_name, variable_name)` pair via
:meth:`Catalog.get_variable`. The same short code can legitimately
appear under more than one dataset (e.g. `"2m-temperature"` lives
in both `reanalysis-era5-single-levels` and
`reanalysis-era5-land`), so the dataset name is part of the
identity.

The path to the bundled catalog directory lives at
:data:`CATALOG_PATH`; tests can monkey-patch that module attribute to
redirect the loader at a temporary directory or single YAML file (the
loader accepts both).

Examples:
    - Construct the catalog and reach the structural map:

        ```python
        >>> from earthlens.ecmwf import Catalog
        >>> cat = Catalog()
        >>> cat.get_variable(
        ...     "reanalysis-era5-single-levels", "2m-temperature"
        ... ).nc_variable
        't2m'
        >>> cat.get_dataset("reanalysis-era5-pressure-levels").pressure_level
        ['1000']

        ```
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from earthlens.base import AbstractCatalog, FluxableLeaf, Provider
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.providers import (
    clear_providers_cache as _clear_providers_cache_base,
)
from earthlens.base.providers import load_providers
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict
from earthlens.ecmwf.constraints import fetch_constraints

# `read_cdsapirc` / `download_job` / `list_recent_jobs` were split out of this
# module into `earthlens.ecmwf.jobs` (N3 in
# the cross-backend catalog comparison). `Catalog` still delegates to
# `download_job` / `list_recent_jobs` internally, so import them under private
# names; `_read_cdsapirc` is re-exported only so any external caller using
# `from earthlens.ecmwf.catalog import _read_cdsapirc` keeps working.
from earthlens.ecmwf.jobs import download_job as _download_job_impl
from earthlens.ecmwf.jobs import list_recent_jobs as _list_recent_jobs_impl
from earthlens.ecmwf.jobs import read_cdsapirc as _read_cdsapirc  # noqa: F401

_LEGACY_MARS_KEYS: frozenset[str] = frozenset(
    {"number_para", "download type", "var_name"}
)

CATALOG_PATH: Path = Path(__file__).parent / "catalog"
PROVIDERS_PATH: Path = Path(__file__).parent / "providers.yaml"

# Module-level cache of parsed catalog data, keyed on
# `(resolved_path, mtime_ns)` so any real file mutation (`vim`-save,
# script append) invalidates the entry naturally. Mirrors the GEE
# pattern (H1 / M2) so repeated `Catalog()` construction is ~1 ms
# instead of paying the YAML parse + pydantic validation each time.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Dataset], dict[str, str]]] = (
    CatalogParseCache()
)


def clear_catalog_cache() -> None:
    """Empty the module-level catalog + providers parse caches.

    Useful in tests that rewrite the catalog on disk and want to
    force a re-parse. Production callers do not need this — the
    cache keys include `st_mtime_ns`, so any real file mutation
    invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()
    _clear_providers_cache_base()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='CDS', shard_noun='per-family')


def _merge_available(
    block: Any, available: list[str], by_store: dict[str, str]
) -> None:
    """Union one file's `available_datasets` block into the flat list + store map.

    Args:
        block: A file's `available_datasets` value — a flat list of ids, or a
            per-store `{cds: [...], ads: [...], ...}` mapping (in which
            case each id is also recorded against its store for endpoint
            auto-resolution).
        available: Accumulator for the flat id list (mutated in place).
        by_store: Accumulator mapping each id to its store slug (mutated).
    """
    if isinstance(block, dict):
        for store, ids in block.items():
            for ident in ids or []:
                available.append(ident)
                by_store[str(ident)] = str(store)
    else:
        available += list(block)


def _merge_datasets(
    data: dict[str, Any],
    yaml_path: Path,
    datasets_yaml: dict[str, Any],
    seen_in: dict[str, str],
) -> None:
    """Union one file's `datasets:` map, rejecting a key declared twice.

    Args:
        data: The parsed YAML mapping for one catalog file.
        yaml_path: That file's path (named in the duplicate-key error).
        datasets_yaml: Accumulator of `dataset_key -> body` (mutated in place).
        seen_in: Accumulator of `dataset_key -> filename` for the error message.

    Raises:
        ValueError: If a dataset key already appears in `datasets_yaml`.
    """
    for ds_key, ds_body in (data.get("datasets") or {}).items():
        if ds_key in datasets_yaml:
            raise ValueError(
                f"duplicate dataset key {ds_key!r}: declared in both "
                f"`{seen_in[ds_key]}` and `{yaml_path.name}`. Each CDS "
                "dataset key must live in exactly one file."
            )
        seen_in[ds_key] = yaml_path.name
        datasets_yaml[ds_key] = ds_body


def _load_catalog_data(
    path: Path,
) -> tuple[list[str], dict[str, Dataset], dict[str, str]]:
    """Parse, validate, and cache the CDS catalog at `path`.

    Returns a `(available_datasets, datasets)` tuple of the same shape
    :class:`Catalog` exposes. When `path` is a directory, every `*.yaml`
    is merged: `available_datasets:` entries are concatenated and
    `datasets:` maps are unioned (a dataset key declared in two files is
    an error). `available_datasets:` may be either a flat list of ids or a
    per-store mapping (`{cds: [...], ads: [...], ewds: [...]}`, written by
    `earthlens datasets refresh ecmwf`); both are unioned into the flat
    list. Cached on the resolved path plus every contributing file's
    `mtime_ns` so a second `Catalog()` on an unchanged catalog skips both
    YAML parsing and pydantic validation.

    Raises:
        ValueError: If the catalog is missing, has no `datasets:` block,
            has no variables under any dataset, or declares the same
            dataset key twice anywhere.
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    available: list[str] = []
    by_store: dict[str, str] = {}
    datasets_yaml: dict[str, Any] = {}
    seen_in: dict[str, str] = {}
    for yaml_path in files:
        data = load_yaml_strict(yaml_path) or {}
        _merge_available(data.get("available_datasets") or [], available, by_store)
        _merge_datasets(data, yaml_path, datasets_yaml, seen_in)

    if not datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty "
            "'datasets' key. The catalog must contain at least "
            "one dataset with one variable. See the schema header "
            "in `_index.yaml`."
        )

    structural, total_vars = _build_dataset_map(datasets_yaml, path)
    _synthesize_monthly_entries(structural, datasets_yaml)

    if total_vars == 0:
        raise ValueError(
            f"{path} has no variables under any dataset. "
            "The catalog must contain at least one variable. "
            "See the schema header in `_index.yaml`."
        )

    _CATALOG_CACHE[key] = (available, structural, by_store)
    return _CATALOG_CACHE[key]


def _provider_for_dataset(ds_name: str) -> str:
    """Map a CDS dataset name to its canonical provider slug (L2).

    Pattern-matched at load time rather than carried in the YAML —
    CDS dataset names already encode their provider through their
    name prefixes (`reanalysis-carra-*`, `projections-cmip5-*`,
    `projections-cordex-*`, etc.).
    """
    if ds_name.startswith(("reanalysis-carra", "reanalysis-pan-carra")):
        return "carra-consortium"
    if ds_name.startswith("reanalysis-cerra"):
        return "cerra-consortium"
    if ds_name.startswith("projections-cmip5"):
        return "cmip5-modelling-centres"
    if ds_name.startswith("projections-cordex"):
        return "cordex-consortium"
    if ds_name.startswith("cams-"):
        return "copernicus-cams"
    if ds_name.startswith(("cems-", "efas-")):
        return "copernicus-cems"
    return "ecmwf"


def _build_dataset_map(
    datasets_yaml: dict[str, dict[str, Any]],
    catalog_path: Path,
) -> tuple[dict[str, Dataset], int]:
    """Build the structural per-dataset :class:`Dataset` map (N1).

    Walks every entry in `datasets_yaml`, validates each variable into
    a :class:`Variable`, and packs the per-dataset metadata (monthly
    cross-reference, pressure_level / product_type defaults, extras,
    request_kind) into a :class:`Dataset`. Returns the map plus the
    total variable count (used by the caller to fail loudly when a
    catalog declares zero variables).

    Args:
        datasets_yaml: Raw `datasets:` mapping from the YAML.
        catalog_path: Path of the YAML file (used in error messages).

    Returns:
        (`structural`, `total_vars`) — the dataset map and the count
        of variables built across all datasets.

    Raises:
        ValueError: If any variable fails :class:`Variable` validation.
    """
    structural: dict[str, Dataset] = {}
    total_vars = 0
    for ds_name, ds_body in datasets_yaml.items():
        monthly = ds_body.get("monthly")
        pressure_level = ds_body.get("pressure_level")
        ds_product_type = ds_body.get("product_type")
        ds_extras = dict(ds_body.get("extras") or {})
        ds_request_kind = ds_body.get("request_kind", "form")
        ds_endpoint = ds_body.get("endpoint", "cds")
        ds_grid_resolution = ds_body.get("grid_resolution")
        ds_vars: dict[str, Variable] = {}
        for code, entry in (ds_body.get("variables") or {}).items():
            merged = dict(entry)
            # The catalog key is the CDS dataset short name by default, but a row
            # may set `cds_dataset:` explicitly to curate two configs of ONE CDS
            # dataset under distinct catalog ids — e.g. the GloFAS historical
            # `consolidated` vs `intermediate` streams both retrieve from
            # `cems-glofas-historical`. setdefault (not assignment) so the explicit
            # override wins; no existing row sets it, so this is backward-compatible.
            merged.setdefault("cds_dataset", ds_name)
            # Remember the catalog key the variable is curated under. It equals
            # cds_dataset for every ordinary row, and differs only when the row
            # overrode cds_dataset (the GloFAS intermediate stream). Naming the
            # output by dataset_id keeps the intermediate's file from colliding
            # with the consolidated stream (same cds_variable + cds_dataset).
            merged.setdefault("dataset_id", ds_name)
            # Default cds_variable to the slug-with-underscores form
            # of the YAML key (e.g. "2m-temperature" -> "2m_temperature").
            # A per-variable row may set `cds_variable` explicitly
            # to override this when the request name does not match.
            merged.setdefault("cds_variable", code.replace("-", "_"))
            # Per-variable override wins; otherwise inherit the
            # dataset-level default. Only single-level datasets
            # leave both unset.
            if "cds_pressure_level" not in merged and pressure_level is not None:
                merged["cds_pressure_level"] = pressure_level
            # Same parent-default / per-row-override pattern for
            # product_type. Parent unset → Variable's own default (an empty
            # list), which the request builder omits unless a row sets it.
            if "product_type" not in merged and ds_product_type is not None:
                merged["product_type"] = ds_product_type
            # Merge parent-level extras under per-row overrides:
            # row-level keys win on collision so a variable can
            # diverge from the family defaults (e.g. one CARRA row
            # carrying a different leadtime than the rest).
            row_extras = dict(merged.get("extras") or {})
            merged["extras"] = {**ds_extras, **row_extras}
            merged.setdefault("request_kind", ds_request_kind)
            merged.setdefault("endpoint", ds_endpoint)
            if "grid_resolution" not in merged and ds_grid_resolution is not None:
                merged["grid_resolution"] = ds_grid_resolution
            try:
                ds_vars[code] = Variable(**merged)
            except ValidationError as exc:
                raise ValueError(
                    f"{catalog_path} entry {code!r} failed validation:\n{exc}"
                ) from exc
            total_vars += 1
        structural[ds_name] = Dataset(
            monthly=monthly,
            pressure_level=pressure_level,
            product_type=ds_product_type,
            extras=ds_extras,
            request_kind=ds_request_kind,
            endpoint=ds_endpoint,
            grid_resolution=ds_grid_resolution,
            provider=_provider_for_dataset(ds_name),
            variables=ds_vars,
        )
    return structural, total_vars


def _synthesize_monthly_entries(
    structural: dict[str, Dataset],
    datasets_yaml: dict[str, dict[str, Any]],
) -> None:
    """Mutate `structural` to add an auto-synthesised entry per `monthly:` xref (N1).

    The YAML keeps `monthly: <name>` on the parent dataset (compact); the
    catalog presents both names as queryable datasets with the same
    variable set, so users can name either form in their `variables`
    dict. The synthesised entry rebrands each variable's `cds_dataset`
    to the monthly name; everything else (variable code, units,
    nc_variable, extras) is shared. The monthly entry needs its own
    product_type — there is no hardcoded fallback; the parent must
    declare `monthly_product_type:` alongside `monthly:`.

    Raises:
        ValueError: If a dataset declares `monthly:` but is missing
            `monthly_product_type:`.
    """
    for ds_name, ds_body in datasets_yaml.items():
        ds = structural[ds_name]
        if not ds.monthly or ds.monthly in structural:
            continue
        monthly_pt = ds_body.get("monthly_product_type")
        if monthly_pt is None:
            raise ValueError(
                f"dataset {ds_name!r} declares `monthly: "
                f"{ds.monthly!r}` but no `monthly_product_type:`. "
                "Auto-synthesis of the monthly-means catalog "
                "entry needs an explicit product_type for the "
                "synthesized variables (e.g. "
                "`monthly_product_type: [monthly_averaged_reanalysis]`)."
            )
        rebranded = {
            # dataset_id tracks cds_dataset here (the synthesised entry is keyed
            # under ds.monthly), so the monthly output filename is unaffected.
            code: var.model_copy(
                update={
                    "cds_dataset": ds.monthly,
                    "dataset_id": ds.monthly,
                    "product_type": monthly_pt,
                }
            )
            for code, var in ds.variables.items()
        }
        structural[ds.monthly] = Dataset(
            monthly=None,
            pressure_level=ds.pressure_level,
            product_type=monthly_pt,
            extras=dict(ds.extras),
            request_kind=ds.request_kind,
            endpoint=ds.endpoint,
            grid_resolution=ds.grid_resolution,
            provider=_provider_for_dataset(ds.monthly),
            variables=rebranded,
        )


def _validate_endpoint(value: str) -> str:
    """Reject an `endpoint` slug the endpoint router does not know.

    Args:
        value: The endpoint slug from the catalog.

    Returns:
        str: The validated slug.

    Raises:
        ValueError: If `value` is not a known CADS endpoint.
    """
    from earthlens.ecmwf.endpoints import ENDPOINTS

    if value not in ENDPOINTS:
        raise ValueError(
            f"unknown endpoint {value!r}; expected one of {sorted(ENDPOINTS)}"
        )
    return value


def _validate_grid_resolution(value: float | None) -> float | None:
    """Reject a non-positive `grid_resolution` (a zero would divide the snap).

    Args:
        value: The grid spacing in degrees, or `None`.

    Returns:
        float | None: The validated value.

    Raises:
        ValueError: If `value` is not `None` and not strictly positive.
    """
    if value is not None and value <= 0:
        raise ValueError(f"grid_resolution must be > 0, got {value!r}")
    return value


# Tokens in a `time_aggregation` / `temporal_resolution` value that mark the
# samples as a server-side temporal aggregate (a daily-or-coarser mean). Both the
# `day` and `daily` spellings appear in the catalog, so both are listed.
_TEMPORAL_AGGREGATE_TOKENS = (
    "mean",
    "average",
    "avg",
    "climatolog",
    "daily",
    "day",
    "dekad",
    "pentad",
    "week",
    "month",
    "season",
    "annual",
    "year",
)


def _denotes_temporal_aggregate(value: Any) -> bool:
    """Whether a `time_aggregation` / `temporal_resolution` value marks an aggregate.

    Returns `True` for a daily-or-coarser mean (`"daily"`, `"1_month_mean"`,
    `"monthly_mean"`, ...) and `False` for a raw / sub-daily / instantaneous value
    (`"instantaneous"`, `"1_hour"`, `"sub-daily"`, ...) or an empty one. Accepts a
    scalar or a list (CDS spells these both ways). Cadence-only values (`"daily"`,
    `"monthly"`) are assumed to name a mean — sum tokens are deliberately excluded,
    since the flag routes `op="auto"` to `"mean"` (see `is_pre_aggregated`).

    Args:
        value: The `time_aggregation` / `temporal_resolution` extra, or `None`.

    Returns:
        `True` when the value denotes a temporal aggregate.

    Examples:
        - Daily-or-coarser means are aggregates; raw / sub-daily values are not:

            ```python
            >>> from earthlens.ecmwf.catalog import _denotes_temporal_aggregate
            >>> _denotes_temporal_aggregate("1_month_mean")
            True
            >>> _denotes_temporal_aggregate("daily")
            True
            >>> _denotes_temporal_aggregate("instantaneous")
            False
            >>> _denotes_temporal_aggregate("1_hour")
            False
            >>> _denotes_temporal_aggregate(None)
            False

            ```
    """
    if not value:
        return False
    items = value if isinstance(value, (list, tuple)) else [value]
    text = " ".join(str(item).lower() for item in items)
    # Sub-daily / instantaneous samples are raw, not aggregates. This veto must
    # run before the token match — a `sub-daily` value contains the `daily`
    # token. Match the sub-daily spellings specifically (compacting separators)
    # so a coarser-than-daily `subseasonal` value is not swallowed; any new
    # sub-daily spelling must be added here.
    compact = text.replace("-", "").replace("_", "")
    if "instant" in text or "hour" in text or "subdaily" in compact:
        return False
    return any(token in text for token in _TEMPORAL_AGGREGATE_TOKENS)


class Variable(FluxableLeaf):
    """Per-variable catalog entry consumed by :class:`ECMWF`.

    A frozen pydantic model carrying the metadata for one row in the
    bundled CDS catalog. Loaded through :class:`Catalog`, which
    rewraps any :class:`pydantic.ValidationError` with the offending
    row's catalog key so a typo in the file (e.g. `cd_dataset` vs
    `cds_dataset`) surfaces at import time, not mid-download.
    Inherits `types` + `is_flux` from
    :class:`earthlens.base.FluxableLeaf`.

    Attributes:
        cds_dataset: CDS dataset short name the retrieve is sent to (the
            download target), e.g. `"reanalysis-era5-single-levels"`.
        dataset_id: Catalog key the variable is curated under. Equals
            `cds_dataset` for every ordinary row, but differs when a row
            overrides `cds_dataset` to curate a second config of one CDS
            dataset under a distinct id (the GloFAS historical
            `intermediate` stream, which retrieves from
            `cems-glofas-historical`). Used to name the output file so the
            two configs do not collide; `None` on a directly-built spec
            falls back to `cds_dataset`.
        cds_variable: CDS variable name passed in the retrieve()
            request, e.g. `"2m_temperature"`.
        nc_variable: Short variable name inside the CDS NetCDF
            (e.g. `"t2m"`); used by post-processing scripts to
            index `fh.variables[...]`. See
            `examples/post_process_ecmwf_netcdf.py`.
        units: Raw ERA5 unit string emitted by CDS for this variable
            (used in the output filename). The package returns values
            in their native ERA5 units; downstream code is responsible
            for any unit conversion. See `docs/examples/catalog.md`
            for the conversion factors typical ERA5 workflows apply.
        cds_pressure_level: Optional list of pressure levels (as
            strings, e.g. `["1000"]`) for pressure-level datasets.
        product_type: CDS `product_type` request parameter. Picks
            the data flavor within a dataset (e.g. `["reanalysis"]`
            vs `["ensemble_mean"]` for ERA5; `["analysis"]` vs
            `["forecast_based"]` for CARRA). Defaults to an empty
            list, which the request builder omits from the request
            (CAMS and other families that key on `type`/`quantity`
            carry none); ERA5 rows set `["reanalysis"]` explicitly and
            auto-synthesized monthly-means entries override to
            `["monthly_averaged_reanalysis"]`. Per-dataset and
            per-variable overrides land here via the catalog
            loader's merge.
        types: Optional `"flux"` or `"state"` marker. Flux values
            are accumulated per timestep on CDS so monthly
            aggregation multiplies by the number of days in the
            month; state values are instantaneous.
        extras: Free-form bag of additional CDS request parameters
            forwarded verbatim to `client.retrieve()`. Holds the
            non-ERA5 request fields that newer CDS dataset families
            require — e.g. `{"domain": "east", "leadtime_hour": "1"}`
            for CARRA, `{"experiment": "ssp585", "model": "ec_earth3"}`
            for CMIP6. Keys not enumerated in this model are not
            silently dropped: they live here and reach the server.
        endpoint: CADS instance this dataset lives on — `"cds"`
            (default), `"ads"`, `"ewds"`, `"ecds"` or `"xds"`. Propagated from the parent
            dataset; selects the retrieve URL via
            `earthlens.ecmwf.endpoints.open_client`.
        grid_resolution: Native grid spacing in degrees for the
            dataset (e.g. `0.05` for GloFAS on EWDS), or `None` to
            fall back to the ERA5 default (`ERA5_GRID_DEGREES`).
            Propagated from the parent dataset; used by
            `ECMWF._create_grid` to snap the bbox to the right grid.
    """

    # `model_config` (frozen=True, extra="forbid") and the `types` field
    # + `is_flux` property are inherited from `FluxableLeaf`.

    cds_dataset: str
    dataset_id: str | None = None
    cds_variable: str
    nc_variable: str
    units: str
    product_type: list[str] = Field(default_factory=list)
    cds_pressure_level: list[str] | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    request_kind: str = "form"
    endpoint: str = "cds"
    grid_resolution: float | None = None

    @field_validator("extras", mode="before")
    @classmethod
    def _reject_legacy_mars_keys(cls, value: Any) -> Any:
        """Forbid the pre-cdsapi MARS keys from leaking back via `extras`.

        `number_para` / `download type` / `var_name` were the
        request-shape keys of the legacy MARS-ECMWFAPI flow. They are
        meaningless under cdsapi and would silently corrupt requests
        if they reached :meth:`ECMWF._api`; reject them at load time so
        a stale catalog row fails loud instead of mid-download.
        """
        if not isinstance(value, dict):
            return value
        offending = _LEGACY_MARS_KEYS & set(value)
        if offending:
            raise ValueError(
                f"extras carries legacy MARS keys {sorted(offending)!r}; "
                "these are not valid under cdsapi"
            )
        return value

    @field_validator("endpoint")
    @classmethod
    def _known_endpoint(cls, value: str) -> str:
        """Reject an `endpoint` slug the router does not know."""
        return _validate_endpoint(value)

    @field_validator("grid_resolution")
    @classmethod
    def _positive_grid_resolution(cls, value: float | None) -> float | None:
        """Reject a non-positive `grid_resolution` (would divide the snap by zero)."""
        return _validate_grid_resolution(value)

    # `is_flux` property is inherited from `FluxableLeaf` (N1 in
    # the cross-backend catalog comparison).

    @property
    def is_pre_aggregated(self) -> bool:
        """Whether each NetCDF sample is already a server-side temporal aggregate.

        `True` for the CDS families whose samples are aggregated on the server,
        so re-accumulating them over a coarser window over-counts:

        * the daily-statistics family (`derived-era5-*-daily-statistics`), whose
          dataset-level `daily_statistic` request extra is merged into every
          child variable's `extras`;
        * the ERA5 monthly-means family (`reanalysis-era5-*-monthly-means`),
          whose product type is `monthly_averaged_*`;
        * families carrying a `time_aggregation` / `temporal_resolution` extra
          that denotes a daily-or-coarser mean (`ecv-for-climate-change`,
          `reanalysis-carra-means` / `-pan-carra-means`,
          `projections-cordex-domains-single-levels`); and
        * monthly datasets whose only marker is a `-monthly` dataset id — the
          CMIP5 monthly projections (`projections-cmip5-monthly-*`) and the
          seasonal monthly-mean forecasts (`seasonal-monthly-single-levels`,
          whose `monthly_mean` lives in `extras["product_type"]`).

        The `time_aggregation` / `temporal_resolution` markers live in `extras`
        (or the dataset id), not the `product_type` field, which stays
        `[reanalysis]` on these rows — hence the extra checks below.

        Every flagged family is treated as a temporal **mean**, mirroring the
        established `reanalysis-era5-*-monthly-means` convention: the flagged
        flux families are all mean products — `ecv-for-climate-change`
        (`1_month_mean`), `-cordex-` (`monthly_mean`), CMIP5 monthly
        (`mean-*-flux` variables), CARRA / pan-CARRA (the `*-means` datasets) and
        `seasonal-monthly-single-levels` (`extras["product_type"] ==
        ["monthly_mean"]`).
        A family whose samples were pre-aggregated as a *total* (accumulation)
        would need `sum`, not `mean`, and so must not be flagged here; none
        exists in the shipped catalog. The extra-based branch additionally flags
        many non-flux (`state`) satellite / in-situ / CMIP6 rows, where the flag
        is a functional no-op (`_resolve_op` maps `state` to `mean` regardless).

        `earthlens.aggregate._resolve_op` reads this so `op="auto"` reduces such
        variables with `"mean"` rather than `"sum"` — a plain `sum` over samples
        that are themselves daily/monthly aggregates multiplies by the number of
        samples per window (e.g. ~30× for monthly windows over daily statistics).

        Returns:
            `True` when each sample is a server-side temporal aggregate — so
            `op="auto"` should average rather than sum — and `False` otherwise.

        Examples:
            - An ERA5 monthly-means flux variable is pre-aggregated (its product
              type is `monthly_averaged_*`):

                ```python
                >>> from earthlens.ecmwf.catalog import Variable
                >>> monthly = Variable(
                ...     cds_dataset="reanalysis-era5-single-levels-monthly-means",
                ...     cds_variable="total_precipitation",
                ...     nc_variable="tp",
                ...     units="m",
                ...     product_type=["monthly_averaged_reanalysis"],
                ...     types="flux",
                ... )
                >>> monthly.is_pre_aggregated
                True

                ```
            - A CARRA-means variable is flagged via its `time_aggregation` extra,
              even though its product type is not `monthly_averaged_*`:

                ```python
                >>> from earthlens.ecmwf.catalog import Variable
                >>> carra = Variable(
                ...     cds_dataset="reanalysis-carra-means",
                ...     cds_variable="10m_wind_gust",
                ...     nc_variable="fg10",
                ...     units="m s**-1",
                ...     types="flux",
                ...     extras={"time_aggregation": "daily"},
                ... )
                >>> carra.is_pre_aggregated
                True

                ```
            - A raw hourly ERA5 flux variable is not, so `op="auto"` still sums it:

                ```python
                >>> from earthlens.ecmwf.catalog import Variable
                >>> raw = Variable(
                ...     cds_dataset="reanalysis-era5-single-levels",
                ...     cds_variable="total_precipitation",
                ...     nc_variable="tp",
                ...     units="m",
                ...     product_type=["reanalysis"],
                ...     types="flux",
                ... )
                >>> raw.is_pre_aggregated
                False

                ```
        """
        if self.extras.get("daily_statistic"):
            return True
        if any(pt.startswith("monthly_averaged") for pt in self.product_type):
            return True
        if _denotes_temporal_aggregate(self.extras.get("time_aggregation")):
            return True
        if _denotes_temporal_aggregate(self.extras.get("temporal_resolution")):
            return True
        # CMIP5 monthly projections and seasonal monthly-mean forecasts carry no
        # in-row time_aggregation marker (the seasonal family's `monthly_mean`
        # lives in extras["product_type"]), so fall back to the `-monthly`
        # dataset-id suffix. Validated against the shipped catalog: every
        # `-monthly` id is a genuine monthly aggregate (no raw / sub-monthly id
        # contains `-monthly`); a future id that did would need an explicit marker.
        return "-monthly" in self.cds_dataset


class Dataset(BaseModel):
    """One CDS dataset's section in the catalog.

    Mirrors the shape of a single `datasets.<name>:` block in the
    bundled CDS catalog — the monthly-aggregate variant of the
    dataset, the default pressure levels (for pressure-level
    datasets), and the per-variable map. Same dataset name is used
    as the parent key in :attr:`Catalog.datasets`; it is not stored
    again here.

    Attributes:
        monthly: CDS dataset short name to use when
            `temporal_resolution == "monthly"`. `None` when the
            dataset has no monthly-aggregate variant.
        pressure_level: Default list of pressure levels (as strings,
            e.g. `["1000"]`) for pressure-level datasets. `None`
            for single-level datasets. Propagated to each variable's
            `cds_pressure_level` at load time.
        extras: Default extra CDS request parameters propagated into
            each child :class:`Variable`'s `extras` map. Per-row
            `extras:` overrides win over these defaults. Carries
            the family-wide selectors (e.g. `domain`, `leadtime_hour`,
            `experiment`, `model`) that the dataset's request shape
            requires beyond the ERA5 standard set.
        endpoint: CADS instance the dataset lives on — `"cds"`
            (default), `"ads"`, `"ewds"`, `"ecds"` or `"xds"`. Inherited by every child
            variable and used to route the retrieve URL.
        grid_resolution: Native grid spacing in degrees (e.g. `0.05`
            for GloFAS), or `None` to use the ERA5 default. Inherited
            by every child variable.
        variables: Per-variable map keyed by the slugified short code
            (e.g. `"2m-temperature"`).

    Examples:
        - Inspect a single-level dataset entry:

            ```python
            >>> from earthlens.ecmwf import Catalog
            >>> cat = Catalog()
            >>> single = cat.datasets["reanalysis-era5-single-levels"]
            >>> single.monthly
            'reanalysis-era5-single-levels-monthly-means'
            >>> single.pressure_level is None
            True
            >>> "2m-temperature" in single.variables
            True

            ```
        - Pressure-level datasets carry the default level list:

            ```python
            >>> from earthlens.ecmwf import Catalog
            >>> cat = Catalog()
            >>> press = cat.datasets["reanalysis-era5-pressure-levels"]
            >>> press.pressure_level
            ['1000']
            >>> press.variables["temperature"].cds_pressure_level
            ['1000']

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    monthly: str | None = None
    pressure_level: list[str] | None = None
    product_type: list[str] | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    request_kind: str = "form"
    endpoint: str = "cds"
    grid_resolution: float | None = None
    provider: str | None = None
    variables: dict[str, Variable] = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def _known_endpoint(cls, value: str) -> str:
        """Reject an `endpoint` slug the router does not know."""
        return _validate_endpoint(value)

    @field_validator("grid_resolution")
    @classmethod
    def _positive_grid_resolution(cls, value: float | None) -> float | None:
        """Reject a non-positive `grid_resolution` (would divide the snap by zero)."""
        return _validate_grid_resolution(value)


class Catalog(AbstractCatalog):
    """Variable catalog for the CDS-backed ECMWF data source.

    Reads the bundled CDS catalog (the `catalog/` directory, shipped as
    package data) and exposes its consumed top-level sections as typed
    pydantic fields.
    Instantiate with no arguments (`Catalog()`) — :func:`model_post_init`
    parses the YAML and populates every field in one pass.

    Variables are addressed by the `(dataset_name, variable_name)`
    pair via :meth:`get_variable`; there is no flat per-code lookup.
    The same short code can legitimately appear under more than one
    dataset (e.g. `"2m-temperature"` lives in both
    `reanalysis-era5-single-levels` and `reanalysis-era5-land`), so
    the dataset name is part of the identity.

    Attributes:
        available_datasets: Informational list of every dataset id across
            all five stores (CDS + ADS + EWDS + ECDS + XDS), unioned from the
            per-store `available_datasets:` block in `_index.yaml`; runtime
            code does not consume it.
        datasets: Structural map keyed by CDS dataset short name. Each
            value is a :class:`Dataset` carrying that dataset's
            monthly-aggregate variant and its per-variable map. The
            authoritative store: every catalog lookup goes through
            it.

    Examples:
        - Look up a variable by `(dataset_name, variable_name)`:

            ```python
            >>> from earthlens.ecmwf import Catalog
            >>> spec = Catalog().get_variable(
            ...     "reanalysis-era5-single-levels", "2m-temperature"
            ... )
            >>> spec.cds_dataset
            'reanalysis-era5-single-levels'
            >>> spec.nc_variable
            't2m'

            ```
        - The same short code under a different dataset is a
          different :class:`Variable`:

            ```python
            >>> from earthlens.ecmwf import Catalog
            >>> Catalog().get_variable(
            ...     "reanalysis-era5-land", "2m-temperature"
            ... ).cds_dataset
            'reanalysis-era5-land'

            ```
        - Iterate variables grouped by dataset (structural):

            ```python
            >>> from earthlens.ecmwf import Catalog
            >>> cat = Catalog()
            >>> cat.get_dataset("reanalysis-era5-pressure-levels").monthly
            'reanalysis-era5-pressure-levels-monthly-means'
            >>> sorted(cat.get_dataset("reanalysis-era5-pressure-levels").variables)[:3]
            ['divergence', 'fraction-of-cloud-cover', 'geopotential']

            ```
        - Inspect what the five stores host overall:

            ```python
            >>> from earthlens.ecmwf import Catalog
            >>> len(Catalog().available_datasets)
            174

            ```
    """

    _catalog_kind: str = "CDS catalog"

    available_datasets: list[str] = Field(default_factory=list)
    available_by_store: dict[str, str] = Field(default_factory=dict)
    datasets: dict[str, Dataset] = Field(default_factory=dict)
    providers: dict[str, Provider] = Field(default_factory=dict)

    def store_for(self, dataset_id: str) -> str | None:
        """Return the store slug (e.g. `cds` / `ewds` / `xds`) hosting a dataset.

        Reads the per-store availability index. Used to auto-resolve the
        `endpoint` for a raw-request passthrough when the caller omits it.

        Args:
            dataset_id: A Copernicus dataset id (e.g. `"cams-global-reanalysis-eac4"`).

        Returns:
            str | None: The store slug, or `None` if the id is not in the index.

        Examples:
            - An ADS (CAMS) dataset resolves to the `ads` store:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> Catalog().store_for("cams-global-reanalysis-eac4")
                'ads'

                ```
            - An id absent from every store's index returns `None`:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> Catalog().store_for("not-a-real-dataset") is None
                True

                ```
        """
        return self.available_by_store.get(dataset_id)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `available_datasets`, `datasets`, `providers` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "available_datasets": loaded.available_datasets,
            "available_by_store": loaded.available_by_store,
            "datasets": loaded.datasets,
            "providers": loaded.providers,
        }

    @classmethod
    def load(
        cls,
        catalog_path: Path | None = None,
        providers_path: Path | None = None,
    ) -> Catalog:
        """Read the CDS catalog + providers registry from disk (cached).

        Mirrors :meth:`earthlens.gee.Catalog.load` so the two backends
        feel identical. Validates that every `Dataset.provider` slug
        is in the registry; an unregistered slug is a load-time error.

        Args:
            catalog_path: Path to a catalog directory (of per-family
                `*.yaml` files) or a single YAML file. Defaults to
                module-level :data:`CATALOG_PATH`.
            providers_path: Path to `providers.yaml`. Defaults to
                module-level :data:`PROVIDERS_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: Propagated from :func:`_load_catalog_data` or
                :func:`earthlens.base.providers.load_providers`, plus
                an unregistered-slug error if the YAML references a
                provider not in the registry.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        providers_path = (
            providers_path if providers_path is not None else PROVIDERS_PATH
        )
        available_datasets, datasets, available_by_store = _load_catalog_data(
            catalog_path
        )
        providers = load_providers(providers_path)
        unknown = sorted(
            {
                d.provider
                for d in datasets.values()
                if d.provider and d.provider not in providers
            }
        )
        if unknown:
            raise ValueError(
                f"the following provider slugs are referenced by "
                f"`{catalog_path.name}` but missing from {providers_path}: "
                f"{unknown}. Add them to providers.yaml or fix the typo."
            )
        return cls(
            available_datasets=list(available_datasets),
            available_by_store=dict(available_by_store),
            datasets=dict(datasets),
            providers=dict(providers),
        )

    def get_catalog(self) -> dict[str, Dataset]:
        """Return the structural per-dataset map.

        Satisfies the abstract base's contract; the actual parsing
        is done in :func:`model_post_init`.

        Returns:
            dict[str, Dataset]: One entry per CDS dataset. Same
            object as :attr:`datasets`.

        Examples:
            - Inspect the dataset count and a sample:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> mapping = Catalog().get_catalog()
                >>> "reanalysis-era5-single-levels" in mapping
                True
                >>> mapping["reanalysis-era5-single-levels"].monthly
                'reanalysis-era5-single-levels-monthly-means'

                ```
        """
        return self.datasets

    def get_variable(self, dataset_name: str, variable_name: str) -> Variable:
        """Return the :class:`Variable` for a `(dataset, code)` pair.

        Args:
            dataset_name: CDS dataset short name as it appears as a
                key in :attr:`datasets` (e.g.
                `"reanalysis-era5-single-levels"`).
            variable_name: Short variable code as it appears as a
                YAML key under that dataset (e.g.
                `"2m-temperature"`, `"total-precipitation"`).

        Returns:
            Variable: Per-variable metadata loaded from the bundled
            CDS catalog.

        Raises:
            KeyError: If `dataset_name` is not curated, or if
                `variable_name` is not declared under that dataset.

        Examples:
            - Look up a single-level ERA5 variable and read its CDS
              dataset and NetCDF short name:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> spec = Catalog().get_variable(
                ...     "reanalysis-era5-single-levels", "2m-temperature"
                ... )
                >>> spec.cds_dataset
                'reanalysis-era5-single-levels'
                >>> spec.nc_variable, spec.units
                ('t2m', 'K')

                ```
            - Pressure-level variables expose `cds_pressure_level`:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> spec = Catalog().get_variable(
                ...     "reanalysis-era5-pressure-levels", "temperature"
                ... )
                >>> spec.cds_pressure_level
                ['1000']

                ```
            - The same short code under a different dataset is a
              different Variable:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> Catalog().get_variable(
                ...     "reanalysis-era5-land", "2m-temperature"
                ... ).cds_dataset
                'reanalysis-era5-land'

                ```
            - Unknown dataset or variable raises `KeyError`:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> Catalog().get_variable(
                ...     "reanalysis-era5-single-levels", "not-a-variable"
                ... )
                Traceback (most recent call last):
                    ...
                KeyError: 'not-a-variable'

                ```
        """
        return self.datasets[dataset_name].variables[variable_name]

    # `get_dataset(name)` (with the did-you-mean hint) and the dict-like
    # `__getitem__` / `__contains__` / `__iter__` / `__len__` / `__repr__`
    # / `__str__` dunders are inherited from
    # :class:`earthlens.base.AbstractCatalog` (M1 in
    # the cross-backend catalog comparison).

    def health(self) -> dict[str, list[str]]:
        """Report structural hygiene issues across the loaded catalog (L1).

        Returns a mapping `check_name -> list of "<dataset>/<variable>"
        offenders`. An empty list means the check is currently passing;
        an empty dict means the catalog is clean. Most schema-level
        invariants (duplicate keys, unknown fields, missing required
        fields, legacy MARS keys in `extras`) are already enforced at
        load time — this method covers the residual data-quality checks
        that can't be expressed in the pydantic schema.

        Checks reported:

        * `variable_missing_nc_variable` — variables whose
          `nc_variable` is empty or whitespace-only (would break
          downstream NetCDF reads).
        * `dataset_without_variables` — datasets carrying zero
          curated variables. Should always be `[]` since the loader
          rejects these; included for defence in depth.
        """
        missing_nc: list[str] = []
        empty_dataset: list[str] = []
        unregistered_provider: list[str] = []
        used_providers: set[str] = set()
        for ds_name, ds in self.datasets.items():
            if not ds.variables:
                empty_dataset.append(ds_name)
                continue
            for var_code, var in ds.variables.items():
                if not var.nc_variable or not var.nc_variable.strip():
                    missing_nc.append(f"{ds_name}/{var_code}")
            if ds.provider:
                used_providers.add(ds.provider)
                if ds.provider not in self.providers:
                    unregistered_provider.append(ds_name)
        unused_provider = sorted(set(self.providers) - used_providers)
        return {
            "variable_missing_nc_variable": sorted(missing_nc),
            "dataset_without_variables": sorted(empty_dataset),
            "unregistered_provider": sorted(unregistered_provider),
            "unused_provider": unused_provider,
        }

    def describe(self, dataset_name: str) -> dict[str, Any]:
        """Return a structured introspection record for a CDS dataset.

        Useful for "what variables and extras does dataset X expose?"
        questions at runtime — the CLI / notebook caller can dump
        the result without needing to walk the YAML themselves.

        Args:
            dataset_name: CDS dataset short name as it appears as a
                key in :attr:`datasets` (e.g.
                `"reanalysis-era5-land"`).

        Returns:
            dict with keys `dataset` (the short name), `monthly`
            (the monthly-aggregate dataset name or `None`),
            `pressure_level` (the default level list or `None`),
            `extras` (the parent-level request defaults), and
            `variables` (sorted list of the variable short codes
            available under this dataset).

        Raises:
            KeyError: If `dataset_name` is not a curated dataset
                (i.e. not present in :attr:`datasets`).

        Examples:
            - Describe ERA5-Land at a glance:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> info = Catalog().describe("reanalysis-era5-land")
                >>> info["dataset"]
                'reanalysis-era5-land'
                >>> info["monthly"]
                'reanalysis-era5-land-monthly-means'
                >>> len(info["variables"]) == 60
                True
                >>> "2m-temperature" in info["variables"]
                True

                ```
        """
        ds = self.get_dataset(dataset_name)
        return {
            "dataset": dataset_name,
            "monthly": ds.monthly,
            "pressure_level": ds.pressure_level,
            "extras": dict(ds.extras),
            "variables": sorted(ds.variables),
        }

    def minimal_valid_request(self, dataset_name: str) -> dict[str, Any]:
        """Return a known-valid minimal request for `dataset_name`.

        Walks the dataset's published `constraints.json` (cached
        per-process) and returns the first entry expanded into a
        request dict with one value per selector. Useful for:

        * verifying a CDS account is set up correctly (submit the
          returned dict via :meth:`cdsapi.Client.retrieve` and watch
          for a NetCDF rather than a 400),
        * seeing what a valid extras schema looks like for a new
          dataset before authoring catalog rows,
        * starting points for tests.

        The returned request always carries `data_format: netcdf`;
        the rest is whatever the first constraint entry enumerates.

        Args:
            dataset_name: CDS dataset short name. Does not need to be
                in :attr:`datasets` — the constraints endpoint is
                hit directly so any addressable dataset works.

        Returns:
            dict[str, Any]: A request dict ready to pass to
            :meth:`cdsapi.Client.retrieve`. Empty dict (besides
            `data_format`) when the dataset's constraints are
            empty / unreachable.

        Examples:
            - Inspect ECMWF's published shape for a new dataset
              before authoring rows. Marked `# doctest: +SKIP`
              because it requires network access:

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> req = Catalog().minimal_valid_request(  # doctest: +SKIP
                ...     "reanalysis-cerra-land",
                ... )
                >>> sorted(req.keys())  # doctest: +SKIP
                ['data_format', 'day', 'leadtime_hour', 'level_type', ...]

                ```
        """
        from earthlens.ecmwf.endpoints import constraints_base_url

        dataset = self.datasets.get(dataset_name)
        endpoint = dataset.endpoint if dataset is not None else "cds"
        constraints = fetch_constraints(dataset_name, constraints_base_url(endpoint))
        request: dict[str, Any] = {"data_format": "netcdf"}
        if not constraints:
            return request
        # Pick the first entry that has at least one variable —
        # entries with empty `variable` lists are dataset-form
        # placeholders that don't make a usable retrieve request.
        for entry in constraints:
            if entry.get("variable"):
                for key, value in entry.items():
                    if isinstance(value, list) and value:
                        request[key] = value[:1]
                    else:
                        request[key] = value
                return request
        # No entry had variables — fall back to the first one anyway
        # (some datasets identify the data column via an extra rather
        # than a `variable` list).
        first = constraints[0]
        for key, value in first.items():
            if isinstance(value, list) and value:
                request[key] = value[:1]
            else:
                request[key] = value
        return request

    def list_recent_jobs(
        self,
        status: str | None = None,
        max_age_min: int = 60,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return the user's recent CDS retrieval jobs.

        Thin wrapper that delegates to
        :func:`earthlens.ecmwf.jobs.list_recent_jobs` (N3); see that
        for the full docstring. Kept on `Catalog` as a convenience so
        `Catalog().list_recent_jobs(...)` keeps working.
        """
        return _list_recent_jobs_impl(
            status=status, max_age_min=max_age_min, limit=limit
        )

    def download_job(
        self,
        job_id: str,
        target: Path | str,
        chunk_size: int = 1 << 20,
    ) -> Path:
        """Download the result asset of a successful CDS job.

        Thin wrapper that delegates to
        :func:`earthlens.ecmwf.jobs.download_job` (N3); see that for
        the full docstring.
        """
        return _download_job_impl(job_id, target, chunk_size=chunk_size)
