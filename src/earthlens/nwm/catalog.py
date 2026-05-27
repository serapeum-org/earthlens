"""Product + configuration catalog for the National Water Model backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`nwm_data_catalog.yaml`. NWM is two-axis: a **product** (what the file
contains — `chrtout` channel routing, `ldasout` land surface) crossed
with a **configuration** (which operational run produced it —
`short_range`, `analysis_assim`, `medium_range`). A concrete S3 object
key needs both, plus a cycle datetime and a forecast/analysis step, so
the catalog keeps the two as separate maps and the backend assembles
the key from a `(configuration, product, cycle, step)` tuple.

The product is the "dataset" role (the key the user names in
`variables={product: [variable, ...]}`), so it lives under the inherited
:attr:`~earthlens.base.AbstractCatalog.datasets` field — which is what
gives the catalog its `cat["chrtout"]` / `"chrtout" in cat` / `len(cat)`
dict-like surface and the did-you-mean error for free. Configurations
hang off a parallel :attr:`Catalog.configurations` map.

:data:`CATALOG_PATH` is the path to the bundled YAML;
:func:`clear_catalog_cache` empties the `(path, mtime)` parse cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog, OutputKind
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "nwm_data_catalog.yaml"

_CATALOG_CACHE: dict[tuple[str, int], "Catalog"] = {}


def clear_catalog_cache() -> None:
    """Empty the module-level NWM catalog parse cache."""
    _CATALOG_CACHE.clear()


class NWMVariable(BaseModel):
    """One variable carried by an NWM product (the "variable" analog).

    A frozen value object with descriptive metadata only — NWM variables
    carry no request-shaping parameters because the MVP downloads whole
    files (subsetting a single variable out of a file is a read, deferred
    to pyramids `PY-G`).

    Attributes:
        units: Physical unit (`"m3 s-1"`, `"m3 m-3"`, `"1"` for a
            dimensionless fraction).
        long_name: Human-readable description used in docs and logs.

    Examples:
        - Build a variable row directly:
            ```python
            >>> from earthlens.nwm import NWMVariable
            >>> v = NWMVariable(units="m3 s-1", long_name="River channel flow rate")
            >>> v.units
            'm3 s-1'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    units: str = ""
    long_name: str = ""


class NWMProduct(BaseModel):
    """One NWM product's row (the "dataset" analog).

    The product key (`"chrtout"`) is the parent key in
    :attr:`Catalog.datasets` and is repeated here as :attr:`product` so a
    row carries its own identity when passed around outside the catalog.

    Attributes:
        product: Product key (`"chrtout"`, `"ldasout"`) — the value used
            in `variables={product: [...]}`.
        output_kind: `"tabular"` for the feature-id-indexed stream-reach
            product (`chrtout`), `"raster"` for the gridded land-surface
            product (`ldasout`). Drives the backend's per-instance
            `OUTPUT_KIND` and the facade's `aggregate=` gate.
        dims: The product's dimensions (`["feature_id", "time"]` vs
            `["y", "x", "time"]`) — informational, surfaced in docs.
        s3_token: The `{output}` token in the S3 file name
            (`channel_rt`, `land`). For an ensemble configuration the
            member rides on this token (`channel_rt_1`).
        configurations: The configuration keys this product is published
            under.
        retro_zarr: The retrospective (v3.0) Zarr store URI for this
            product. Used only by the `PY-G`-gated retrospective path.
        description: Human-readable summary.
        variables: Per-variable metadata keyed by variable name.

    Examples:
        - Inspect a product's kind and a variable:
            ```python
            >>> from earthlens.nwm import Catalog
            >>> chrtout = Catalog().get_product("chrtout")
            >>> chrtout.output_kind
            'tabular'
            >>> chrtout.variables["streamflow"].units
            'm3 s-1'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: str
    output_kind: OutputKind
    dims: list[str] = Field(default_factory=list)
    s3_token: str
    configurations: list[str] = Field(default_factory=list)
    retro_zarr: str = ""
    description: str = ""
    variables: dict[str, NWMVariable] = Field(default_factory=dict)


class NWMConfig(BaseModel):
    """One NWM operational configuration's row.

    The configuration key (`"short_range"`) is the parent key in
    :attr:`Catalog.configurations` and is repeated here as :attr:`key`.

    Attributes:
        key: Configuration key (`"short_range"`, `"analysis_assim"`,
            `"medium_range"`).
        description: Human-readable summary.
        domain: Spatial domain token (`"conus"`) — also the file-name
            suffix before `.nc`.
        family: The `{config}` token in the S3 file name. Equals the
            configuration directory for deterministic runs; ensemble
            runs append the member to the directory (`medium_range_mem1`)
            while the file-name family stays `medium_range`.
        cycles_utc: The configuration's daily run hours, in `[0, 23]`.
        step_kind: `"forecast"` (`fNNN` lead steps) or `"analysis"`
            (`tmNN` look-back steps) — selects the step-token format.
        first_step: First step published (`1` for forecasts, `0` for
            analyses); the default step when none is requested.
        horizon_h: Maximum step.
        step_cadence_h: Spacing between published steps.
        members: Ensemble member count — `0` for a deterministic run, or
            the member count (`6` for `medium_range`) for an ensemble,
            whose directory is `{family}_mem{N}` and whose product token
            carries the member (`channel_rt_1`).

    Examples:
        - Read a configuration's horizon:
            ```python
            >>> from earthlens.nwm import Catalog
            >>> Catalog().get_config("short_range").horizon_h
            18

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    description: str = ""
    domain: str = "conus"
    family: str
    cycles_utc: list[int] = Field(default_factory=list)
    step_kind: Literal["forecast", "analysis"] = "forecast"
    first_step: int = 1
    horizon_h: int = 0
    step_cadence_h: int = 1
    members: int = 0


class Catalog(AbstractCatalog):
    """Product + configuration catalog for the NWM backend.

    Reads the bundled `nwm_data_catalog.yaml` (shipped as package data)
    and exposes its `products:` block as a map of :class:`NWMProduct`
    rows keyed by product key under the inherited :attr:`datasets` field,
    and its `configurations:` block as a parallel
    :attr:`configurations` map of :class:`NWMConfig` rows. Instantiate
    with no arguments (`Catalog()`); :func:`model_post_init` loads and
    validates the YAML in one pass and caches it by `(path, mtime)`.

    Resolve a product with :meth:`get_product` (a thin alias over
    :meth:`~earthlens.base.AbstractCatalog.get_dataset`) and a
    configuration with :meth:`get_config`.

    Attributes:
        datasets: Map from product key to its :class:`NWMProduct` row.
        configurations: Map from configuration key to its
            :class:`NWMConfig` row.

    Examples:
        - List products and resolve one:
            ```python
            >>> from earthlens.nwm import Catalog
            >>> cat = Catalog()
            >>> cat.products()
            ['chrtout', 'ldasout']
            >>> cat.get_product("ldasout").output_kind
            'raster'
            >>> "short_range" in cat.configurations
            True

            ```
        - An unknown product raises with a did-you-mean hint:
            ```python
            >>> from earthlens.nwm import Catalog
            >>> Catalog().get_product("chrtou")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'chrtou' is not in the NWM catalog. Known datasets: [...]. Did you mean 'chrtout'?

            ```
    """

    _catalog_kind: str = "NWM catalog"

    datasets: dict[str, NWMProduct] = Field(default_factory=dict)
    configurations: dict[str, NWMConfig] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no products were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached by
        `(path, mtime)`); passing `datasets=...` skips the disk read
        (used in tests).

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is
                missing, empty, or has a malformed row.
        """
        if self.datasets:
            return
        loaded = Catalog.load()
        self.datasets = loaded.datasets
        self.configurations = loaded.configurations

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the NWM catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `products:` block, or any
                product / configuration row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = 0
        cache_key = (str(path.resolve()), mtime)
        cached = _CATALOG_CACHE.get(cache_key)
        if cached is not None:
            return cached

        data = load_yaml_strict(path) or {}
        products_yaml = data.get("products") or {}
        if not products_yaml:
            raise ValueError(
                f"{path} is missing or has an empty 'products:' block. "
                "The NWM catalog must list at least one product."
            )
        products: dict[str, NWMProduct] = {}
        for key, body in products_yaml.items():
            try:
                products[key] = NWMProduct(product=key, **dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{path} product {key!r} failed validation:\n{exc}"
                ) from exc
        configurations: dict[str, NWMConfig] = {}
        for key, body in (data.get("configurations") or {}).items():
            try:
                configurations[key] = NWMConfig(key=key, **dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{path} configuration {key!r} failed validation:\n{exc}"
                ) from exc
        catalog = cls(datasets=products, configurations=configurations)
        _CATALOG_CACHE[cache_key] = catalog
        return catalog

    def get_catalog(self) -> dict[str, NWMProduct]:
        """Return the product map (satisfies the abstract contract).

        Returns:
            dict[str, NWMProduct]: Same object as :attr:`datasets`.
        """
        return self.datasets

    def get_product(self, key: str) -> NWMProduct:
        """Return the :class:`NWMProduct` for `key`, with a did-you-mean hint.

        Thin alias over
        :meth:`~earthlens.base.AbstractCatalog.get_dataset`.

        Args:
            key: A product key (`"chrtout"`, `"ldasout"`).

        Returns:
            NWMProduct: The matching product row.

        Raises:
            ValueError: If `key` is not a registered NWM product.
        """
        return self.get_dataset(key)

    def get_config(self, key: str) -> NWMConfig:
        """Return the :class:`NWMConfig` for `key`, with a did-you-mean hint.

        Args:
            key: A configuration key (`"short_range"`,
                `"analysis_assim"`, `"medium_range"`).

        Returns:
            NWMConfig: The matching configuration row.

        Raises:
            ValueError: If `key` is not a registered NWM configuration.
        """
        try:
            return self.configurations[key]
        except KeyError:
            import difflib

            close = difflib.get_close_matches(key, self.configurations, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{key!r} is not an NWM configuration. "
                f"Known configurations: {sorted(self.configurations)}.{hint}"
            ) from None

    def products(self) -> list[str]:
        """Return the registered product keys, sorted.

        Returns:
            list[str]: The product keys (`["chrtout", "ldasout"]`).
        """
        return sorted(self.datasets)
