"""Dataset-catalog loader for the NWP backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`nwp_data_catalog.yaml`. Mirrors the shape of
:mod:`earthlens.ecmwf.catalog` and :mod:`earthlens.gee.catalog`: a
single YAML file with a top-level `datasets:` block keyed by model key
(`gfs`, `gefs`, `hrrr`, `ifs-hres`, `icon-global`, …). Each block
parses into an :class:`NWPModel` carrying the provider, the forecast
cadence (`cycles_utc` / `horizon_h`), the download `backend`
(`herbie` / `ecmwf-opendata` / `direct-https` / `direct-boto3`), the
cloud `mirrors`, the direct-centre `url_template`, and the
`param → selector` band map.

A model key resolves to an :class:`NWPModel` via
:meth:`Catalog.get_model` / :meth:`Catalog.resolve` /
`Catalog()["..."]`, each with a did-you-mean hint on a miss (inherited
from :class:`earthlens.base.AbstractCatalog`). The path to the bundled
YAML lives at :data:`CATALOG_PATH`; tests can monkey-patch that module
attribute to redirect the loader at a temporary file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "nwp_data_catalog.yaml"

# Module-level cache keyed on `(resolved_path, mtime_ns)` so editing the
# YAML invalidates the entry without re-parsing on every construction.
_CATALOG_CACHE: dict[Any, dict[str, NWPModel]] = CatalogParseCache()

#: The download backends an `NWPModel` can declare. `herbie` and
#: `ecmwf-opendata` go through their SDKs (`.idx` byte-range subsetting);
#: `direct-https` / `direct-boto3` are earthlens' own per-centre modules
#: for providers Herbie does not cover (DWD, Météo-France, ECCC).
BackendLiteral = Literal[
    "herbie",
    "ecmwf-opendata",
    "direct-https",
    "direct-boto3",
    "meteofrance-api",
    "eccc-msc",
]

#: The set of backends earthlens knows how to dispatch. Used by the
#: backend to fail fast on an unknown `backend:` value at construction.
KNOWN_BACKENDS: frozenset[str] = frozenset(
    (
        "herbie",
        "ecmwf-opendata",
        "direct-https",
        "direct-boto3",
        "meteofrance-api",
        "eccc-msc",
    )
)


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force
    a re-parse. Production callers do not need this — the cache key
    includes the file's `st_mtime_ns`, so any real mutation invalidates
    the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> dict[str, NWPModel]:
    """Parse, validate, and cache the NWP catalog at `path`.

    Args:
        path: Path to `nwp_data_catalog.yaml` (or a test override).

    Returns:
        dict[str, NWPModel]: The curated `datasets:` map, keyed by
            model key.

    Raises:
        ValueError: If the file has no `datasets:` block or a model row
            fails validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The catalog must contain at least one model."
        )
    models: dict[str, NWPModel] = {}
    for model_key, body in datasets_yaml.items():
        try:
            models[model_key] = NWPModel(**(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} model {model_key!r} failed validation:\n{exc}"
            ) from exc
    _CATALOG_CACHE[key] = models
    return models


class NWPModel(BaseModel):
    """One curated NWP model row.

    Mirrors a single `datasets.<key>:` block in
    `nwp_data_catalog.yaml`. The model key itself is the parent key in
    :attr:`Catalog.datasets` and is not stored on the row.

    Attributes:
        provider: Provider slug (e.g. `"noaa-nodd"`, `"dwd-opendata"`,
            `"ecmwf-opendata"`, `"meteofrance"`).
        model_family: Herbie model family token (e.g. `"gfs"`,
            `"hrrr"`, `"ifs"`); empty for direct-centre models.
        resolution: Native horizontal resolution, advisory (e.g.
            `"0.25deg"`, `"13km"`).
        cycles_utc: The model's daily run hours, in `[0, 23]` (e.g.
            `[0, 6, 12, 18]`).
        horizon_h: Maximum forecast lead time in hours.
        cadence_h: Spacing of the run hours in `cycles_utc`, advisory.
        step_cadence_h: Spacing between published forecast steps, in
            hours, used to expand a `horizon=` request (e.g. `3` for a
            model whose steps are f000/f003/f006/…). Approximate — a
            model with a mixed grid (hourly then 3-hourly) is coarsened
            to a single cadence; the `errors="warn"` fetch policy skips
            any expanded step the model doesn't actually carry. Defaults
            to `1` (hourly).
        product: Herbie product token, required for some models (e.g.
            HRRR `"wrfsfcf"`); `None` otherwise.
        format: On-disk format the fetch produces (`"grib2"`).
        idx: Whether the source exposes a `.idx` byte-range index (NOAA
            / ECMWF yes; DWD no — its files are already per-variable).
        backend: Which download path handles this model (see
            :data:`BackendLiteral`).
        mirrors: Ordered list of cloud-mirror keys the model is served
            from (e.g. `["aws", "google", "azure"]`).
        url_template: For `direct-*` backends, a `str.format` template
            with `{cycle}` / `{date}` / `{step}` / `{var}` / `{var_lc}`
            fields. `None` for SDK-backed models.
        bands: Map from earthlens parameter name to the centre's
            selector — a Herbie `search` regex (`":TMP:2 m above
            ground:"`) for SDK models, or a provider var token
            (`"T_2M"`) for direct ones.
        members: Ensemble member ids, when the model is an ensemble
            (empty for deterministic models). The first entry is the
            default representative fetched when no `members=` is given
            (e.g. `"mean"` for GEFS, `"control"` for ENS). Each centre
            maps an id to its SDK's member selector (Herbie `member=`
            for GEFS; `type=pf` + `number=` for ECMWF ENS).
        request_options: Free-form per-centre extras the adapter splats
            into its request. ECMWF Open Data uses `ecmwf_model` /
            `stream` / `type` (e.g. `{"ecmwf_model": "aifs-single"}` for
            AIFS, `{"stream": "enfo", "type": "cf"}` for the ENS control);
            an unsigned-S3 centre uses `bucket` / `key_template` /
            `region`. Empty for the simple deterministic models.
        license: SPDX-style licence identifier the provider publishes the
            data under, surfaced as catalog metadata so downstream users
            and redistribution honour it. Never inferred from the URL —
            populated row-by-row in `nwp_data_catalog.yaml` from the
            provider's stated terms (`"PD-US-GOV"` for NOAA NODD;
            `"CC-BY-4.0"` for ECMWF Open Data and DWD; `"Etalab-2.0"`
            for Météo-France; `"OGL-Canada-2.0"` for ECCC). `None` only
            for an ad-hoc row that has not been curated yet.
        retention_days: How long the provider keeps a cycle online before
            it rolls off the live endpoint. `None` means archival or
            unspecified (the backend stays silent); a positive integer is
            the rolling-window length in days, and `NWP.__init__` emits a
            :class:`RetentionWarning` when the requested `start` is older
            than `now - retention_days` (the #1 confusing failure for
            short-retention providers — DWD keeps roughly one day, MF
            fourteen). Never inferred — populated per row from the
            provider's stated retention policy.
        grid_kind: The model's native horizontal grid type. Defaults to
            `"regular-latlon"` (every NOAA / ECMWF / DWD-ICON-EU / ECCC
            row); `"icosahedral"` flags an unstructured DWD ICON grid
            (icon-global, icon-d2, icon-eps, icon-eu-eps, icon-d2-eps).
            `NWP._aggregate` checks this field — not the URL — to refuse
            aggregation on a grid `pyramids.dataset.DatasetCollection`
            cannot co-register.
        title: Short human-readable label (e.g. `"NOAA GFS (Global
            Forecast System)"`). Surfaced as the `title` column in the
            federated `earthlens datasets where / search / list` CLI
            output, so an `nwp` row reads the same as its `gee` / `s3` /
            `radar` siblings instead of a blank cell. `None` for an
            ad-hoc row.
        description: One-sentence summary of the model — provider,
            domain, resolution, and forecast horizon. Backs the CLI's
            title fallback and gives `datasets search` free-text a
            richer field to match against. `None` for an ad-hoc row.

    Examples:
        - Build a minimal Herbie-backed row and read its selector:
            ```python
            >>> from earthlens.nwp import NWPModel
            >>> row = NWPModel(
            ...     provider="noaa-nodd",
            ...     backend="herbie",
            ...     cycles_utc=[0, 12],
            ...     bands={"temperature_2m": ":TMP:2 m above ground:"},
            ... )
            >>> row.backend
            'herbie'
            >>> row.bands["temperature_2m"]
            ':TMP:2 m above ground:'

            ```
        - Optional fields fall back to documented defaults:
            ```python
            >>> from earthlens.nwp import NWPModel
            >>> row = NWPModel(provider="dwd-opendata")
            >>> row.format, row.idx, row.cycles_utc
            ('grib2', True, [])

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_family: str = ""
    resolution: str = ""
    cycles_utc: list[int] = Field(default_factory=list)
    horizon_h: int = 0
    cadence_h: int | None = None
    step_cadence_h: int = 1
    product: str | None = None
    format: str = "grib2"
    idx: bool = True
    backend: BackendLiteral = "herbie"
    mirrors: list[str] = Field(default_factory=list)
    url_template: str | None = None
    bands: dict[str, str] = Field(default_factory=dict)
    request_options: dict[str, Any] = Field(default_factory=dict)
    members: list[str] = Field(default_factory=list)
    license: str | None = None
    retention_days: int | None = None
    grid_kind: Literal["regular-latlon", "icosahedral"] = "regular-latlon"
    title: str | None = None
    description: str | None = None


class Catalog(AbstractCatalog[NWPModel]):
    """Model catalog for the NWP backend.

    Reads the bundled `nwp_data_catalog.yaml` (shipped as package data)
    and exposes its `datasets:` block as a typed `dict[str, NWPModel]`.
    Instantiate with no arguments (`Catalog()`) —
    :func:`model_post_init` parses the YAML and populates
    :attr:`datasets` in one pass.

    Attributes:
        datasets: Structural map keyed by the model key; each value is
            an :class:`NWPModel`.

    Examples:
        - Load the bundled catalog and check which models are present:
            ```python
            >>> from earthlens.nwp import Catalog
            >>> cat = Catalog()
            >>> "gfs" in cat and "icon-eu" in cat and "aifs" in cat
            True

            ```
        - Resolve one model and read its download backend:
            ```python
            >>> from earthlens.nwp import Catalog
            >>> Catalog().get_model("icon-global").backend
            'direct-https'

            ```
    """

    _catalog_kind: str = "NWP catalog"

    datasets: dict[str, NWPModel] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets` read from
                the bundled catalog.
        """
        return {"datasets": _load_catalog_data(CATALOG_PATH)}

    def get_model(self, model_key: str) -> NWPModel:
        """Resolve a model key to its :class:`NWPModel` row.

        Args:
            model_key: A curated model key (e.g. `"gfs"`,
                `"icon-global"`).

        Returns:
            NWPModel: The resolved row.

        Raises:
            ValueError: When `model_key` is unknown (with a
                did-you-mean hint from the base class).

        Examples:
            - Resolve a known model:
                ```python
                >>> from earthlens.nwp import Catalog
                >>> Catalog().get_model("gfs").horizon_h
                384

                ```
            - A typo raises with a did-you-mean hint:
                ```python
                >>> from earthlens.nwp import Catalog
                >>> Catalog().get_model("gffs")  # doctest: +ELLIPSIS
                Traceback (most recent call last):
                    ...
                ValueError: 'gffs' is not in the NWP catalog. Known datasets: [...]. Did you mean 'gfs'?

                ```
        """
        return cast("NWPModel", self.get_dataset(model_key))

    def resolve(self, model_key: str) -> NWPModel:
        """Alias for :meth:`get_model` (matches the other backends' surface)."""
        return self.get_model(model_key)
