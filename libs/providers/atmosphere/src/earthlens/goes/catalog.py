"""Product / domain / satellite catalog for the GOES ABI backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`goes_data_catalog.yaml`. A GOES request is three-axis: a **satellite**
(which bucket — `east`/`west` resolve to the current operational
`noaa-goes19` / `noaa-goes18`), a **product** (which ABI product family,
e.g. `abi-l2-mcmip`), and a **domain** (`C` CONUS, `F` Full Disk, `M1` /
`M2` Mesoscale). A concrete S3 prefix is `{product_group}{domain_suffix}`
(e.g. `ABI-L2-MCMIPC`); the two mesoscale subsectors share one `...M`
prefix and are split by the filename token.

The product is the "dataset" role (the key the user names in
`dataset=` / `variables={product: [...]}`), so it lives under the
inherited :attr:`~earthlens.base.AbstractCatalog.datasets` field — which
gives the catalog its `cat["abi-l2-mcmip"]` / `"abi-l2-mcmip" in cat` /
`len(cat)` dict-like surface and the did-you-mean error for free.
Domains, satellites, and the 16 ABI channels hang off parallel maps.

:data:`CATALOG_PATH` is the path to the bundled YAML;
:func:`clear_catalog_cache` empties the `(path, mtime)` parse cache.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "goes_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level GOES catalog parse cache."""
    _CATALOG_CACHE.clear()


class GOESChannel(BaseModel):
    """One of the 16 ABI spectral bands (reference metadata).

    Attributes:
        wavelength_um: Central wavelength in micrometres.
        name: Human-readable band name (`"Red (visible)"`).

    Examples:
        - Read a channel's wavelength:
            ```python
            >>> from earthlens.goes import Catalog
            >>> Catalog().channels["C02"].wavelength_um
            0.64

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    wavelength_um: float
    name: str = ""


class GOESDomain(BaseModel):
    """One ABI scan domain (CONUS / Full Disk / Mesoscale).

    Attributes:
        name: Human-readable domain name.
        prefix_suffix: The letter appended to a product's `product_group`
            to form the S3 prefix — `"C"`, `"F"`, or `"M"` (both
            mesoscale subsectors share the `"M"` prefix).
        subsector: Filename token that distinguishes a shared-prefix
            subsector (`"M1"` / `"M2"`); empty for `C` / `F`.
        cadence_minutes: Nominal minutes between scans — informational
            only (enumeration is listing-driven, not cadence-computed).

    Examples:
        - The two mesoscale domains share one `...M` prefix:
            ```python
            >>> from earthlens.goes import Catalog
            >>> cat = Catalog()
            >>> cat.domains["M1"].prefix_suffix, cat.domains["M1"].subsector
            ('M', 'M1')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    prefix_suffix: str
    subsector: str = ""
    cadence_minutes: float = 0.0


class GOESProduct(BaseModel):
    """One curated ABI product family (the "dataset" analog).

    The product key (`"abi-l2-mcmip"`) is the parent key in
    :attr:`Catalog.datasets` and is repeated here as :attr:`product` so a
    row carries its own identity when passed around outside the catalog.

    Attributes:
        product: Friendly product key (`"abi-l2-mcmip"`) — the value used
            in `dataset=` / `variables={product: [...]}`.
        product_group: The S3 prefix stem before the domain suffix
            (`"ABI-L2-MCMIP"`, `"ABI-L1b-Rad"`).
        level: Processing level (`"L1b"` / `"L2"`).
        description: Human-readable summary.
        domains: The domain keys this product publishes (a subset of
            :attr:`Catalog.domains`), e.g. `["C", "F", "M1", "M2"]`.
        band_split: `True` when each ABI channel is a **separate** file
            (`ABI-L1b-Rad`, `ABI-L2-CMIP`) so `variables=` picks which
            granule files are fetched; `False` when one file carries all
            bands (`ABI-L2-MCMIP`) so `variables=` is informational.
        default_domain: Domain used when the request names none.
        bands: For a `band_split` product, the channel tokens
            (`["C01", ..., "C16"]`) `variables=` may select; for a
            combined product, the in-file band-variable names
            (informational).

    Examples:
        - Inspect a product's group and domains:
            ```python
            >>> from earthlens.goes import Catalog
            >>> p = Catalog().get_product("abi-l2-mcmip")
            >>> p.product_group
            'ABI-L2-MCMIP'
            >>> p.band_split
            False

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: str
    product_group: str
    level: str = "L2"
    description: str = ""
    domains: list[str] = Field(default_factory=list)
    band_split: bool = False
    default_domain: str = "C"
    bands: list[str] = Field(default_factory=list)


def _parse_block(
    path: Path,
    rows: dict[str, Any] | None,
    model: type,
    noun: str,
    *,
    key_field: str | None = None,
) -> dict[str, Any]:
    """Validate one YAML block into `{key: model}`, naming the offender on error.

    The products, domains and channels blocks are validated identically apart
    from the row model and the word used in the error, so they share this.

    Args:
        path: The catalog file, named in any error.
        rows: The raw block, or `None` when the key is absent.
        model: The pydantic row class to build.
        noun: What one row is called, for the error message.
        key_field: Field to pass the mapping key into (products carry their own
            `product` id); `None` when the key is not part of the row.

    Returns:
        dict[str, Any]: One validated row per key, in file order.

    Raises:
        ValueError: If any row fails validation.
    """
    parsed: dict[str, Any] = {}
    for key, body in (rows or {}).items():
        fields = dict(body or {})
        if key_field is not None:
            fields[key_field] = key
        try:
            parsed[key] = model(**fields)
        except ValidationError as exc:
            raise ValueError(
                f"{path} {noun} {key!r} failed validation:\n{exc}"
            ) from exc
    return parsed


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the GOES catalog YAML into a fully-populated :class:`Catalog`.

    Args:
        files: The contributing YAML files (GOES ships a single file).

    Returns:
        dict[str, Any]: The validated construction kwargs (products,
            domains, satellites, channels). The payload is cached, not a
            built Catalog — `load()` makes a fresh instance per call.

    Raises:
        ValueError: If the `products:` block is missing or empty, or any
            product / domain / channel row fails validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    products_yaml = data.get("products") or {}
    if not products_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'products:' block. "
            "The GOES catalog must list at least one product."
        )
    return {
        "datasets": _parse_block(
            path, products_yaml, GOESProduct, "product", key_field="product"
        ),
        "domains": _parse_block(path, data.get("domains"), GOESDomain, "domain"),
        "channels": _parse_block(path, data.get("channels"), GOESChannel, "channel"),
        "satellites": {
            str(k): str(v) for k, v in (data.get("satellites") or {}).items()
        },
    }


class Catalog(AbstractCatalog):
    """Product / domain / satellite catalog for the GOES ABI backend.

    Reads the bundled `goes_data_catalog.yaml` (shipped as package data)
    and exposes its `products:` block as a map of :class:`GOESProduct`
    rows keyed by product key under the inherited :attr:`datasets` field,
    plus parallel :attr:`domains`, :attr:`satellites`, and
    :attr:`channels` maps. Instantiate with no arguments (`Catalog()`);
    :func:`model_post_init` loads and validates the YAML in one pass and
    caches it by `(path, mtime)`.

    Resolve a product with :meth:`get_product` (a thin alias over
    :meth:`~earthlens.base.AbstractCatalog.get_dataset`), a domain with
    :meth:`get_domain`, and a satellite/role to its bucket with
    :meth:`bucket_for`.

    Attributes:
        datasets: Map from product key to its :class:`GOESProduct` row.
        domains: Map from domain key (`"C"`, `"F"`, `"M1"`, `"M2"`) to
            its :class:`GOESDomain` row.
        satellites: Map from role / satellite number (`"east"`, `"19"`)
            to its unsigned bucket name.
        channels: Map from ABI channel token (`"C01"`) to its
            :class:`GOESChannel` metadata.
        available_datasets: Sorted product keys.

    Examples:
        - List products and resolve a satellite:
            ```python
            >>> from earthlens.goes import Catalog
            >>> cat = Catalog()
            >>> "abi-l2-mcmip" in cat
            True
            >>> cat.bucket_for("east")
            'noaa-goes19'

            ```
        - An unknown product raises with a did-you-mean hint:
            ```python
            >>> from earthlens.goes import Catalog
            >>> Catalog().get_product("abi-l2-mcmi")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'abi-l2-mcmi' is not in the GOES catalog. Known datasets: [...]. Did you mean 'abi-l2-mcmip'?

            ```
    """

    _catalog_kind: str = "GOES catalog"

    datasets: dict[str, GOESProduct] = Field(default_factory=dict)
    domains: dict[str, GOESDomain] = Field(default_factory=dict)
    satellites: dict[str, str] = Field(default_factory=dict)
    channels: dict[str, GOESChannel] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no products were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached by
        `(path, mtime)`); passing `datasets=...` skips the disk read
        (used in tests). Either way the `available_datasets` index is
        derived from the loaded map.

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is
                missing, empty, or has a malformed row.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
            self.domains = loaded.domains
            self.satellites = loaded.satellites
            self.channels = loaded.channels
        self.available_datasets = sorted(self.datasets)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the GOES catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `products:` block, or any product / domain / satellite /
                channel row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        payload = load_catalog(path, _CATALOG_CACHE, _parse_catalog, provider="GOES")
        return cls(**payload)

    def get_product(self, key: str) -> GOESProduct:
        """Return the :class:`GOESProduct` for `key`, with a did-you-mean hint.

        Thin alias over
        :meth:`~earthlens.base.AbstractCatalog.get_dataset`.

        Args:
            key: A product key (`"abi-l2-mcmip"`, `"abi-l1b-rad"`).

        Returns:
            GOESProduct: The matching product row.

        Raises:
            ValueError: If `key` is not a registered GOES product.
        """
        return cast("GOESProduct", self.get_dataset(key))

    def get_domain(self, key: str) -> GOESDomain:
        """Return the :class:`GOESDomain` for `key`, with a did-you-mean hint.

        Args:
            key: A domain key (`"C"`, `"F"`, `"M1"`, `"M2"`).

        Returns:
            GOESDomain: The matching domain row.

        Raises:
            ValueError: If `key` is not a registered GOES domain.
        """
        try:
            return self.domains[key]
        except KeyError:
            close = difflib.get_close_matches(key, self.domains, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{key!r} is not a GOES domain. "
                f"Known domains: {sorted(self.domains)}.{hint}"
            ) from None

    def bucket_for(self, satellite: str) -> str:
        """Resolve a satellite role / number to its unsigned bucket name.

        Args:
            satellite: A role (`"east"` / `"west"`) or satellite number
                (`"16"` / `"18"` / `"19"`). Case-insensitive for roles.

        Returns:
            str: The bucket name (`"noaa-goes19"`).

        Raises:
            ValueError: If `satellite` maps to no known bucket.
        """
        key = str(satellite).lower()
        try:
            return self.satellites[key]
        except KeyError:
            close = difflib.get_close_matches(key, self.satellites, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{satellite!r} is not a known GOES satellite. "
                f"Known: {sorted(self.satellites)}.{hint}"
            ) from None

    def products(self) -> list[str]:
        """Return the registered product keys, sorted.

        Returns:
            list[str]: The product keys.
        """
        return sorted(self.datasets)
