"""Product catalog for the GloH2O MSWEP / MSWX backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`mswep_data_catalog.yaml`. A concrete Drive path needs four coordinates
— product, version (which fixes the **root folder**), variant, and
temporal resolution — plus, for MSWX only, a **variable** level MSWEP
does not have. The catalog therefore keeps a per-product
:attr:`MswepProduct.path_template` rather than one shared template, so
the two shapes stay explicit:

* MSWEP — `{root}/{variant}/{temporal}/{stem}.nc`
* MSWX — `{root}/{variant}/{variable}/{temporal}/{stem}.nc`

**Provisional rows.** The `provisional` flag guards a value that could
not be verified without an approved GloH2O Drive share. A flagged row
surfaces as :attr:`MswepVersion.provisional` /
:attr:`MswepVariable.provisional` / :attr:`MswepVariant.provisional` /
:attr:`MswepResolution.provisional`, and
:meth:`Catalog.check_not_provisional` refuses it, so a request built on
an unverified constant fails loudly instead of resolving to a Drive path
that does not exist and degrading into an empty download. The share has
since been walked, so the shipped `mswep_data_catalog.yaml` currently
marks **nothing** provisional; the guard stays in place for any value
added unverified in future.

:data:`CATALOG_PATH` is the path to the bundled YAML;
:func:`clear_catalog_cache` empties the `(path, mtime)` parse cache.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "mswep_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level MSWEP catalog parse cache."""
    _CATALOG_CACHE.clear()


class ProvisionalValueError(ValueError):
    """Raised when a request resolves onto an unverified catalog value.

    A value the catalog cannot confirm without an approved GloH2O Drive
    share is marked `provisional`, and resolving against it raises this
    rather than letting `files.list` quietly return nothing (which the
    missing-granule path would log and skip, yielding a silently partial
    time series). The share has since been walked, so the shipped catalog
    currently marks nothing provisional; this guard remains for any value
    added unverified later.
    """


class MswepVersion(BaseModel):
    """One dataset version and the Drive root folder it lives in.

    Roots are version-stamped and coexist in the share (`MSWEP_V280`
    beside `MSWEP_V315`), so the version is a user-facing selector, not
    a constant. This matters beyond tidiness: GloH2O recommends V2.80
    over V3.15/V3.16 for trend analysis, because the newer versions
    show artificially low precipitation over 2000-2015.

    Attributes:
        version: The version key (`"2.80"`, `"3.15"`).
        root: The Drive root folder name (`"MSWEP_V280"`).
        description: Human-readable note, e.g. a known-defect warning.
        provisional: `True` when the root name is unverified.

    Examples:
        - Read the root folder for a version:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> Catalog().get_product("mswep").versions["2.80"].root
            'MSWEP_V280'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = ""
    root: str
    description: str = ""
    provisional: bool = False


class MswepVariant(BaseModel):
    """One data variant and its window.

    Two kinds exist. An **analysis** variant (`Past`, `Past_nogauge`,
    `NRT`) is one granule per valid time, addressed by the product's
    `path_template`, and is date-constrained: MSWEP's `Past` variants
    end 2024-12-31 and `NRT` starts 2025-01-01, so the requested window
    decides which variant can serve it.

    A **forecast** variant (MSWX's medium-range and seasonal ensembles)
    is not addressable by that template at all: a forecast granule is
    identified by an initialisation time, a lead time **and** an
    ensemble member, none of which the analysis template carries. Those
    variants therefore record what is known about the stream —
    :attr:`members`, :attr:`horizon`, :attr:`base_model`,
    :attr:`update_cadence` — and stay `provisional` until the real
    layout is read off an approved share, rather than shipping a guessed
    path shape that would resolve to nothing.

    Attributes:
        variant: The variant key, which is also its Drive folder name.
        kind: `"analysis"` (one granule per valid time) or `"forecast"`
            (ensemble, addressed by init time + lead + member).
        description: Human-readable summary.
        start: First date the variant covers, or `None` if unbounded.
        end: Last date the variant covers, or `None` for "to real time".
        members: Ensemble size for a forecast variant; `0` for analysis.
        horizon: Human-readable forecast length (`"10 days"`).
        base_model: The NWP system the stream is bias-corrected from
            (`"GEFS"`, `"SEAS5"`).
        update_cadence: How often the stream is re-initialised.
        notes: What still has to be pinned before the variant is usable.
        provisional: `True` when the window or layout is unverified.

    Examples:
        - MSWEP's gauge-corrected history ends with 2024:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> Catalog().get_product("mswep").variants["Past"].end
            datetime.date(2024, 12, 31)

            ```
        - MSWX's seasonal stream (`Long`) records its ensemble size:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> seasonal = Catalog().get_product("mswx").variants["Long"]
            >>> seasonal.kind, seasonal.members, seasonal.base_model
            ('forecast', 51, 'SEAS5')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    variant: str = ""
    kind: Literal["analysis", "forecast"] = "analysis"
    description: str = ""
    start: dt.date | None = None
    end: dt.date | None = None
    members: int = 0
    horizon: str = ""
    base_model: str = ""
    update_cadence: str = ""
    notes: str = ""
    provisional: bool = False

    @property
    def is_forecast(self) -> bool:
        """Return whether this variant is an ensemble forecast stream."""
        return self.kind == "forecast"

    def covers(self, day: dt.date) -> bool:
        """Return whether `day` falls inside the variant's window.

        Args:
            day: The date to test.

        Returns:
            bool: `True` when `day` is within `[start, end]`; an unset
                bound is treated as open.
        """
        if self.start is not None and day < self.start:
            return False
        return not (self.end is not None and day > self.end)


class MswepResolution(BaseModel):
    """One temporal resolution: its Drive folder and file-name stem.

    Attributes:
        resolution: The resolution key (`"hourly"`, `"daily"`).
        folder: The Drive folder name — note the upstream's mixed
            casing (`Hourly`, `3hourly`, `Daily`, `Monthly`).
        stem: `strftime` pattern for the file name without `.nc`
            (`"%Y%j.%H"` for hourly/3-hourly, `"%Y%j"` daily,
            `"%Y%m"` monthly).
        step: ISO-8601 duration between granules (`"PT3H"`, `"P1D"`).
        units: Physical units of the accumulation at this resolution.
        provisional: `True` when the folder name is unverified.

    Examples:
        - Daily granules are named by day-of-year:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> daily = Catalog().get_product("mswep").resolutions["daily"]
            >>> daily.folder, daily.stem, daily.units
            ('Daily', '%Y%j', 'mm/day')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolution: str = ""
    folder: str
    stem: str
    step: str
    units: str = ""
    provisional: bool = False


class MswepVariable(BaseModel):
    """One requestable variable.

    For MSWEP this is the single `precipitation` field. For MSWX the key
    is a **Drive folder name** (`Temp`), because MSWX shards by variable
    one level above the temporal folder.

    Attributes:
        variable: The variable key / MSWX folder name.
        netcdf_field: Name of the field inside the granule.
        long_name: Human-readable description.
        dims: Grid dimensions, when known (`[1800, 3600]`).
        provisional: `True` when the folder spelling is unverified.

    Examples:
        - MSWEP carries one variable:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> list(Catalog().get_product("mswep").variables)
            ['precipitation']

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    variable: str = ""
    netcdf_field: str = ""
    long_name: str = ""
    dims: list[int] = Field(default_factory=list)
    provisional: bool = False


class GaugeMetadataFile(BaseModel):
    """One auxiliary gauge-metadata CSV.

    Attributes:
        name: The file name, as it appears in the share.
        description: What the file contains, from the MSWEP documentation.

    Examples:
        - Read what a file holds:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> row = Catalog().gauge_metadata.files["daily_station_locations.csv"]
            >>> "Latitude" in row.description
            True

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    description: str = ""


class GaugeMetadata(BaseModel):
    """The `Gauge_metadata` folder and the CSVs inside it.

    Describes the rain gauges behind MSWEP's gauge-correction step. The
    documentation names the folder and every file, but not the folder's
    **parent** — it reads "included in the `Gauge_metadata` folder". The
    v3.16 share carries it directly under the version root (the shared
    `folder_id`), alongside `Past` / `NRT`, so the backend looks for it
    there in a single lookup.

    Attributes:
        folder: The folder name (`"Gauge_metadata"`).
        files: File name to its :class:`GaugeMetadataFile` row.

    Examples:
        - The five documented files are registered:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> len(Catalog().gauge_metadata.files)
            5

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    folder: str = "Gauge_metadata"
    files: dict[str, GaugeMetadataFile] = Field(default_factory=dict)


class MswepProduct(BaseModel):
    """One product (`mswep` or `mswx`) and everything needed to path it.

    Attributes:
        product: The product key.
        description: Human-readable summary.
        path_template: Per-product **analysis** Drive path shape. MSWX
            carries a `{variable}` placeholder MSWEP does not.
        forecast_path_template: The **forecast** path shape, when the
            product has forecast variants (MSWX). It carries `{init}` and
            `{member}` levels — a forecast granule is keyed by
            initialisation time and ensemble member as well as
            variable / temporal / valid-time.
        default_version: Version used when the caller names none.
        versions: Version key to :class:`MswepVersion`.
        variants: Variant key to :class:`MswepVariant`.
        resolutions: Resolution key to :class:`MswepResolution`.
        variables: Variable key to :class:`MswepVariable`.

    Examples:
        - MSWX's analysis path shape has the extra variable level:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> Catalog().get_product("mswx").path_template
            '{root}/{variant}/{variable}/{temporal}/{stem}.nc'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: str
    description: str = ""
    path_template: str
    forecast_path_template: str = ""
    default_version: str
    versions: dict[str, MswepVersion] = Field(default_factory=dict)
    variants: dict[str, MswepVariant] = Field(default_factory=dict)
    resolutions: dict[str, MswepResolution] = Field(default_factory=dict)
    variables: dict[str, MswepVariable] = Field(default_factory=dict)

    @property
    def needs_variable_folder(self) -> bool:
        """Return whether the analysis path template carries a `{variable}` level."""
        return "{variable}" in self.path_template

    def variant_for(self, day: dt.date) -> str | None:
        """Return the first variant whose window covers `day`.

        Args:
            day: The date to place.

        Returns:
            str | None: The variant key, or `None` when no variant
                covers the date.
        """
        for key, variant in self.variants.items():
            if variant.covers(day):
                return key
        return None


def _rows(
    body: dict[str, Any],
    block: str,
    model: type[BaseModel],
    id_field: str,
    path: Path,
    product: str,
) -> dict[str, Any]:
    """Validate one keyed block of a product into its row model.

    Args:
        body: The product's YAML body.
        block: Block name (`"versions"`, `"variants"`, …).
        model: The pydantic row model to build.
        id_field: Field on the model that repeats the key.
        path: Catalog path, quoted in errors.
        product: Product key, quoted in errors.

    Returns:
        dict[str, Any]: Key to validated row.

    Raises:
        ValueError: When a row fails validation.
    """
    out: dict[str, Any] = {}
    for key, row in (body.get(block) or {}).items():
        try:
            out[key] = model(**{id_field: key}, **dict(row or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} product {product!r} {block} {key!r} failed validation:\n{exc}"
            ) from exc
    return out


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the MSWEP catalog YAML into `Catalog` construction kwargs.

    Args:
        files: The contributing YAML files (MSWEP ships a single file).

    Returns:
        dict[str, Any]: Validated construction kwargs. The payload is
            cached rather than a built Catalog, so each `load()` yields a
            fresh instance and one caller mutating its `datasets` map
            cannot reach another's. The frozen row objects inside are
            shared; treat them as read-only.

    Raises:
        ValueError: When the `products:` block is missing or empty, or a
            row fails validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    products_yaml = data.get("products") or {}
    if not products_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'products:' block. "
            "The MSWEP catalog must list at least one product."
        )

    products: dict[str, MswepProduct] = {}
    for key, body in products_yaml.items():
        body = dict(body or {})
        try:
            products[key] = MswepProduct(
                product=key,
                description=body.get("description", ""),
                path_template=body["path_template"],
                forecast_path_template=body.get("forecast_path_template", ""),
                default_version=body["default_version"],
                versions=_rows(body, "versions", MswepVersion, "version", path, key),
                variants=_rows(body, "variants", MswepVariant, "variant", path, key),
                resolutions=_rows(
                    body, "resolutions", MswepResolution, "resolution", path, key
                ),
                variables=_rows(
                    body, "variables", MswepVariable, "variable", path, key
                ),
            )
        except KeyError as exc:
            raise ValueError(
                f"{path} product {key!r} is missing required field {exc}."
            ) from exc
        except ValidationError as exc:
            raise ValueError(
                f"{path} product {key!r} failed validation:\n{exc}"
            ) from exc

    gauge_yaml = dict(data.get("gauge_metadata") or {})
    gauge_files: dict[str, GaugeMetadataFile] = {}
    for name, row in (gauge_yaml.get("files") or {}).items():
        try:
            gauge_files[name] = GaugeMetadataFile(name=name, **dict(row or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} gauge_metadata file {name!r} failed validation:\n{exc}"
            ) from exc

    return {
        "gauge_metadata": GaugeMetadata(
            folder=gauge_yaml.get("folder", "Gauge_metadata"), files=gauge_files
        ),
        "datasets": products,
        "license_id": data.get("license", ""),
        "attribution": data.get("attribution", ""),
        "nrt_revision_days": int(data.get("nrt_revision_days", 0)),
        "granule_warn_threshold": int(data.get("granule_warn_threshold", 0)),
    }


class Catalog(AbstractCatalog[MswepProduct]):
    """Product catalog for the MSWEP / MSWX backend.

    Reads the bundled `mswep_data_catalog.yaml` (shipped as package
    data) and exposes its `products:` block as :class:`MswepProduct`
    rows under the inherited :attr:`datasets` field — which supplies the
    `cat["mswep"]` / `"mswep" in cat` / `len(cat)` surface and the
    did-you-mean error for free. Instantiate with no arguments;
    `model_post_init` loads and validates the YAML once, cached by
    `(path, mtime)`.

    Attributes:
        datasets: Product key to its :class:`MswepProduct` row.
        available_datasets: Sorted product keys.
        license_id: SPDX-ish licence label (`"CC-BY-NC"`), fed to
            `warn_license`.
        attribution: The citation the licence requires downstream
            products to carry.
        nrt_revision_days: Trailing window in which NRT granules are
            still being revised upstream, so a cached copy is stale.
        granule_warn_threshold: Granule count above which a request
            warns and points at rclone for bulk transfers.

    Examples:
        - List products and read the licence:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> cat = Catalog()
            >>> cat.products()
            ['mswep', 'mswx']
            >>> cat.license_id
            'CC-BY-NC'
            >>> cat.nrt_revision_days
            10

            ```
        - An unknown product raises with a did-you-mean hint:
            ```python
            >>> from earthlens.mswep import Catalog
            >>> Catalog().get_product("mswpe")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'mswpe' is not in the MSWEP catalog. Known datasets: [...]. Did you mean 'mswep'?

            ```
    """

    _catalog_kind: str = "MSWEP catalog"

    datasets: dict[str, MswepProduct] = Field(default_factory=dict)
    gauge_metadata: GaugeMetadata = Field(default_factory=GaugeMetadata)
    license_id: str = ""
    attribution: str = ""
    nrt_revision_days: int = 0
    granule_warn_threshold: int = 0

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no products were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached by
        `(path, mtime)`); passing `datasets=...` skips the disk read.

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is
                missing, empty, or has a malformed row.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
            self.gauge_metadata = loaded.gauge_metadata
            self.license_id = loaded.license_id
            self.attribution = loaded.attribution
            self.nrt_revision_days = loaded.nrt_revision_days
            self.granule_warn_threshold = loaded.granule_warn_threshold
        self.available_datasets = sorted(self.datasets)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the MSWEP catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            Catalog: A fully-populated catalog.

        Raises:
            ValueError: If `catalog_path` does not exist, has no
                `products:` block, or any row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        payload = load_catalog(path, _CATALOG_CACHE, _parse_catalog, provider="MSWEP")
        return cls(**payload)

    def get_product(self, key: str) -> MswepProduct:
        """Return the :class:`MswepProduct` for `key`, with a did-you-mean hint.

        Args:
            key: A product key (`"mswep"`, `"mswx"`).

        Returns:
            MswepProduct: The matching product row.

        Raises:
            ValueError: If `key` is not a registered product.
        """
        return cast("MswepProduct", self.get_dataset(key))

    def products(self) -> list[str]:
        """Return the registered product keys, sorted.

        Returns:
            list[str]: `["mswep", "mswx"]`.
        """
        return sorted(self.datasets)

    @staticmethod
    def check_not_provisional(row: BaseModel, what: str) -> None:
        """Raise when a catalog row is an unverified placeholder.

        Guards the four constants that could not be confirmed without an
        approved GloH2O share. Resolving against one would build a Drive
        path that does not exist — and because a missing granule is
        logged and skipped, the request would return a silently partial
        result instead of failing.

        Args:
            row: The catalog row to check.
            what: Description of the row, quoted in the error.

        Raises:
            ProvisionalValueError: When `row.provisional` is `True`.
        """
        if not getattr(row, "provisional", False):
            return
        raise ProvisionalValueError(
            f"{what} is marked provisional in the MSWEP catalog: its value "
            "could not be verified without an approved GloH2O Drive share, so "
            "resolving against it would build a path that does not exist and "
            "return an empty result. Confirm the real value inside your share "
            "and drop `provisional: true` from mswep_data_catalog.yaml."
        )
