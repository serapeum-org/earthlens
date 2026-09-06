"""Product catalog for the PVGIS backend.

PVGIS exposes several *tools* under one keyless REST base
(`https://re.jrc.ec.europa.eu/api/v5_3/<tool>`); a request selects one via
`variables=["seriescalc"]` / `["tmy"]`. This module is the bridge from that
product id to the concrete request shape: the tool name, the endpoint
segment, the per-product default query params, and the canonical value
columns the parser keeps.

`Catalog` is a thin `earthlens.base.AbstractCatalog` subclass that loads the
bundled `pvgis_data_catalog.yaml` and exposes each row as a `Product`.
Resolve one with `get` (a did-you-mean hint on an unknown id) and list the
ids with `available`.

`CATALOG_PATH` is the path to the bundled YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "pvgis_data_catalog.yaml"

#: Module-level cache of parsed catalog rows, keyed on the resolved path
#: plus the YAML's `st_mtime_ns`, so editing the file invalidates the entry
#: without re-parsing on every `Catalog()`. Mirrors the sibling loaders.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Product]] = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key includes
    the file's `st_mtime_ns`, so any real edit invalidates the entry.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> dict[str, Product]:
    """Parse, validate, and cache the product catalog at `path`.

    Args:
        path: Path to the catalog YAML (default `CATALOG_PATH`).

    Returns:
        Mapping from product id to its `Product` row.

    Raises:
        ValueError: If the file has no `products:` block, or a row fails
            `Product` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    products_yaml = data.get("products") or {}
    if not products_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'products:' block. "
            "The PVGIS catalog must list at least one product."
        )
    rows: dict[str, Product] = {}
    for name, body in products_yaml.items():
        try:
            rows[name] = Product(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} product {name!r} failed validation:\n{exc}"
            ) from exc

    _CATALOG_CACHE[key] = rows
    return rows


class Product(BaseModel):
    """One PVGIS tool's catalog row.

    The product id is the parent key in `Catalog.datasets`; the row carries
    everything the backend needs to shape a request for it.

    Attributes:
        tool: The PVGIS tool name (`"seriescalc"`, `"tmy"`).
        endpoint: The REST endpoint segment appended to the base URL —
            usually identical to `tool`.
        default_params: Per-product default query params, merged *under* the
            caller's backend kwargs (so a caller override always wins).
        columns: The canonical value columns the parser keeps, including the
            `time` column.
        description: Human-readable summary of the product.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.pvgis import Product
            >>> p = Product(tool="seriescalc", endpoint="seriescalc",
            ...             columns=["time", "G(i)"])
            >>> p.tool
            'seriescalc'
            >>> p.default_params
            {}

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    endpoint: str
    default_params: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    description: str = ""


class Catalog(AbstractCatalog[Product]):
    """Product catalog for the PVGIS backend.

    Reads the bundled `pvgis_data_catalog.yaml` (shipped as package data) and
    exposes its `products:` block as a map of `Product` rows keyed by product
    id. Instantiate with no arguments (`Catalog()`); resolve a row with
    `get`, or list the ids with `available`.

    Attributes:
        datasets: Map from product id to its `Product` row.

    Examples:
        - Resolve a product and read its tool:
            ```python
            >>> from earthlens.pvgis import Catalog
            >>> Catalog().get("seriescalc").tool
            'seriescalc'

            ```
        - An unknown but close id raises with a did-you-mean hint:
            ```python
            >>> from earthlens.pvgis import Catalog
            >>> Catalog().get("seriescal")
            Traceback (most recent call last):
                ...
            ValueError: 'seriescal' is not in the PVGIS product catalog. Known products: ['seriescalc', 'tmy']. Did you mean 'seriescalc'?

            ```
    """

    _catalog_kind: str = "PVGIS product catalog"
    _entry_noun: str = "products"

    #: The product rows live in the base `datasets` field so the inherited
    #: dict surface (`len`, `in`, `[]`, iteration) and `get_dataset`'s
    #: did-you-mean hint work unchanged; narrowed here to `Product` values.
    datasets: dict[str, Product] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_products_alias(cls, data: Any) -> Any:
        """Accept the `products=` kwarg as an alias for `datasets`.

        Lets callers (and tests) construct `Catalog(products={...})` while
        the rows live in the base `datasets` field. An explicit `datasets=`
        always wins.

        Args:
            data: The raw model input (a mapping for keyword construction).

        Returns:
            The input with `products` renamed to `datasets`, untouched
            otherwise.
        """
        if isinstance(data, dict) and "products" in data and "datasets" not in data:
            data = dict(data)
            data["datasets"] = data.pop("products")
        return data

    @property
    def products(self) -> dict[str, Product]:
        """The product map — alias for the base `datasets` field.

        Returns:
            dict[str, Product]: The same mapping stored in `datasets`.
        """
        return self.datasets

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets` read from
                the bundled catalog.
        """
        return {"datasets": Catalog.load().datasets}

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the PVGIS product catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If the file has no `products:` block, or a row fails
                `Product` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(datasets=dict(_load_catalog_data(catalog_path)))

    def available(self) -> list[str]:
        """The sorted list of curated product ids.

        Returns:
            list[str]: Every curated catalog key, sorted.

        Examples:
            - List the shipped products:
                ```python
                >>> from earthlens.pvgis import Catalog
                >>> Catalog().available()
                ['seriescalc', 'tmy']

                ```
        """
        return sorted(self.datasets)

    def get(self, product: str) -> Product:
        """Resolve a product id to its `Product` row.

        Thin wrapper over the inherited `get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown id.

        Args:
            product: A product id (`"seriescalc"`, `"tmy"`).

        Returns:
            Product: The matching catalog row.

        Raises:
            ValueError: If `product` is not a known id; the message names the
                catalog kind and, when a close match exists, a did-you-mean
                hint.
        """
        return cast("Product", self.get_dataset(product))
