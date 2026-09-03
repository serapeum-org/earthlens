"""Two-layer catalog for the Sentinel Hub backend: collections + evalscripts.

Sentinel Hub renders from an evalscript — there is no granule to fetch — so the
"catalog" has two layers (the headline design decision, `G1`):

* **Collections** — the underlying Sentinel Hub `DataCollection`s
  (`SENTINEL2_L2A`, `SENTINEL1_IW`, `SENTINEL3_OLCI`, …) with their bands. A
  request names a collection (plus an explicit `evalscript=`).
* **Evalscript recipes** — a curated library of named, parametric `.js` files
  bundled under `evalscripts/` (`sentinel-2-l2a-ndvi`, `…-true-colour`, …), each
  fixing a base collection + the render logic. A recipe is either a `"render"`
  recipe (writes a raster) or a `"stats"` recipe (emits the `dataMask` band the
  Statistical API requires). A request that names a recipe gets that fixed
  evalscript over its collection.

The catalog ships as a directory of YAML files at
`src/earthlens/sentinel_hub/catalog/` (`collections.yaml`, `recipes.yaml`, plus
`_index.yaml` carrying the informational `available_collections:` index rebuilt
by `tools/sentinel_hub/refresh_sh_catalog.py`). The loader unions the per-file
`collections:` / `recipes:` blocks into one :class:`Catalog` (a key declared in
two files is a load-time error) and caches on `(path, mtime_ns)`, the same way
the openEO / GEE / STAC catalogs do. The evalscript `.js` files live alongside
under `evalscripts/` and are read at request time (not parsed here).

The bundled catalog directory lives at :data:`CATALOG_PATH` and the evalscript
directory at :data:`EVALSCRIPTS_PATH`; tests can monkey-patch either to redirect
the loader at a temporary directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catalog"
EVALSCRIPTS_PATH: Path = Path(__file__).parent / "evalscripts"

#: Allowed values of a recipe's `kind` field.
_RECIPE_KINDS: frozenset[str] = frozenset({"render", "stats"})

# Module-level parse cache, keyed on the resolved path plus a tuple of
# `(file, st_mtime_ns)` for every YAML the load touched — editing any file
# invalidates the entry. Mirrors the openEO / STAC / GEE caches.
_CATALOG_CACHE: dict[
    Any,
    tuple[dict[str, Collection], dict[str, EvalscriptRecipe], list[str]],
] = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='Sentinel Hub')


class Extent(BaseModel):
    """Spatial/temporal coverage of a Sentinel Hub collection.

    Attributes:
        start_date: First available date (`YYYY-MM-DD`), or `None`.
        end_date: Last available date, or `None` for a rolling collection.
        bbox: `[west, south, east, north]` in EPSG:4326, or `None` for global.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_date: str | None = None
    end_date: str | None = None
    bbox: tuple[float, float, float, float] | None = None


class Band(BaseModel):
    """Per-band metadata for one band of a Sentinel Hub collection.

    Frozen value object; the band name is the parent mapping key and is not
    repeated in the body. Every field is optional because band metadata is
    uneven across collections.

    Attributes:
        common_name: Common name (`"red"`, `"nir"`), or `None`.
        description: Human description of the band, or `None`.
        units: Physical unit string, or `None`.
        resolution: Native ground sample distance in metres, or `None`.
        center_wavelength: Central wavelength in micrometres (optical), or `None`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    common_name: str | None = None
    description: str | None = None
    units: str | None = None
    resolution: float | None = None
    center_wavelength: float | None = None


class Collection(BaseModel):
    """One curated Sentinel Hub data collection, addressed by a logical key.

    Attributes:
        sh_collection: The `sentinelhub.DataCollection` member name this key
            binds to (`"SENTINEL2_L2A"`, `"SENTINEL1_IW"`, …).
        bands: Band name → :class:`Band` metadata for the bands the collection
            exposes.
        default_bands: Bands used when the request names none. Falls back to
            every key of `bands` when empty.
        cadence: Native revisit cadence label, or `None`.
        resolution: Native ground sample distance in metres, or `None`.
        extent: Spatial/temporal coverage, or `None`.
        description: One-line human summary, or `None`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sh_collection: str
    bands: dict[str, Band] = Field(default_factory=dict)
    default_bands: list[str] = Field(default_factory=list)
    cadence: str | None = None
    resolution: float | None = None
    extent: Extent | None = None
    description: str | None = None

    @property
    def effective_bands(self) -> list[str]:
        """The band names to request by default (`default_bands`, else all bands)."""
        return list(self.default_bands or list(self.bands))


class EvalscriptRecipe(BaseModel):
    """One curated evalscript recipe fixing a base collection + a `.js` file.

    Attributes:
        base_collection: The `DataCollection` member name the recipe renders
            (e.g. `"SENTINEL2_L2A"`).
        evalscript: The bundled `.js` filename under `evalscripts/`
            (e.g. `"ndvi.js"`).
        bands: The collection bands the evalscript consumes (informational +
            used by the refresh/validate tool).
        output_bands: The number of bands the evalscript writes.
        kind: `"render"` (writes a raster) or `"stats"` (emits a `dataMask` band
            for the Statistical API).
        description: One-line human summary, or `None`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_collection: str
    evalscript: str
    bands: list[str] = Field(default_factory=list)
    output_bands: int = 1
    kind: str = "render"
    description: str | None = None

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        """Validate `kind` is one of the recognised recipe kinds.

        Args:
            value: The recipe kind.

        Returns:
            The validated kind.

        Raises:
            ValueError: If `kind` is not `"render"` or `"stats"`.
        """
        if value not in _RECIPE_KINDS:
            raise ValueError(
                f"recipe kind must be one of {sorted(_RECIPE_KINDS)}, got {value!r}."
            )
        return value


class ResolvedRequest(BaseModel):
    """The uniform shape a resolved collection-or-recipe key takes.

    Both a plain collection and a recipe resolve to this so the backend's
    request-builder consumes one type. A plain collection resolves with
    `evalscript=None` (the backend must then have an explicit `evalscript=`); a
    recipe carries its bundled `.js` filename.

    Attributes:
        key: The logical key the request named.
        sh_collection: The `DataCollection` member name to bind + render.
        bands: The bands to request (request override applied by the backend).
        evalscript: The bundled `.js` filename, or `None` for a plain collection.
        output_bands: Number of output bands the evalscript writes.
        is_recipe: Whether the key named a recipe (vs a plain collection).
        kind: `"render"` or `"stats"` (recipes only; `"render"` for a plain
            collection).
    """

    model_config = ConfigDict(frozen=True)

    key: str
    sh_collection: str
    bands: list[str] = Field(default_factory=list)
    evalscript: str | None = None
    output_bands: int = 1
    is_recipe: bool = False
    kind: str = "render"


def read_evalscript(filename: str) -> str:
    """Read a bundled evalscript `.js` file by name.

    Args:
        filename: The `.js` filename (e.g. `"ndvi.js"`) under
            :data:`EVALSCRIPTS_PATH`.

    Returns:
        The evalscript source.

    Raises:
        FileNotFoundError: When no such bundled evalscript exists.
    """
    path = EVALSCRIPTS_PATH / filename
    if not path.is_file():
        available = sorted(p.name for p in EVALSCRIPTS_PATH.glob("*.js"))
        raise FileNotFoundError(
            f"bundled evalscript {filename!r} not found under {EVALSCRIPTS_PATH}. "
            f"Available: {available}."
        )
    return path.read_text(encoding="utf-8")


def _load_catalog_data(
    path: Path,
) -> tuple[dict[str, Collection], dict[str, EvalscriptRecipe], list[str]]:
    """Parse, validate, and cache the Sentinel Hub catalog at `path`.

    Merges every `*.yaml` under a directory: `collections:` and `recipes:` maps
    are unioned (a duplicate key across files is an error), and the
    `available_collections:` lists are concatenated (de-duplicated, sorted).
    Cached on the resolved path plus every contributing file's `mtime_ns`.

    Args:
        path: Catalog directory (default) or a single `*.yaml` file.

    Returns:
        `(collections, recipes, available_collections)`.

    Raises:
        ValueError: On missing `collections:`/`recipes:` content, a duplicate
            key, or an invalid row.
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    collections_yaml: dict[str, Any] = {}
    recipes_yaml: dict[str, Any] = {}
    available_collections: list[str] = []
    origin: dict[str, Path] = {}
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        for col_key, col_body in (data.get("collections") or {}).items():
            if col_key in collections_yaml:
                raise ValueError(
                    f"collection {col_key!r} declared in two catalog files: "
                    f"{origin['c:' + col_key]} and {file_path}"
                )
            collections_yaml[col_key] = col_body
            origin["c:" + col_key] = file_path
        for rec_key, rec_body in (data.get("recipes") or {}).items():
            if rec_key in recipes_yaml:
                raise ValueError(
                    f"recipe {rec_key!r} declared in two catalog files: "
                    f"{origin['r:' + rec_key]} and {file_path}"
                )
            recipes_yaml[rec_key] = rec_body
            origin["r:" + rec_key] = file_path
        available_collections.extend(data.get("available_collections") or [])

    if not collections_yaml and not recipes_yaml:
        raise ValueError(
            f"{path} has no 'collections:' or 'recipes:' content. The catalog "
            "must contain at least one curated collection or recipe."
        )

    collections: dict[str, Collection] = {}
    for col_key, col_body in collections_yaml.items():
        body = dict(col_body or {})
        extent_body = body.pop("extent", None)
        bands_yaml = dict(body.pop("bands", {}) or {})
        try:
            bands = {
                name: Band(**dict(band_body or {}))
                for name, band_body in bands_yaml.items()
            }
            extent = Extent(**dict(extent_body)) if extent_body else None
            collections[col_key] = Collection(extent=extent, bands=bands, **body)
        except ValidationError as exc:
            raise ValueError(
                f"invalid collection {col_key!r} in {origin['c:' + col_key]}: {exc}"
            ) from exc

    recipes: dict[str, EvalscriptRecipe] = {}
    for rec_key, rec_body in recipes_yaml.items():
        try:
            recipes[rec_key] = EvalscriptRecipe(**dict(rec_body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"invalid recipe {rec_key!r} in {origin['r:' + rec_key]}: {exc}"
            ) from exc

    cached_value = (collections, recipes, sorted(set(available_collections)))
    _CATALOG_CACHE[key] = cached_value
    return cached_value


class Catalog(AbstractCatalog[Collection]):
    """YAML-backed catalog of Sentinel Hub collections and evalscript recipes.

    Reads every `*.yaml` under :data:`CATALOG_PATH` on construction and merges
    them into typed :class:`Collection` / :class:`EvalscriptRecipe` models.
    Collections are stored under the inherited :attr:`datasets` field (keyed by
    logical key); recipes live in :attr:`recipes`; the informational index lives
    in :attr:`available_collections`.

    Attributes:
        datasets: Logical collection key → :class:`Collection`.
        recipes: Recipe key → :class:`EvalscriptRecipe`.
        available_collections: Every collection the backend can render
            (refreshed index; informational).

    Examples:
        - A recipe resolves to its bundled evalscript; a collection to a bare bind:
            ```python
            >>> from earthlens.sentinel_hub import Catalog
            >>> cat = Catalog()
            >>> cat.is_recipe("sentinel-2-l2a-ndvi")
            True
            >>> cat.get_collection("sentinel-2-l2a").sh_collection
            'SENTINEL2_L2A'

            ```
        - Resolving normalises both layers to one shape:
            ```python
            >>> from earthlens.sentinel_hub import Catalog
            >>> r = Catalog().resolve("sentinel-2-l2a-ndvi")
            >>> r.sh_collection, r.evalscript, r.is_recipe
            ('SENTINEL2_L2A', 'ndvi.js', True)

            ```
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _catalog_kind: str = "Sentinel Hub catalog"

    datasets: dict[str, Collection] = Field(default_factory=dict)
    recipes: dict[str, EvalscriptRecipe] = Field(default_factory=dict)
    available_collections: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no rows were supplied.

        `Catalog()` with no args reads the bundled `catalog/` directory through
        the `(path, mtime)`-keyed cache. If the caller passed `datasets=` or
        `recipes=`, the disk read is skipped (in-memory catalogs for tests).
        The base `available_datasets` field is mirrored from
        `available_collections` so the index is discoverable through the
        `AbstractCatalog` contract, then `super().model_post_init` populates
        `catalog` from `get_catalog()`.

        Raises:
            ValueError: When auto-loading, propagates the loader's errors.
        """
        if not (self.datasets or self.recipes):
            loaded = Catalog.load()
            self.datasets = loaded.datasets
            self.recipes = loaded.recipes
            self.available_collections = loaded.available_collections
        if not self.available_datasets:
            self.available_datasets = list(self.available_collections)
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the Sentinel Hub catalog from disk (cached).

        Args:
            catalog_path: Catalog directory or single `*.yaml` file. Defaults to
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: Propagated from the loader.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        collections, recipes, av_cols = _load_catalog_data(catalog_path)
        return cls(
            datasets=dict(collections),
            recipes=dict(recipes),
            available_collections=list(av_cols),
        )

    def get_collection(self, collection_key: str) -> Collection:
        """Return the :class:`Collection` for `collection_key` (did-you-mean on miss).

        Alias of the inherited :meth:`get_dataset`, named for clarity.

        Args:
            collection_key: Logical collection key.

        Returns:
            The matching :class:`Collection`.

        Raises:
            ValueError: If the key is unknown (message suggests the closest key).
        """
        return cast("Collection", self.get_dataset(collection_key))

    def get_recipe(self, recipe_key: str) -> EvalscriptRecipe:
        """Return the :class:`EvalscriptRecipe` for `recipe_key` (did-you-mean on miss).

        Args:
            recipe_key: Recipe key.

        Returns:
            The matching :class:`EvalscriptRecipe`.

        Raises:
            ValueError: If the key is unknown (message suggests the closest key).
        """
        import difflib

        try:
            return self.recipes[recipe_key]
        except KeyError:
            close = difflib.get_close_matches(recipe_key, self.recipes, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{recipe_key!r} is not a known recipe. "
                f"Known recipes: {sorted(self.recipes)}.{hint}"
            ) from None

    def is_recipe(self, key: str) -> bool:
        """Return whether `key` names a curated evalscript recipe.

        Args:
            key: A logical collection or recipe key.

        Returns:
            `True` when `key` is a recipe.
        """
        return key in self.recipes

    def resolve(self, key: str) -> ResolvedRequest:
        """Resolve a collection-or-recipe key to a uniform :class:`ResolvedRequest`.

        A recipe resolves to its bundled evalscript + bands + kind; a plain
        collection resolves with `evalscript=None` and its default bands (the
        backend then requires an explicit `evalscript=`).

        Args:
            key: A logical collection or recipe key.

        Returns:
            The normalised :class:`ResolvedRequest` the backend builds from.

        Raises:
            ValueError: If `key` is neither a known recipe nor collection
                (message suggests the closest key across both layers).

        Examples:
            - A recipe carries its evalscript + kind:
                ```python
                >>> from earthlens.sentinel_hub import Catalog
                >>> r = Catalog().resolve("sentinel-2-l2a-ndvi-stats")
                >>> r.kind, r.evalscript
                ('stats', 'ndvi_stats.js')

                ```
        """
        if key in self.recipes:
            recipe = self.recipes[key]
            return ResolvedRequest(
                key=key,
                sh_collection=recipe.base_collection,
                bands=list(recipe.bands),
                evalscript=recipe.evalscript,
                output_bands=recipe.output_bands,
                is_recipe=True,
                kind=recipe.kind,
            )
        if key in self.datasets:
            collection = self.datasets[key]
            return ResolvedRequest(
                key=key,
                sh_collection=collection.sh_collection,
                bands=collection.effective_bands,
                evalscript=None,
                is_recipe=False,
                kind="render",
            )
        import difflib

        known = sorted(set(self.recipes) | set(self.datasets))
        close = difflib.get_close_matches(key, known, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"{key!r} is not a known Sentinel Hub collection or recipe. "
            f"Known keys: {known}.{hint}"
        )
