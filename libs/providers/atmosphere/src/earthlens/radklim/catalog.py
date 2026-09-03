"""Product catalog for the DWD RADKLIM / RADOLAN radar-precipitation backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`radklim_data_catalog.yaml`. RADKLIM is two-stream: the reprocessed
climatology (`reproc`, one yearly NetCDF archive) and the operational
near-real-time stream (`operational`, per-timestamp HDF5 / binary granules),
each in an hourly (`RW`) and a 5-min (`YW`) product. The four products are the
"dataset" role — the key the user names in `dataset=` / `variables={dataset:
[...]}` — so they live under the inherited
:attr:`~earthlens.base.AbstractCatalog.datasets` field, which gives the
catalog its `cat["radklim-yw"]` / `"radklim-yw" in cat` / `len(cat)` dict-like
surface and the did-you-mean error for free.

:data:`CATALOG_PATH` is the path to the bundled YAML;
:func:`clear_catalog_cache` empties the `(path, mtime)` parse cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "radklim_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level RADKLIM catalog parse cache."""
    _CATALOG_CACHE.clear()


class RadklimProduct(BaseModel):
    """One RADKLIM / RADOLAN product row (the "dataset" analog).

    The product key (`"radklim-yw"`) is the parent key in
    :attr:`Catalog.datasets` and is repeated here as :attr:`product` so a row
    carries its own identity when passed around outside the catalog.

    Attributes:
        product: Product key — the value used in `dataset=` /
            `variables={product: [...]}`.
        stream: `"reproc"` (RADKLIM climatology, yearly NetCDF archive) or
            `"operational"` (RADOLAN near-real-time, per-timestamp granules).
        code: DWD product code — `"RW"` / `"YW"` for reproc (upper case, as it
            appears in the archive name), `"rw"` / `"yw"` for operational (lower
            case, as it appears in the URL path and granule name).
        cadence: Human-readable step (`"5-min"`, `"hourly"`).
        step_minutes: The cadence in minutes (`5` or `60`).
        cdc_frequency: The CDC grids-tree path token for a reproc product
            (`"5_minutes"` / `"hourly"`); empty for operational.
        version: Reprocessing version folder (`"2017_002"`); empty for
            operational.
        default_format: The format fetched when the request names none —
            `"nc"` (reproc archive), `"hdf5"` (operational).
        formats: The formats this product is served in (a subset of
            `nc` / `hdf5` / `bin`).
        data_period: Coverage as `start/` (or `start/end`); empty when rolling.
        retention_days: Approximate rolling-retention depth of the operational
            stream, in days; `0` for the full reproc archive.
        description: Human-readable summary.

    Examples:
        - Inspect a product's stream and format:
            ```python
            >>> from earthlens.radklim import Catalog
            >>> yw = Catalog().get_product("radklim-yw")
            >>> (yw.stream, yw.default_format, yw.step_minutes)
            ('reproc', 'nc', 5)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: str
    stream: Literal["reproc", "operational"]
    code: str
    cadence: str = ""
    step_minutes: int
    cdc_frequency: str = ""
    version: str = ""
    default_format: Literal["nc", "hdf5", "bin"]
    formats: list[str] = Field(default_factory=list)
    data_period: str = ""
    retention_days: int = 0
    description: str = ""


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the RADKLIM catalog YAML into `Catalog` construction kwargs.

    Args:
        files: The contributing YAML files (RADKLIM ships a single file).

    Returns:
        dict[str, Any]: The validated construction kwargs (`datasets`,
            `license`, `grid`).

    Raises:
        ValueError: If the `products:` block is missing / empty or a row fails
            validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    products_yaml = data.get("products") or {}
    if not products_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'products:' block. "
            "The RADKLIM catalog must list at least one product."
        )
    products: dict[str, RadklimProduct] = {}
    for key, body in products_yaml.items():
        try:
            products[key] = RadklimProduct(product=key, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} product {key!r} failed validation:\n{exc}"
            ) from exc
    return {
        "datasets": products,
        "license": data.get("license", ""),
        "grid": dict(data.get("grid") or {}),
    }


class Catalog(AbstractCatalog):
    """Catalog of DWD RADKLIM / RADOLAN products.

    Reads the bundled `radklim_data_catalog.yaml` (shipped as package data) and
    exposes its `products:` block as a map of :class:`RadklimProduct` rows keyed
    by product key under the inherited :attr:`datasets` field. Instantiate with
    no arguments (`Catalog()`); :func:`model_post_init` loads and validates the
    YAML in one pass and caches it by `(path, mtime)`.

    Attributes:
        datasets: Map from product key to its :class:`RadklimProduct` row.
        license: The DWD open-data licence string (`CC-BY-4.0/GeoNutzV`).
        grid: The fixed RADOLAN grid descriptor (`id` / `description`).
        available_datasets: Sorted product keys.

    Examples:
        - List products and resolve one:
            ```python
            >>> from earthlens.radklim import Catalog
            >>> cat = Catalog()
            >>> cat.products()
            ['radklim-rw', 'radklim-yw', 'radolan-rw', 'radolan-yw']
            >>> cat.get_product("radolan-yw").retention_days
            2
            >>> cat.license
            'CC-BY-4.0/GeoNutzV'

            ```
        - An unknown product raises with a did-you-mean hint:
            ```python
            >>> from earthlens.radklim import Catalog
            >>> Catalog().get_product("radklim-y")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'radklim-y' is not in the RADKLIM catalog. Known datasets: [...]. Did you mean 'radklim-yw'?

            ```
    """

    _catalog_kind: str = "RADKLIM catalog"

    datasets: dict[str, RadklimProduct] = Field(default_factory=dict)
    license: str = ""
    grid: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no products were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached by
        `(path, mtime)`); passing `datasets=...` skips the disk read (used in
        tests). Either way the `available_datasets` index is derived from the
        loaded map.

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is missing,
                empty, or has a malformed row.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
            self.license = loaded.license
            self.grid = loaded.grid
        self.available_datasets = sorted(self.datasets)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the RADKLIM catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `products:` block, or any product row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        payload = load_catalog(path, _CATALOG_CACHE, _parse_catalog, provider="RADKLIM")
        return cls(**payload)

    def get_product(self, key: str) -> RadklimProduct:
        """Return the :class:`RadklimProduct` for `key`, with a did-you-mean hint.

        Thin alias over :meth:`~earthlens.base.AbstractCatalog.get_dataset`.

        Args:
            key: A product key (`"radklim-yw"`, `"radolan-rw"`).

        Returns:
            RadklimProduct: The matching product row.

        Raises:
            ValueError: If `key` is not a registered RADKLIM product.
        """
        return cast("RadklimProduct", self.get_dataset(key))

    def products(self) -> list[str]:
        """Return the registered product keys, sorted.

        Returns:
            list[str]: The product keys.
        """
        return sorted(self.datasets)
