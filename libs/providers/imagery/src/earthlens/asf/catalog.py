"""Platform/product catalog for the ASF InSAR backend.

ASF's `asf_search` SDK filters its queries by raw `platform` /
`dataset` / `product_type` enum values
(`asf_search.PLATFORM.SENTINEL1`,
`asf_search.DATASET.OPERA_S1`,
`asf_search.PRODUCT_TYPE.SLC`). This module is the friendly-name
bridge: each curated row maps one user-facing product key
(`"sentinel-1-slc"`) to its SDK constants and flags whether the
product supports `ASFProduct.stack()` (only the SAR-image classes
do; processed-RTC outputs do not).

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass that loads the bundled `asf_data_catalog.yaml`. Resolve a
key with :meth:`Catalog.resolve` (which accepts a curated key or
one of its aliases, with a did-you-mean hint on an unknown name),
and read the row with :meth:`Catalog.get_product`. The catalog is
**hand-curated** — the ASF universe is small enough that a refresh
tool would not pay for itself (mirrors the fdsn / openaq pattern).

:data:`CATALOG_PATH` is the path to the bundled YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "asf_data_catalog.yaml"

#: Module-level cache of parsed catalog rows, keyed on the resolved
#: path plus the YAML's `st_mtime_ns`. Mirrors the cache pattern in
#: the usgs_water / fdsn / cmems loaders so editing the file
#: invalidates the entry without re-parsing on every `Catalog()`.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Product]] = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to
    force a re-parse. Production callers do not need this — the
    cache key includes the file's `st_mtime_ns`, so any real edit
    invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> dict[str, Product]:
    """Parse, validate, and cache the product catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        Mapping from friendly product key to its :class:`Product`.

    Raises:
        ValueError: If the file has no `products:` block, or a row
            fails :class:`Product` validation.
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
            "The ASF catalog must list at least one product."
        )
    rows: dict[str, Product] = {}
    for name, body in products_yaml.items():
        try:
            rows[name] = Product(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} product {name!r} failed validation:\n{exc}"
            ) from exc

    # Sanity-check the informational `available_products:` index — if
    # present, it must match the curated keys exactly. The index is
    # documentation; a stale block would silently lie to anyone
    # reading the YAML.
    declared = data.get("available_products")
    if declared is not None:
        curated = sorted(rows)
        if sorted(declared) != curated:
            extra = sorted(set(declared) - set(rows))
            missing = sorted(set(rows) - set(declared))
            raise ValueError(
                f"{path}: available_products: index drifted from products: "
                f"block — extra={extra!r}, missing={missing!r}"
            )

    _CATALOG_CACHE[key] = rows
    return rows


class Product(BaseModel):
    """One ASF product catalog row.

    Each row maps a friendly product key (`"sentinel-1-slc"`) to the
    raw `asf_search` enum members needed to issue a search:

    * either a :attr:`platform` (a `PLATFORM` member name like
      `"SENTINEL1"`) — used for the Sentinel-1, ALOS, ERS, JERS,
      RADARSAT, SEASAT, NISAR families;
    * or a :attr:`dataset` (a `DATASET` member name like
      `"OPERA_S1"`) — used for the processed-product families
      (OPERA-S1, ARIA S1 GUNW, SLC-BURST) whose `asf_search` query
      key is `dataset=` rather than `platform=`.

    Exactly one of `platform` / `dataset` must be set (validator).

    Attributes:
        platform: A member name of `asf_search.PLATFORM` (e.g.
            `"SENTINEL1"`, `"ALOS"`). Mutually exclusive with
            `dataset`.
        dataset: A member name of `asf_search.DATASET` (e.g.
            `"OPERA_S1"`, `"ARIA_S1_GUNW"`, `"SLC_BURST"`).
            Mutually exclusive with `platform`.
        product_type: A member name of `asf_search.PRODUCT_TYPE`
            (e.g. `"SLC"`, `"BURST"`, `"L1_1"`, `"RTC"`). Maps to
            the `processingLevel=` search kwarg.
        aliases: Friendly names that resolve to this row (e.g.
            `["s1-slc", "sentinel1-slc"]` for `"sentinel-1-slc"`).
        stackable: `True` when `ASFProduct.stack()` is meaningful
            on this product type (Sentinel-1 SLC + BURST, ALOS
            PALSAR SLC, …). `False` for processed-derivative
            products (RTC, GRD, OPERA-RTC) where there is no
            baseline-comparable acquisition.
        description: Free-form one-line label used in error
            messages and documentation.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.asf import Product
            >>> p = Product(platform="SENTINEL1", product_type="SLC",
            ...             stackable=True)
            >>> p.platform
            'SENTINEL1'
            >>> p.product_type
            'SLC'

            ```
        - A row with both `platform` and `dataset` is rejected
          (raises `pydantic.ValidationError`):
            ```python
            >>> from pydantic import ValidationError
            >>> from earthlens.asf import Product
            >>> try:
            ...     Product(platform="SENTINEL1", dataset="OPERA_S1",
            ...             product_type="RTC")
            ... except ValidationError:
            ...     print("rejected")
            rejected

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str | None = None
    dataset: str | None = None
    product_type: str
    aliases: list[str] = Field(default_factory=list)
    stackable: bool = False
    description: str = ""

    @model_validator(mode="after")
    def _exactly_one_platform_or_dataset(self) -> Product:
        """Require exactly one of `platform` / `dataset`.

        `asf_search` queries select by either a `platform` member or
        a `dataset` member (for the processed-product families) —
        never both, never neither. Enforcing the invariant here keeps
        the search wiring straightforward.

        Raises:
            ValueError: When both `platform` and `dataset` are set,
                or when neither is set.
        """
        has_platform = self.platform is not None
        has_dataset = self.dataset is not None
        if has_platform == has_dataset:
            raise ValueError(
                "exactly one of `platform` or `dataset` must be set "
                f"(got platform={self.platform!r}, dataset={self.dataset!r})"
            )
        return self


class Catalog(AbstractCatalog[Product]):
    """Product catalog for the ASF InSAR backend.

    Reads the bundled `asf_data_catalog.yaml` (shipped as package
    data) and exposes its `products:` block as a map of
    :class:`Product` rows keyed by friendly product key.
    Instantiate with no arguments (`Catalog()`).

    Attributes:
        products: Map from the friendly product key to its
            :class:`Product` row. Alias for the base `datasets`
            field; the inherited dict surface (`len`, `in`, `[]`,
            iteration) and the did-you-mean error message work
            against the same store.

    Examples:
        - Resolve a friendly alias to the curated key:
            ```python
            >>> from earthlens.asf import Catalog
            >>> Catalog().resolve("s1-slc")
            'sentinel-1-slc'

            ```
        - An unknown but close name raises with a did-you-mean
          hint:
            ```python
            >>> from earthlens.asf import Catalog
            >>> try:
            ...     Catalog().resolve("sentinel1-slx")
            ... except ValueError as exc:
            ...     print("rejected" if "Did you mean" in str(exc)
            ...           or "not in the ASF" in str(exc) else "ok")
            rejected

            ```
    """

    _catalog_kind: str = "ASF product catalog"
    _entry_noun: str = "products"

    datasets: dict[str, Product] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_products_alias(cls, data: Any) -> Any:
        """Accept the domain-named `products=` kwarg as an alias for `datasets`.

        Lets tests and refresh tools construct
        `Catalog(products={...})` without leaking the base field
        name.

        Args:
            data: The raw model input.

        Returns:
            The input with `products` renamed to `datasets`,
            untouched otherwise.
        """
        if isinstance(data, dict) and "products" in data and "datasets" not in data:
            data = dict(data)
            data["datasets"] = data.pop("products")
        return data

    @property
    def products(self) -> dict[str, Product]:
        """The product map — alias for the base :attr:`datasets` field.

        Returns:
            dict[str, Product]: The same mapping stored in
                :attr:`datasets`.

        Examples:
            - The alias and the base field are the same object:
                ```python
                >>> from earthlens.asf import Catalog
                >>> cat = Catalog()
                >>> cat.products is cat.datasets
                True

                ```
        """
        return self.datasets

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog and verify alias uniqueness.

        An alias that appears on more than one row would silently
        shadow the later row in :meth:`resolve` (the loop returns
        the first match). Catch the typo at construction so the
        broken catalog fails fast.

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML
                is missing, empty, or has a malformed row; or when
                an alias is reused across two product rows.
        """
        if not self.datasets:
            self.datasets = Catalog.load().datasets
        seen: dict[str, str] = {}
        for canonical, row in self.datasets.items():
            for alias in row.aliases:
                prior = seen.get(alias)
                if prior is not None:
                    raise ValueError(
                        f"alias {alias!r} appears on both {prior!r} and "
                        f"{canonical!r}; aliases must be unique across "
                        "the ASF catalog"
                    )
                seen[alias] = canonical
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the ASF product catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `products:` block, or a
                row fails :class:`Product` validation.

        Examples:
            - Load the bundled catalog and inspect a curated row:
                ```python
                >>> from earthlens.asf import Catalog
                >>> cat = Catalog.load()
                >>> "sentinel-1-slc" in cat.products
                True
                >>> cat.get_product("sentinel-1-slc").product_type
                'SLC'

                ```
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(datasets=dict(_load_catalog_data(catalog_path)))

    @property
    def available_products(self) -> list[str]:
        """The sorted list of curated friendly product keys.

        Returns:
            list[str]: Every curated catalog key, sorted.

        Examples:
            - List the curated names and check membership:
                ```python
                >>> from earthlens.asf import Catalog
                >>> names = Catalog().available_products
                >>> "sentinel-1-slc" in names
                True
                >>> names == sorted(names)
                True

                ```
        """
        return sorted(self.datasets)

    def get_product(self, key: str) -> Product:
        """Resolve a friendly key to its :class:`Product` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which
        raises a `ValueError` with a did-you-mean hint on an unknown
        key.

        Args:
            key: A friendly product key (`"sentinel-1-slc"`).

        Returns:
            Product: The matching catalog row.

        Raises:
            ValueError: If `key` is not a known product; the message
                names the catalog kind and, when a close match
                exists, adds a did-you-mean hint.
        """
        return cast("Product", self.get_dataset(key))

    def resolve(self, key_or_alias: str) -> str:
        """Resolve a curated key or an alias to the curated key.

        A curated key passes through unchanged. An alias resolves to
        the curated key that lists it. Anything else raises with a
        did-you-mean hint over the union of curated keys and
        aliases.

        Args:
            key_or_alias: A curated key (`"sentinel-1-slc"`) or one
                of its aliases (`"s1-slc"`).

        Returns:
            str: The curated key.

        Raises:
            ValueError: If `key_or_alias` is neither a curated key
                nor a known alias.
        """
        if key_or_alias in self.datasets:
            return key_or_alias
        for canonical, row in self.datasets.items():
            if key_or_alias in row.aliases:
                return canonical
        # Unknown name — drop into the base did-you-mean error path,
        # which always raises a `ValueError` with a hint.
        self.get_dataset(key_or_alias)
        # Defensive — the call above always raises for an unknown
        # key, so this line is unreachable.
        return key_or_alias

    def stackable_products(self) -> list[str]:
        """The sorted list of curated keys whose `stackable` flag is `True`.

        Returns:
            list[str]: Every curated key where
                :attr:`Product.stackable` is `True`, sorted.

        Examples:
            - Inspect the InSAR-ready subset:
                ```python
                >>> from earthlens.asf import Catalog
                >>> stackable = Catalog().stackable_products()
                >>> "sentinel-1-slc" in stackable
                True
                >>> "opera-rtc-s1" in stackable
                False
                >>> stackable == sorted(stackable)
                True

                ```
        """
        return sorted(k for k, p in self.datasets.items() if p.stackable)
