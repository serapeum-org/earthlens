"""Product / sub-alias / availability catalog for the WorldPop backend.

The WorldPop hub exposes a three-level REST scheme: a top-level **product
alias** (`pop`, `age_structures`, `births`, …), each with a set of
**sub-aliases** that pin a concrete variant (constrained vs unconstrained,
100 m vs 1 km, per-country vs global mosaic, and the product *generation* —
the classic R2021 line vs the newer Global-2 `G2_*` lines). A sub-alias
maps to a `…/{alias}/{subalias}` REST path that lists one GeoTIFF record
per year.

That matrix is small and slow-changing, so it is curated as config-as-code
in the bundled `worldpop_data_catalog.yaml` and validated here against typed
pydantic rows. A request names one or more product keys — canonical
(`"pop"`) or a friendly alias (`"population"`) — plus
`constrained` / `unadjusted` / `resolution` / `scope` / `generation`
selectors; `Catalog.resolve` maps an alias to its canonical product and
`Catalog.pick_subalias` resolves the selectors to a single sub-alias id,
raising a `ValueError` that lists the valid options for the product when no
sub-alias matches (did-you-mean).
"""

from __future__ import annotations

import difflib
import warnings
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "worldpop_data_catalog.yaml"

#: The product generations WorldPop publishes, newest last. `"R2021"` is the
#: classic 2000–2020 line (`wpgp` / `cic2020_*`); `"R2024B"` / `"R2025A"` /
#: `"2024"` are the Global-2 `G2_*` lines (2015–2030).
GENERATIONS: tuple[str, ...] = ("R2021", "2024", "R2024B", "R2025A")


def _years_set(spec: str) -> set[int]:
    """Expand a `years:` spec (`"2000-2020"` or `"2020"`) to a set of ints.

    Args:
        spec: Either a single year (`"2020"`) or an inclusive
            `"{start}-{end}"` range (`"2000-2020"`).

    Returns:
        set[int]: Every year the spec covers.

    Raises:
        ValueError: If `spec` is not a year or a `start-end` range.

    Examples:
        - A single year and a range both expand:
            ```python
            >>> from earthlens.worldpop.catalog import _years_set
            >>> sorted(_years_set("2020"))
            [2020]
            >>> sorted(_years_set("2018-2020"))
            [2018, 2019, 2020]

            ```
    """
    text = spec.strip()
    if "-" in text:
        lo_s, hi_s = text.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
        return set(range(lo, hi + 1))
    return {int(text)}


class SubAlias(BaseModel):
    """One concrete WorldPop variant under a product alias.

    Maps the selector tuple `(constrained, unadjusted, resolution, scope,
    generation)` to a REST sub-alias `id` and the years it offers. The tuple
    is unique within a product, so `Catalog.pick_subalias` can resolve a
    request to exactly one sub-alias.

    Attributes:
        id: The real REST sub-alias id (`"wpgp"`, `"cic2020_100m"`,
            `"G2_CN_POP_R25A_100m"`, …) — the `{subalias}` path segment.
        constrained: Whether this is the settlement-masked *constrained*
            variant (`True`) or the *unconstrained* variant (`False`).
        unadjusted: `True` for the raw variant; `False` for the
            UN-adjusted variant. WorldPop's "UN adjusted" sub-aliases set
            this `False`, the plain ones `True`. Defaults to `True`.
        resolution: Pixel size — `"100m"` or `"1km"`.
        scope: `"countries"` (per-ISO3 rasters) or `"global"` (a single
            global mosaic).
        generation: The product generation (`"R2021"`, `"R2024B"`,
            `"R2025A"`, `"2024"`).
        level: Aggregation level for products that publish both
            (`pwd`): `"national"` or `"subnational"`. `"national"` for
            every other product.
        archive: The archive format the product is distributed in, or `""`
            for plain per-year GeoTIFFs. `"7z"` (`dependency_ratios`,
            per-continent) and `"zip"` (`future_pop`, per-SSP) products are
            downloaded as an archive and extracted before cropping; `"zip"`
            (multi-GB) additionally requires the `allow_large_archive` opt-in.
        years: The years this sub-alias offers, as a single year
            (`"2020"`) or an inclusive range (`"2000-2020"`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    constrained: bool = False
    unadjusted: bool = True
    resolution: str = "100m"
    scope: str = "countries"
    generation: str = "R2021"
    level: str = "national"
    archive: str = ""
    years: str = "2000-2020"

    def years_set(self) -> set[int]:
        """Return the set of years this sub-alias offers (parsed from `years`)."""
        return _years_set(self.years)

    def selector(self) -> tuple[bool, bool, str, str, str, str]:
        """Return the selector key.

        The key is `(constrained, unadjusted, resolution, scope, generation,
        level)` — unique within a product.
        """
        return (
            self.constrained,
            self.unadjusted,
            self.resolution,
            self.scope,
            self.generation,
            self.level,
        )


class Product(BaseModel):
    """One curated WorldPop product family (a top-level REST alias).

    Attributes:
        alias: Canonical product alias (`"pop"`, `"age_structures"`); set
            from the YAML mapping key by the loader.
        friendly: Friendly names that also resolve to this product
            (`["population", "population_counts"]`).
        kind: `"raster"` (gridded only) or `"mixed"` (gridded **and** a
            tabular demographic breakdown).
        demographic: Whether the product ships per-cohort age/sex rasters
            that earthlens tabularises (only `age_structures` in practice).
        unit: Human-readable unit of the values (`"people/pixel"`).
        worldpoppy_id: The matching WorldPopPy product id for the optional
            `api="worldpoppy"` path, or `None` if unmapped.
        rest_alias: The top-level REST alias to query when it differs from
            the catalog key. Empty (the default) means "use the key". The
            covariate products set this to `"covariates"` (they are
            sub-aliases of the shared `covariates` endpoint).
        description: Human-readable description of the product (the hub's
            title), shown in docs / `describe`.
        subaliases: The concrete variants this product offers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    alias: str = ""
    friendly: list[str] = Field(default_factory=list)
    kind: str = "raster"
    demographic: bool = False
    unit: str = ""
    worldpoppy_id: str | None = None
    rest_alias: str = ""
    description: str = ""
    subaliases: list[SubAlias] = Field(default_factory=list)

    def endpoint(self) -> str:
        """Return the REST alias to query (`rest_alias` or the product key)."""
        return self.rest_alias or self.alias

    def selectors(self) -> list[tuple[bool, bool, str, str, str, str]]:
        """Return every sub-alias selector tuple (for did-you-mean listings)."""
        return [s.selector() for s in self.subaliases]


# Module-level cache of parsed product maps, keyed on `(resolved_path,
# mtime_ns)` so any real file mutation invalidates the entry naturally.
# Mirrors the ECMWF / GEE / tropycal catalog caches so repeated `Catalog()`
# construction skips the YAML parse + pydantic validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level product parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key includes
    `st_mtime_ns`, so any real file mutation invalidates the entry on its
    own.
    """
    _CATALOG_CACHE.clear()


def _parse_products(files: list[Path]) -> dict[str, Product]:
    """Parse and validate the WorldPop catalog rows.

    Args:
        files: The contributing YAML files (WorldPop ships a single file).

    Returns:
        dict[str, Product]: The validated rows.

    Raises:
        ValueError: If a required block is missing or a row fails
            validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    products_yaml = data.get("products") or {}
    if not products_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'products:' block. "
            "The WorldPop catalog must list at least one product."
        )
    products: dict[str, Product] = {}
    for alias, body in products_yaml.items():
        try:
            products[alias] = Product(alias=alias, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} product {alias!r} failed validation:\n{exc}"
            ) from exc
    return products


def _load_products(path: Path) -> dict[str, Product]:
    """Return the parsed WorldPop catalog at `path`, memoised on its mtime.

    Args:
        path: The catalog file.

    Returns:
        dict[str, Product]: From the cache when the file is unchanged.

    Raises:
        ValueError: If the path does not exist, or parsing fails.
    """
    return load_catalog(path, _CATALOG_CACHE, _parse_products, provider="WorldPop")


class Catalog(AbstractCatalog):
    """Product / sub-alias availability catalog for the WorldPop backend.

    Reads the bundled `worldpop_data_catalog.yaml` and exposes its
    `products:` block as a map of `Product` rows keyed by canonical alias
    under the inherited `datasets` field (giving `cat["pop"]`, `"pop" in
    cat`, `len(cat)`, and the did-you-mean error for free). Instantiate
    with no arguments (`Catalog()`); `model_post_init` loads and validates
    the YAML.

    Attributes:
        datasets: Map from canonical product alias to its `Product` row.
        available_datasets: Every curated product alias, sorted.
    """

    _catalog_kind: str = "WorldPop product catalog"
    #: Plural noun for the did-you-mean message ("Known products: …"); the
    #: shared AbstractCatalog reads this (its entries are products, not
    #: "datasets").
    _entry_noun: str = "products"

    datasets: dict[str, Product] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no products were supplied.

        `Catalog()` with no args reads `CATALOG_PATH`; passing
        `datasets=...` skips the disk read (used in tests).

        Raises:
            ValueError: Propagated from `load` when the YAML is missing,
                empty, or has a malformed product row.
        """
        if not self.datasets:
            self.datasets = dict(_load_products(CATALOG_PATH))
        if not self.available_datasets:
            self.available_datasets = sorted(self.datasets)
        self._index_aliases()

    def _index_aliases(self) -> None:
        """Build the alias → canonical-alias lookup from the loaded rows."""
        index: dict[str, str] = {}
        for alias, product in self.datasets.items():
            index[alias.lower()] = alias
            for friendly in product.friendly:
                index[friendly.lower()] = alias
        object.__setattr__(self, "_alias_index", index)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the WorldPop product catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog` with `datasets` and the
            `available_datasets` index set.

        Raises:
            ValueError: If the file has no `products:` block, or a row
                fails `Product` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        products = _load_products(catalog_path)
        return cls(datasets=dict(products), available_datasets=sorted(products))

    def get(self, alias: str) -> Product:
        """Return the `Product` for a canonical alias, did-you-mean on miss.

        Args:
            alias: A canonical product alias (`"pop"`).

        Returns:
            Product: The matching row.

        Raises:
            ValueError: If `alias` is not a curated product.
        """
        return cast("Product", self.get_dataset(alias))

    def resolve(self, key: str) -> str:
        """Resolve a product key or friendly alias to its canonical alias.

        Args:
            key: A canonical alias (`"pop"`) or a friendly alias
                (`"population"`, case-insensitive).

        Returns:
            str: The canonical product alias.

        Raises:
            ValueError: If `key` matches no product or alias; the message
                lists the known products with a did-you-mean hint.

        Examples:
            - A friendly alias and a canonical key both resolve:
                ```python
                >>> from earthlens.worldpop import Catalog
                >>> cat = Catalog()
                >>> cat.resolve("population")
                'pop'
                >>> cat.resolve("pop")
                'pop'

                ```
        """
        index: dict[str, str] = getattr(self, "_alias_index", {})
        canonical = index.get(key.lower())
        if canonical is not None:
            return canonical
        close = difflib.get_close_matches(key.lower(), index, n=1)
        hint = f" Did you mean {index[close[0]]!r}?" if close else ""
        raise ValueError(
            f"{key!r} is not a known WorldPop product or alias. "
            f"Known products: {sorted(self.datasets)}.{hint}"
        )

    def available_products(self) -> list[str]:
        """Return the curated product aliases, sorted."""
        return sorted(self.datasets)

    def pick_subalias(
        self,
        product: str,
        *,
        constrained: bool = False,
        unadjusted: bool = True,
        resolution: str = "100m",
        scope: str = "countries",
        generation: str = "R2021",
        level: str = "national",
    ) -> str:
        """Resolve the selector kwargs to a single REST sub-alias id.

        A product with exactly one sub-alias (`births`, `urban_change`, …)
        returns it directly — the selector kwargs do not apply, since there
        is only one variant. As a guard against a silently-ignored request,
        a `resolution` that differs from the sole sub-alias's resolution
        emits a `UserWarning` (the other selectors are product-intrinsic for
        single-variant products and are not warned on). Otherwise the kwargs
        must match one sub-alias's
        `(constrained, unadjusted, resolution, scope, generation, level)`
        tuple exactly.

        Args:
            product: A product key or alias (resolved first).
            constrained: Settlement-masked variant (`True`) vs
                unconstrained (`False`).
            unadjusted: Raw (`True`) vs UN-adjusted (`False`).
            resolution: `"100m"` or `"1km"`.
            scope: `"countries"` or `"global"`.
            generation: One of `GENERATIONS`.
            level: `"national"` or `"subnational"` (only `pwd` differs).

        Returns:
            str: The matching sub-alias id (e.g. `"wpgp"`).

        Raises:
            ValueError: If no sub-alias matches the selector; the message
                lists the product's available sub-alias tuples.

        Examples:
            - The classic unconstrained 100 m country series resolves to `wpgp`:
                ```python
                >>> from earthlens.worldpop import Catalog
                >>> Catalog().pick_subalias("pop")
                'wpgp'

                ```
        """
        code = self.resolve(product)
        row = self.datasets[code]
        if len(row.subaliases) == 1:
            only = row.subaliases[0]
            if resolution != only.resolution:
                warnings.warn(
                    f"{code!r} offers only the {only.resolution!r} sub-alias "
                    f"{only.id!r}; the requested resolution={resolution!r} is "
                    "ignored.",
                    UserWarning,
                    stacklevel=2,
                )
            return only.id
        want = (constrained, unadjusted, resolution, scope, generation, level)
        for sub in row.subaliases:
            if sub.selector() == want:
                return sub.id
        options = "\n".join(
            f"  - id={s.id!r} constrained={s.constrained} "
            f"unadjusted={s.unadjusted} resolution={s.resolution!r} "
            f"scope={s.scope!r} generation={s.generation!r} level={s.level!r}"
            for s in row.subaliases
        )
        raise ValueError(
            f"{code!r} has no variant for constrained={constrained}, "
            f"unadjusted={unadjusted}, resolution={resolution!r}, scope={scope!r}, "
            f"generation={generation!r}, level={level!r}. "
            f"Available sub-aliases:\n{options}"
        )

    def subalias(self, product: str, subalias_id: str) -> SubAlias:
        """Return the `SubAlias` row of a product by its REST id.

        Args:
            product: A product key or alias (resolved first).
            subalias_id: A sub-alias id belonging to that product.

        Returns:
            SubAlias: The matching sub-alias row.

        Raises:
            ValueError: If `product` is unknown or has no such sub-alias.

        Examples:
            - Look up a sub-alias and read its scope / resolution:
                ```python
                >>> from earthlens.worldpop import Catalog
                >>> sub = Catalog().subalias("pop", "wpgp")
                >>> sub.scope
                'countries'
                >>> sub.resolution
                '100m'

                ```
        """
        code = self.resolve(product)
        for sub in self.datasets[code].subaliases:
            if sub.id == subalias_id:
                return sub
        raise ValueError(
            f"{code!r} has no sub-alias {subalias_id!r}; "
            f"have {[s.id for s in self.datasets[code].subaliases]}."
        )

    def validate(  # type: ignore[override]
        self,
        product: str,
        *,
        constrained: bool = False,
        unadjusted: bool = True,
        resolution: str = "100m",
        scope: str = "countries",
        generation: str = "R2021",
        level: str = "national",
        year: int | None = None,
    ) -> tuple[str, str]:
        """Validate a full request and return `(product, subalias_id)`.

        Resolves the product, picks the sub-alias from the selectors, and —
        when `year` is given — checks the sub-alias offers it.

        Args:
            product: A product key or alias.
            constrained: See `pick_subalias`.
            unadjusted: See `pick_subalias`.
            resolution: See `pick_subalias`.
            scope: See `pick_subalias`.
            generation: See `pick_subalias`.
            level: See `pick_subalias`.
            year: Optional year to check against the sub-alias's `years`.

        Returns:
            tuple[str, str]: The canonical `(product, subalias_id)`.

        Raises:
            ValueError: If the selector matches no sub-alias, or `year` is
                outside the sub-alias's available years.
        """
        code = self.resolve(product)
        subalias_id = self.pick_subalias(
            code,
            constrained=constrained,
            unadjusted=unadjusted,
            resolution=resolution,
            scope=scope,
            generation=generation,
            level=level,
        )
        if year is not None:
            sub = next(s for s in self.datasets[code].subaliases if s.id == subalias_id)
            years = sub.years_set()
            if year not in years:
                raise ValueError(
                    f"{code}/{subalias_id} does not offer year {year}; "
                    f"available years: {sorted(years)}."
                )
        return code, subalias_id

    def describe(self, product: str) -> dict[str, Any]:
        """Return a structured introspection record for a product.

        Mirrors `earthlens.ecmwf.Catalog.describe` / the tropycal catalog: a
        runtime "what does product X expose?" helper a CLI / notebook can
        dump without walking the YAML.

        Args:
            product: A product key or friendly alias (resolved first).

        Returns:
            dict[str, Any]: Keys `product` (canonical alias), `friendly`,
            `kind`, `demographic`, `unit`, and `subaliases` (a list of
            per-variant dicts with `id` / `constrained` / `unadjusted` /
            `resolution` / `scope` / `generation` / `level` / `years`).

        Raises:
            ValueError: If `product` is not a curated product.

        Examples:
            - Describe the population product at a glance:
                ```python
                >>> from earthlens.worldpop import Catalog
                >>> info = Catalog().describe("population")
                >>> info["product"]
                'pop'
                >>> info["kind"]
                'raster'
                >>> info["subaliases"][0]["id"]
                'wpgp'

                ```
        """
        code = self.resolve(product)
        row = self.datasets[code]
        return {
            "product": code,
            "friendly": list(row.friendly),
            "kind": row.kind,
            "demographic": row.demographic,
            "unit": row.unit,
            "description": row.description,
            "endpoint": row.endpoint(),
            "subaliases": [
                {
                    "id": sub.id,
                    "constrained": sub.constrained,
                    "unadjusted": sub.unadjusted,
                    "resolution": sub.resolution,
                    "scope": sub.scope,
                    "generation": sub.generation,
                    "level": sub.level,
                    "years": sub.years,
                }
                for sub in row.subaliases
            ],
        }

    def health(self) -> dict[str, list[str]]:
        """Report structural hygiene issues across the loaded catalog.

        Mirrors `earthlens.ecmwf.Catalog.health` / `earthlens.gee.Catalog.health`:
        returns a mapping `check_name -> sorted list of offenders`. An empty
        list means the check passes. Schema-level invariants (duplicate keys,
        unknown fields) are already enforced at load time — these are the
        residual data-quality checks the pydantic schema cannot express.

        Checks reported:

        * `product_without_subaliases` — products carrying zero sub-aliases.
        * `demographic_not_mixed` — products flagged `demographic` whose
          `kind` is not `"mixed"`.
        * `subalias_unknown_generation` — `"<product>:<id>"` whose
          `generation` is not in `GENERATIONS`.
        * `subalias_bad_years` — `"<product>:<id>"` whose `years` spec does
          not parse.

        Returns:
            dict[str, list[str]]: The per-check offender lists.

        Examples:
            - The bundled catalog is clean:
                ```python
                >>> from earthlens.worldpop import Catalog
                >>> Catalog().health()
                {'product_without_subaliases': [], 'demographic_not_mixed': [], 'subalias_unknown_generation': [], 'subalias_bad_years': []}

                ```
        """
        no_subaliases: list[str] = []
        demographic_not_mixed: list[str] = []
        unknown_generation: list[str] = []
        bad_years: list[str] = []
        for alias, product in self.datasets.items():
            if not product.subaliases:
                no_subaliases.append(alias)
            if product.demographic and product.kind != "mixed":
                demographic_not_mixed.append(alias)
            for sub in product.subaliases:
                if sub.generation not in GENERATIONS:
                    unknown_generation.append(f"{alias}:{sub.id}")
                try:
                    sub.years_set()
                except ValueError:
                    bad_years.append(f"{alias}:{sub.id}")
        return {
            "product_without_subaliases": sorted(no_subaliases),
            "demographic_not_mixed": sorted(demographic_not_mixed),
            "subalias_unknown_generation": sorted(unknown_generation),
            "subalias_bad_years": sorted(bad_years),
        }
