"""Catalog loader for the JRC hazard backend (EFHM + sea-level forecasts).

The JRC datasets are few enough to live in one `jrc_data_catalog.yaml` at the
package root (rather than a sharded `catalog/` directory), even though they span
three access kinds: the EFHM return-period raster
row plus the three sea-level TWL forecast rows, each tagged with the `kind` the
backend dispatches on, and the shared CC-BY-4.0 licence / attribution. It loads
through the shared strict YAML loader and the `(path, mtime)` parse cache, and
exposes the rows via the inherited `AbstractCatalog` surface (`cat["efhm"]`,
`get_dataset`, the did-you-mean error).

`CATALOG_PATH` is the bundled YAML; `clear_catalog_cache` empties the parse
cache (used by tests that monkey-patch `CATALOG_PATH`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "jrc_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level JRC catalog parse cache."""
    _CATALOG_CACHE.clear()


class Dataset(BaseModel):
    """One JRC dataset row (EFHM, or a sea-level TWL forecast).

    Attributes:
        id: The catalog key (`"efhm"`, `"sea_level_medium_term"`, …).
        kind: Access-method + output discriminator the backend dispatches on —
            `"flood_hazard_raster"` (EFHM), `"sea_level_gridded"`, or
            `"sea_level_coastal"`.
        title: Human-readable product title.
        band: EFHM — the single water-depth band name (`"water_depth"`).
        long_name: Human-readable band description.
        units: Physical units of the values (`"m"`; empty for the tabular
            coastal summary, whose columns carry mixed units).
        dtype: Pixel data type (`"float32"`).
        crs: Native CRS as an EPSG string (`"EPSG:4326"`).
        nodata: The raster no-data value, or `None` when the source declares
            none (the sea-level cubes).
        spatial_resolution: Nominal resolution in metres.
        base_url: The JRC directory root the product's files live under.
        filename_template: The per-return-period file-name template
            (`"Europe_RP{rp}_filled_depth.tif"`).
        return_periods: The published return periods in years (EFHM only).
        source_url: The dataset landing page.
        attribution: The citation this row requires (per dataset; the EFHM row
            leans on the catalog-level attribution instead).
        product: Sea-level only — the jeodpp product subdirectory
            (`"medium_term_forecasts"` / `"subseasonal_forecasts"`).
        cycle_path_template: Sea-level only — `strftime` layout of the cycle
            folders under `product` (`"%Y/%m/%d/%H"`).
        gridded_glob: Sea-level only — glob for the gridded NetCDF in a cycle
            folder (`"*TWLforecastGridded_*.nc"`).
        coastal_glob: Sea-level only — glob for the coastal-summary CSV
            (`"*CoastalForecast_*.csv"`).
        cadence: Advisory cadence label (`"twice-daily"` / `"weekly"`).
        horizon_days: Nominal forecast horizon in days.
        endfls_marker: Name of the 0-byte cycle-complete sentinel (`"endFls"`).
        default_field: Sea-level gridded — the default variable cropped when a
            request names none (`"TWL75"`).
        doi: The dataset DOI (empty until confirmed with the data producer).
        data_period: Human-readable temporal coverage (`"2022-present"`).

    Examples:
        - Read the return periods and band:
            ```python
            >>> from earthlens.jrc import Catalog
            >>> row = Catalog().get("efhm")
            >>> row.band, row.return_periods[:3]
            ('water_depth', [10, 20, 30])

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Literal["flood_hazard_raster", "sea_level_gridded", "sea_level_coastal"] = (
        "flood_hazard_raster"
    )
    title: str = ""
    # EFHM-shaped defaults; the sea-level rows leave these unused.
    band: str = "water_depth"
    long_name: str = ""
    units: str = "m"
    dtype: str = "float32"
    crs: str = "EPSG:4326"
    nodata: float | None = -9999.0
    spatial_resolution: float | None = None
    base_url: str = ""
    filename_template: str = "Europe_RP{rp}_filled_depth.tif"
    return_periods: list[int] = Field(default_factory=list)
    source_url: str = ""
    attribution: str = ""
    # Sea-level forecast fields (unused by the static EFHM raster row).
    product: str = ""
    cycle_path_template: str = ""
    gridded_glob: str = ""
    coastal_glob: str = ""
    cadence: str = ""
    horizon_days: int | None = None
    endfls_marker: str = "endFls"
    default_field: str = ""
    doi: str = ""
    data_period: str = ""


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the JRC catalog YAML into `Catalog` construction kwargs.

    Args:
        files: The contributing YAML files (EFHM ships a single file).

    Returns:
        dict[str, Any]: Validated construction kwargs (`datasets`,
            `available_datasets`, `license_id`, `attribution`).

    Raises:
        ValueError: When the `datasets:` block is missing / empty or a row
            fails validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The JRC catalog must list the efhm product."
        )
    datasets: dict[str, Dataset] = {}
    for key, body in datasets_yaml.items():
        try:
            datasets[key] = Dataset(id=key, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} dataset {key!r} failed validation:\n{exc}"
            ) from exc
    return {
        "datasets": datasets,
        "available_datasets": sorted(datasets),
        "license_id": data.get("license", ""),
        "attribution": data.get("attribution", ""),
    }


class Catalog(AbstractCatalog):
    """Product catalog for the JRC hazard backend.

    Reads the bundled `jrc_data_catalog.yaml` and exposes its rows (the EFHM
    raster plus the sea-level TWL forecasts) under the inherited `datasets`
    field — which supplies the `cat["efhm"]` / `"efhm" in cat` / `len(cat)`
    surface and the did-you-mean error for free.
    Instantiate with no arguments; the base `model_post_init` auto-loads via
    `_autoload`, cached by `(path, mtime)`.

    Attributes:
        datasets: Product key to its `Dataset` row.
        available_datasets: Sorted product keys.
        license_id: SPDX-ish licence label (`"CC-BY-4.0"`, permissive).
        attribution: The citation the licence requires.

    Examples:
        - List products and read the licence:
            ```python
            >>> from earthlens.jrc import Catalog
            >>> cat = Catalog()
            >>> sorted(cat.datasets)
            ['efhm', 'sea_level_medium_term', 'sea_level_subseasonal', 'sea_level_subseasonal_coastal']
            >>> cat.license_id
            'CC-BY-4.0'

            ```
    """

    _catalog_kind: str = "JRC catalog"

    datasets: dict[str, Dataset] = Field(default_factory=dict)
    license_id: str = ""
    attribution: str = ""

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Return the disk payload to fill an empty catalog (base post-init hook).

        Returns:
            dict[str, Any]: The parsed field → value map from `_parse_catalog`.
        """
        return load_catalog(
            CATALOG_PATH, _CATALOG_CACHE, _parse_catalog, provider="JRC"
        )

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the JRC catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            Catalog: A fully-populated catalog.

        Raises:
            ValueError: If `catalog_path` does not exist, has no `datasets:`
                block, or the row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        payload = load_catalog(path, _CATALOG_CACHE, _parse_catalog, provider="JRC")
        return cls(**payload)

    def get(self, key: str) -> Dataset:
        """Return the `Dataset` for `key`, with a did-you-mean hint.

        Args:
            key: A product key (`"efhm"`).

        Returns:
            Dataset: The matching product row.

        Raises:
            ValueError: If `key` is not a registered product.
        """
        return cast("Dataset", self.get_dataset(key))
