"""Product / epoch / resolution / CRS availability catalog for the GHSL backend.

The JRC Global Human Settlement Layer publishes a fixed set of product
families (GHS-POP, GHS-BUILT-S/V/H/C, GHS-SMOD, GHS-LAND, GHS-DUC, and the
R2025A GHS-WUP projections), each available only for a specific matrix of
epochs × resolutions × coordinate-reference-systems per release. Unlike a
large remote index, that matrix is small, slow-changing, and known in
advance, so it is curated as config-as-code in the bundled `catalog/`
directory — per-family `*.yaml` files (`population.yaml`, `built-up.yaml`,
`settlement.yaml`, `land.yaml`, `projections.yaml`) plus an `_index.yaml`
carrying the merged `available_datasets:` list — and validated here against
typed pydantic rows. The loader merges every file at construction time (the
GEE / CMEMS multi-file pattern) through a `(path, mtime_ns)` parse cache.

A request names one or more **product keys** — canonical (`"GHS_POP"`) or a
friendly alias (`"population"`) — plus a `release` / `epoch` / `resolution`
/ `crs`. `Catalog.resolve` maps an alias to its canonical code and
`Catalog.validate` checks a full `(product, release, epoch, resolution,
crs)` tuple against the availability matrix, raising a `ValueError` that
lists the valid options for whichever dimension failed (did-you-mean).

Two GHSL quirks the model captures:

* **Sub-product file tokens.** GHS-BUILT-H ships as `AGBH`/`ANBH`, GHS-BUILT-C
  as `FUN`/`MSZ`, GHS-BUILT-S/V also as a `NRES` (non-residential) variant.
  Their on-disk file stem (`GHS_BUILT_H_ANBH_E2018_…`) differs from their
  product-family directory (`GHS_BUILT_H_GLOBE_R2023A`). Each such variant is
  a distinct catalog row whose `family` field names the directory token while
  the catalog key is the file stem token.
* **Categorical products.** GHS-SMOD and GHS-BUILT-C carry class codes, not
  continuous values; their rows set `categorical: true` and ship a `legend`
  (code → label) and optional `colors` (code → hex), surfaced as a pyramids
  colour-table `DataFrame` via `Product.color_table`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pandas import DataFrame
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

#: Path to the bundled catalog directory of per-family `*.yaml` files plus the
#: `_index.yaml` informational index. Tests can monkey-patch this attribute to
#: redirect the loader at a temporary directory or a single YAML file.
CATALOG_PATH: Path = Path(__file__).parent / "catalog"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus a
#: tuple of `(file, mtime_ns)` for every YAML the load touched, so editing any
#: per-family file invalidates the entry without re-parsing on an unchanged
#: tree. Mirrors the GEE / CMEMS multi-file pattern.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Product]]] = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache keys include
    every contributing file's `st_mtime_ns`, so any real file mutation
    invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='GHSL', shard_noun='per-family')


def _load_catalog_data(path: Path) -> tuple[list[str], dict[str, Product]]:
    """Parse, validate, and cache the GHSL catalog at `path`.

    When `path` is a directory, every `*.yaml` is merged: `available_datasets:`
    lists are concatenated and `products:` maps are unioned (a code declared in
    two files is an error). Cached on the resolved path plus every contributing
    file's `mtime_ns`, so a second `Catalog()` on an unchanged tree skips both
    YAML parsing and pydantic validation.

    Args:
        path: Catalog directory (default `CATALOG_PATH`) or a single `*.yaml`.

    Returns:
        tuple[list[str], dict[str, Product]]: The merged `available_datasets:`
            index and the curated product map (keyed by canonical code).

    Raises:
        ValueError: If no file has a `products:` block, a code is declared in
            two files, a product row fails validation, or a curated code is
            absent from `available_datasets:`.
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    merged_available: list[str] = []
    merged_products_yaml: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        merged_available.extend(data.get("available_datasets") or [])
        for code, body in (data.get("products") or {}).items():
            if code in merged_products_yaml:
                raise ValueError(
                    f"product {code!r} declared in two catalog files: "
                    f"{origin[code]} and {file_path}"
                )
            merged_products_yaml[code] = body
            origin[code] = file_path

    if not merged_products_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'products:' block. "
            "The GHSL catalog must list at least one product."
        )

    available = set(merged_available)
    products: dict[str, Product] = {}
    for code, body in merged_products_yaml.items():
        try:
            products[code] = Product(code=code, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{origin[code]} product {code!r} failed validation:\n{exc}"
            ) from exc
        if available and code not in available:
            raise ValueError(
                f"product {code!r} is in 'products:' but missing from "
                f"'available_datasets:' ({origin[code]}); add it to "
                "_index.yaml too."
            )

    _CATALOG_CACHE[key] = (merged_available, products)
    return _CATALOG_CACHE[key]


#: Friendly resolution label → the token used in the JRC file path / name.
#: Mollweide resolutions are metres (`100`, `1000`, `10`); WGS84 ones are
#: arc-seconds (`3ss`, `30ss`).
RES_TO_TOKEN: dict[str, str] = {
    "10m": "10",
    "100m": "100",
    "1km": "1000",
    "3ss": "3ss",
    "30ss": "30ss",
}
#: Reverse of `RES_TO_TOKEN` — the JRC path token → friendly resolution label.
TOKEN_TO_RES: dict[str, str] = {v: k for k, v in RES_TO_TOKEN.items()}

#: Native source CRS implied by a resolution. GHSL pairs every metric
#: resolution with Mollweide (ESRI:54009) and every arc-second resolution with
#: WGS84 (EPSG:4326) — a resolution uniquely determines the file's source CRS,
#: so the catalog stores resolutions and derives the source CRS rather than
#: listing an independent (and easily-inconsistent) CRS dimension.
RES_TO_SOURCE_CRS: dict[str, str] = {
    "10m": "54009",
    "100m": "54009",
    "1km": "54009",
    "3ss": "4326",
    "30ss": "4326",
}


def native_source_crs(resolution: str) -> str:
    """Return the JRC source CRS token a resolution is delivered in.

    Args:
        resolution: A friendly resolution label (`"100m"`, `"3ss"`, …).

    Returns:
        str: `"54009"` (Mollweide) for metric resolutions, `"4326"`
            (WGS84) for arc-second resolutions.

    Raises:
        ValueError: If `resolution` is not a known GHSL resolution.

    Examples:
        - Metric → Mollweide, arc-second → WGS84:
            ```python
            >>> from earthlens.ghsl.catalog import native_source_crs
            >>> native_source_crs("100m")
            '54009'
            >>> native_source_crs("30ss")
            '4326'

            ```
    """
    try:
        return RES_TO_SOURCE_CRS[resolution]
    except KeyError:
        raise ValueError(
            f"unknown GHSL resolution {resolution!r}; "
            f"known: {sorted(RES_TO_SOURCE_CRS)}."
        ) from None


#: Resolutions delivered as a Mollweide tile grid (fine) rather than a single
#: whole-globe file (coarse). Used as the per-product default when a row does
#: not override `tiled_resolutions`.
DEFAULT_TILED_RESOLUTIONS: tuple[str, ...] = ("10m", "100m", "3ss")


class Availability(BaseModel):
    """The (epochs × resolutions × CRS) a product offers for one release.

    Attributes:
        epochs: The reference years the product publishes for this release
            (GHSL's 5-yearly steps, e.g. `[1975, 1980, …, 2030]`, or a
            single `[2018]` for the epoch-less products).
        resolutions: Friendly resolution labels available
            (`"100m"`, `"1km"`, `"3ss"`, `"30ss"`, `"10m"`). Each implies its
            source CRS via `native_source_crs` (metric → 54009, arc-sec →
            4326), so there is no separate CRS dimension to keep consistent.
        version: The `V{maj}-{min}` data version, as a `(major, minor)`
            string pair. Defaults to `("1", "0")` (the R2023A norm).
        tiled_resolutions: Which of `resolutions` ship as a Mollweide tile
            grid rather than a single whole-globe file. `None` falls back to
            `DEFAULT_TILED_RESOLUTIONS`.
        region: The JRC region token in the path — `"GLOBE"` (default),
            `"EUROPE"`, or `"ARCTIC"`.
        nested: Whether the per-epoch directories sit under an intermediate
            `{stem}_{region}_{release}/` sub-product directory (the R2022A
            layout) rather than directly under the family directory (R2023A).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    epochs: list[int] = Field(min_length=1)
    resolutions: list[str] = Field(min_length=1)
    version: tuple[str, str] = ("1", "0")
    tiled_resolutions: list[str] | None = None
    region: Literal["GLOBE", "EUROPE", "ARCTIC"] = "GLOBE"
    nested: bool = False

    def source_crs(self) -> frozenset[str]:
        """Return the distinct source CRS tokens these resolutions imply."""
        return frozenset(native_source_crs(r) for r in self.resolutions)

    def tiled(self) -> frozenset[str]:
        """Return the resolutions that ship as tiles (vs whole-globe).

        Returns:
            frozenset[str]: The subset of `resolutions` delivered as a tile
                grid; falls back to `DEFAULT_TILED_RESOLUTIONS` intersected
                with `resolutions` when the row leaves it unset.
        """
        if self.tiled_resolutions is not None:
            return frozenset(self.tiled_resolutions)
        return frozenset(DEFAULT_TILED_RESOLUTIONS) & frozenset(self.resolutions)


class Product(BaseModel):
    """One curated GHSL product row.

    The catalog key (a canonical code like `"GHS_POP"` or a sub-product stem
    like `"GHS_BUILT_H_ANBH"`) is the file-stem token; `family` is the
    product-family directory token (defaults to the code, differing only for
    sub-products).

    Attributes:
        code: Canonical product code / file-stem token (e.g. `"GHS_POP"`,
            `"GHS_BUILT_H_ANBH"`). Set from the catalog key by the loader.
        family: Product-family directory token (e.g. `"GHS_BUILT_H"`).
            Defaults to `code`; differs only for the `AGBH`/`ANBH`,
            `FUN`/`MSZ`, and `NRES` sub-products.
        aliases: Friendly names that also resolve to this product
            (`["population", "pop"]`).
        unit: Human-readable unit of the values (`"people/cell"`); empty for
            categorical / tabular products.
        categorical: Whether the values are class codes (reproject with
            nearest-neighbour; carry a legend; reject value-averaging).
        kind: `"raster"` (the default GIS pipeline) or `"tabular"` (DUC and
            the WUP statistics tables — fetched as a side table, no
            mosaic / reproject / crop).
        default_resolution: The resolution used when a request omits
            `resolution=`.
        legend: For categorical products, the class-code → label map.
        colors: Optional class-code → hex-colour map for categorical
            products (e.g. `{30: "#FF0000"}`); paired with `legend`.
        releases: Per-release availability, keyed by release id (`"R2023A"`,
            `"R2022A"`, `"R2025A"`). Each release maps to a **list** of
            `Availability` blocks; a request is valid when some block
            contains both its epoch and resolution. The list expresses
            products whose epoch set differs by resolution (e.g. GHS-BUILT-S
            offers a 12-epoch series at 100 m/1 km/arc-sec plus a single
            2018-only 10 m layer).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = ""
    family: str | None = None
    aliases: list[str] = Field(default_factory=list)
    unit: str = ""
    categorical: bool = False
    kind: str = "raster"
    default_resolution: str | None = None
    legend: dict[int, str] | None = None
    colors: dict[int, str] | None = None
    releases: dict[str, list[Availability]] = Field(default_factory=dict)

    def family_token(self) -> str:
        """Return the product-family directory token (`family` or `code`)."""
        return self.family or self.code

    def release_epochs(self, release: str) -> list[int]:
        """Return the union of epochs across a release's availability blocks.

        Args:
            release: A release id present in `releases`.

        Returns:
            list[int]: Sorted, de-duplicated epochs offered for the release.
        """
        epochs: set[int] = set()
        for block in self.releases.get(release, []):
            epochs.update(block.epochs)
        return sorted(epochs)

    def release_resolutions(self, release: str) -> list[str]:
        """Return the union of resolutions across a release's blocks.

        Args:
            release: A release id present in `releases`.

        Returns:
            list[str]: De-duplicated resolution labels (first-seen order).
        """
        out: list[str] = []
        for block in self.releases.get(release, []):
            for res in block.resolutions:
                if res not in out:
                    out.append(res)
        return out

    def block_for(
        self, release: str, epoch: int, resolution: str
    ) -> Availability | None:
        """Return the availability block matching `(epoch, resolution)`, if any.

        Args:
            release: A release id.
            epoch: The reference year.
            resolution: A friendly resolution label.

        Returns:
            Availability | None: The first block whose `epochs` and
                `resolutions` both contain the request, or `None`.
        """
        for block in self.releases.get(release, []):
            if epoch in block.epochs and resolution in block.resolutions:
                return block
        return None

    def color_table(self) -> DataFrame:
        """Return the class legend as a pyramids colour-table `DataFrame`.

        Builds the `band, values, color, alpha` frame
        `pyramids.dataset.Dataset.color_table` expects: one row per legend
        code, band `1`, the code in `values`, and the hex colour from
        `colors` (falling back to mid-grey `#808080` when a code has no
        colour). Categorical products only.

        Returns:
            DataFrame: Columns `band` (int), `values` (int class code),
                `color` (hex string), `alpha` (int, 255).

        Raises:
            ValueError: If the product is not categorical or has no legend.
        """
        if not self.categorical or not self.legend:
            raise ValueError(
                f"{self.code} is not a categorical product with a legend; "
                "color_table() is only defined for categorical products."
            )
        colors = self.colors or {}
        rows = [
            {
                "band": 1,
                "values": code,
                "color": colors.get(code, "#808080"),
                "alpha": 255,
            }
            for code in self.legend
        ]
        return DataFrame(rows, columns=["band", "values", "color", "alpha"])


class Catalog(AbstractCatalog[Product]):
    """Product / availability catalog for the GHSL backend.

    Merges the bundled `catalog/` directory's per-family `*.yaml` files and
    exposes their `products:` blocks as a map of `Product` rows keyed by
    canonical code under the inherited `datasets` field (giving
    `cat["GHS_POP"]`, `"GHS_POP" in cat`, `len(cat)`, and the did-you-mean
    error for free). Instantiate with no arguments (`Catalog()`);
    `model_post_init` loads and validates the catalog through the parse cache.

    Attributes:
        datasets: Map from canonical product code to its `Product` row.
        available_datasets: Every product code from `_index.yaml`. For GHSL the
            curated set is the full in-scope GLOBE surface, so this equals the
            curated keys (there is no larger auto-discovered universe).
    """

    _catalog_kind: str = "GHSL product catalog"

    datasets: dict[str, Product] = Field(default_factory=dict)
    _alias_index: dict[str, str] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no products were supplied.

        `Catalog()` with no args reads `CATALOG_PATH` (through the
        `(path, mtime_ns)`-keyed parse cache); passing `datasets=...` skips the
        disk read (used in tests).

        Raises:
            ValueError: Propagated from `load` when the catalog is missing,
                empty, or has a malformed product row.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
            self.available_datasets = loaded.available_datasets
        # Populate the inherited `catalog` field (== get_catalog()) per the
        # AbstractCatalog contract, then build the alias lookup.
        super().model_post_init(__context)
        self._index_aliases()

    def _index_aliases(self) -> None:
        """Build the alias → canonical-code lookup from the loaded rows."""
        index: dict[str, str] = {}
        for code, product in self.datasets.items():
            index[code.lower()] = code
            for alias in product.aliases:
                index[alias.lower()] = code
        self._alias_index = index

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the GHSL product catalog from disk (directory or single file).

        Merges every per-family `*.yaml` in the catalog directory (the curated
        `products:` blocks + the `_index.yaml` `available_datasets:` list)
        through the `(path, mtime_ns)`-keyed parse cache.

        Args:
            catalog_path: Catalog directory or single YAML file. Defaults to
                the module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If no file has a `products:` block, a code is declared
                in two files, a row fails `Product` validation, or a curated
                code is absent from `available_datasets:`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        available, products = _load_catalog_data(catalog_path)
        return cls(
            datasets=dict(products),
            available_datasets=list(available),
        )

    def get(self, code: str) -> Product:
        """Return the `Product` for a canonical code, did-you-mean on miss.

        Args:
            code: A canonical product code (`"GHS_POP"`).

        Returns:
            Product: The matching row.

        Raises:
            ValueError: If `code` is not a curated product.
        """
        return cast("Product", self.get_dataset(code))

    def resolve(self, key: str) -> str:
        """Resolve a product key or friendly alias to its canonical code.

        Args:
            key: A canonical code (`"GHS_POP"`) or a friendly alias
                (`"population"`, case-insensitive).

        Returns:
            str: The canonical product code.

        Raises:
            ValueError: If `key` matches no product or alias; the message
                lists the known products + aliases with a did-you-mean hint.

        Examples:
            - An alias and a canonical key both resolve:
                ```python
                >>> from earthlens.ghsl import Catalog
                >>> cat = Catalog()
                >>> cat.resolve("population")
                'GHS_POP'
                >>> cat.resolve("GHS_POP")
                'GHS_POP'

                ```
        """
        import difflib

        index = self._alias_index
        canonical = index.get(key.lower())
        if canonical is not None:
            return canonical
        close = difflib.get_close_matches(key.lower(), index, n=1)
        hint = f" Did you mean {index[close[0]]!r}?" if close else ""
        raise ValueError(
            f"{key!r} is not a known GHSL product or alias. "
            f"Known products: {sorted(self.datasets)}.{hint}"
        )

    def available_products(self) -> list[str]:
        """Return the curated product codes, sorted.

        Returns:
            list[str]: The canonical codes (`["GHS_BUILT_C_FUN", ...]`).
        """
        return sorted(self.datasets)

    def validate(  # type: ignore[override]
        self,
        product: str,
        release: str,
        epoch: int,
        resolution: str,
    ) -> tuple[str, str, int, str]:
        """Validate a request against the availability matrix.

        Checks the source variant `(product, release, epoch, resolution)`
        exists. The source CRS is implied by `resolution` (see
        `native_source_crs`); the user's *output* CRS is independent (the
        backend reprojects to it) and is not validated here.

        Args:
            product: A product key or alias (resolved first).
            release: A release id (`"R2023A"`, `"R2022A"`, `"R2025A"`).
            epoch: The reference year.
            resolution: A friendly resolution label (`"100m"`).

        Returns:
            tuple[str, str, int, str]: The validated
                `(code, release, epoch, resolution)` with `product` resolved
                to its canonical code.

        Raises:
            ValueError: If any dimension is unavailable; the message lists
                the valid options for the first failing dimension.

        Examples:
            - A valid combo passes; an unavailable epoch is rejected:
                ```python
                >>> from earthlens.ghsl import Catalog
                >>> cat = Catalog()
                >>> cat.validate("GHS_BUILT_H_ANBH", "R2023A", 2018, "100m")
                ('GHS_BUILT_H_ANBH', 'R2023A', 2018, '100m')

                ```
        """
        code = self.resolve(product)
        row = self.datasets[code]
        if release not in row.releases:
            raise ValueError(
                f"{code} has no release {release!r}; "
                f"available releases: {sorted(row.releases)}."
            )
        epochs = row.release_epochs(release)
        if epoch not in epochs:
            raise ValueError(
                f"{code} ({release}) has no epoch {epoch}; available epochs: {epochs}."
            )
        resolutions = row.release_resolutions(release)
        if resolution not in resolutions:
            raise ValueError(
                f"{code} ({release}) has no resolution {resolution!r}; "
                f"available resolutions: {resolutions}."
            )
        if row.block_for(release, epoch, resolution) is None:
            raise ValueError(
                f"{code} ({release}) offers epoch {epoch} and resolution "
                f"{resolution!r} but not together; resolution {resolution!r} "
                f"is available only for epochs "
                f"{sorted({e for b in row.releases[release] if resolution in b.resolutions for e in b.epochs})}."
            )
        return code, release, epoch, resolution
