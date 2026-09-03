"""Two-layer catalog for the openEO backend: collections + curated recipes.

openEO is compute, not fetch, so the "catalog" has two layers (the headline
design decision, `G1`):

* **Collections** — the underlying CDSE openEO collections (`SENTINEL2_L2A`,
  `SENTINEL1_GRD`, `SENTINEL_5P_L2`, …) with their bands. A request that names a
  collection gets the default graph (`load_collection → clip → save_result`).
* **Recipes** — a curated library of named **process graphs**
  (`sentinel-2-l2a-ndvi-monthly`, `sentinel-2-l2a-cloud-masked-composite`, …),
  each fixing a base collection + bands + an ordered list of DataCube steps
  (mask → reduce → save). A request that names a recipe gets that fixed graph.

The catalog ships as a directory of YAML files at
`src/earthlens/openeo/catalog/` (`collections.yaml`, `recipes.yaml`, plus
`_index.yaml` carrying the informational `available_collections:` /
`available_processes:` index rebuilt by `tools/openeo/refresh_openeo_catalog.py`).
The loader unions the per-file `collections:` / `recipes:` blocks into one
:class:`Catalog` (a key declared in two files is a load-time error), the same way
the GEE / STAC / CMEMS catalogs merge, and caches on `(path, mtime_ns)`.

The bundled catalog directory lives at :data:`CATALOG_PATH`; tests can
monkey-patch that attribute to redirect the loader at a temporary directory or a
single YAML file.
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

# Module-level parse cache, keyed on the resolved path plus a tuple of
# `(file, st_mtime_ns)` for every YAML the load touched — editing any file
# invalidates the entry without inspecting rows. Mirrors the STAC / GEE caches.
_CATALOG_CACHE: dict[
    Any,
    tuple[
        dict[str, Collection],
        dict[str, Recipe],
        list[str],
        list[str],
    ],
] = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='openEO')


class Extent(BaseModel):
    """Spatial/temporal coverage of an openEO collection.

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
    """Per-band metadata for one band of an openEO collection.

    Frozen value object; the band name is the parent mapping key and is not
    repeated in the body. Mirrors the per-band/asset/variable models the other
    backends use (`earthlens.gee.Band`, `earthlens.stac.Asset`,
    `earthlens.cmems.Variable`). Every field is optional because openEO band
    metadata (`eo:bands`) is uneven across collections.

    Attributes:
        common_name: STAC `eo:bands` common name (`"red"`, `"nir"`), or `None`.
        description: Human description of the band, or `None`.
        units: Physical unit string (openEO `unit`), or `None`.
        dtype: On-disk data type (`"int16"`, `"float32"`, …), or `None`.
        gsd: Band ground sample distance in metres, or `None`.
        center_wavelength: Central wavelength in micrometres (optical), or `None`.
        min: Typical/declared minimum value, or `None`.
        max: Typical/declared maximum value, or `None`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    common_name: str | None = None
    description: str | None = None
    units: str | None = None
    dtype: str | None = None
    gsd: float | None = None
    center_wavelength: float | None = None
    min: float | None = None
    max: float | None = None


class Collection(BaseModel):
    """One curated CDSE openEO collection, addressed by a logical key.

    Attributes:
        collection_id: The UPPERCASE openEO collection id this key loads
            (`"SENTINEL2_L2A"`, `"SENTINEL_5P_L2"`, …).
        bands: Band name → :class:`Band` metadata for every band the collection
            exposes (the full set from `describe_collection`).
        default_bands: Bands pulled when the request names none. Falls back to
            every key of `bands` when empty.
        cadence: Native revisit cadence label, or `None`.
        resolution: Native ground sample distance in metres, or `None`.
        extent: Spatial/temporal coverage.
        description: One-line human summary, or `None`.
        cloud_cover: Whether the collection exposes an `eo:cloud_cover` property
            that the `max_cloud_cover` `load_collection` filter can act on (the
            optical missions: Sentinel-2 L1C / L2A). `False` for SAR, atmosphere,
            elevation, and composite collections, so the backend can reject a
            `max_cloud_cover=` that the collection would ignore / error on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection_id: str
    bands: dict[str, Band] = Field(default_factory=dict)
    default_bands: list[str] = Field(default_factory=list)
    cadence: str | None = None
    resolution: float | None = None
    extent: Extent | None = None
    description: str | None = None
    cloud_cover: bool = False

    @property
    def effective_bands(self) -> list[str]:
        """The band names to request by default (`default_bands`, else all bands)."""
        return list(self.default_bands or list(self.bands))


class Recipe(BaseModel):
    """One curated process graph fixing a base collection, bands, and steps.

    Attributes:
        base_collection: The UPPERCASE openEO collection id the graph loads
            (e.g. `"SENTINEL2_L2A"`).
        bands: Bands to load for the graph. An empty list lets `load_collection`
            pull every band (rarely what a recipe wants).
        graph: Ordered DataCube steps applied after `load_collection`. Each step
            is a single-key mapping `{process_name: {param: value, ...}}` — e.g.
            `{"ndvi": {"nir": "B08", "red": "B04"}}`. A step whose process is a
            DataCube method is dispatched to it; otherwise it is applied via the
            generic `DataCube.process` (with the cube bound as `data`).
        output_format: openEO output format for this recipe (`"GTiff"` /
            `"netCDF"`), or `None` to use the backend default.
        description: One-line human summary, or `None`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_collection: str
    bands: list[str] = Field(default_factory=list)
    graph: list[dict[str, dict[str, Any]]] = Field(default_factory=list)
    output_format: str | None = None
    description: str | None = None

    @field_validator("graph")
    @classmethod
    def _check_single_key_steps(
        cls, value: list[dict[str, dict[str, Any]]]
    ) -> list[dict[str, dict[str, Any]]]:
        """Validate each graph step is a single `{process: params}` mapping.

        Args:
            value: The ordered graph steps.

        Returns:
            The validated steps.

        Raises:
            ValueError: If a step is not a single-key mapping.
        """
        for step in value:
            if len(step) != 1:
                raise ValueError(
                    f"each recipe graph step must be a single "
                    f"{{process: params}} mapping, got {step!r}."
                )
        return value


class ResolvedGraph(BaseModel):
    """The uniform shape a resolved collection-or-recipe key takes.

    Both a plain collection and a recipe resolve to this so the backend's
    graph-builder consumes one type. A collection resolves to an empty `graph`
    (default load → clip → save); a recipe carries its fixed steps.

    Attributes:
        key: The logical key the request named.
        collection_id: The UPPERCASE openEO collection id to `load_collection`.
        bands: The bands to load (request override applied by the backend).
        graph: Ordered DataCube steps (`[]` for a plain collection).
        output_format: Preferred output format, or `None` for the backend
            default.
        is_recipe: Whether the key named a recipe (vs a plain collection).
        supports_cloud_cover: Whether the loaded collection exposes
            `eo:cloud_cover`, so the backend may forward `max_cloud_cover=`.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    collection_id: str
    bands: list[str] = Field(default_factory=list)
    graph: list[dict[str, dict[str, Any]]] = Field(default_factory=list)
    output_format: str | None = None
    is_recipe: bool = False
    supports_cloud_cover: bool = False


def _load_catalog_data(
    path: Path,
) -> tuple[dict[str, Collection], dict[str, Recipe], list[str], list[str]]:
    """Parse, validate, and cache the openEO catalog at `path`.

    Merges every `*.yaml` under a directory: `collections:` and `recipes:` maps
    are unioned (a duplicate key across files is an error), and the
    `available_collections:` / `available_processes:` lists are concatenated
    (de-duplicated, sorted). Cached on the resolved path plus every contributing
    file's `mtime_ns`.

    Args:
        path: Catalog directory (default) or a single `*.yaml` file.

    Returns:
        `(collections, recipes, available_collections, available_processes)`.

    Raises:
        ValueError: On a missing `collections:`/`recipes:` content, a duplicate
            key, an invalid row, or a recipe whose `base_collection` is unknown
            to any declared collection (when collections are present).
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    collections_yaml: dict[str, Any] = {}
    recipes_yaml: dict[str, Any] = {}
    available_collections: list[str] = []
    available_processes: list[str] = []
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
        available_processes.extend(data.get("available_processes") or [])

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

    recipes: dict[str, Recipe] = {}
    for rec_key, rec_body in recipes_yaml.items():
        try:
            recipes[rec_key] = Recipe(**dict(rec_body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"invalid recipe {rec_key!r} in {origin['r:' + rec_key]}: {exc}"
            ) from exc

    cached_value = (
        collections,
        recipes,
        sorted(set(available_collections)),
        sorted(set(available_processes)),
    )
    _CATALOG_CACHE[key] = cached_value
    return cached_value


class Catalog(AbstractCatalog[Collection]):
    """YAML-backed catalog of openEO collections and curated recipes.

    Reads every `*.yaml` under :data:`CATALOG_PATH` on construction and merges
    them into typed :class:`Collection` / :class:`Recipe` models. Collections are
    stored under the inherited :attr:`datasets` field (keyed by logical key);
    recipes live in :attr:`recipes`; the informational indexes live in
    :attr:`available_collections` / :attr:`available_processes`.

    Attributes:
        datasets: Logical collection key → :class:`Collection`.
        recipes: Recipe key → :class:`Recipe`.
        available_collections: Every collection id the backend serves
            (refreshed index; informational).
        available_processes: Every process id the backend advertises (refreshed
            index; informational).

    Examples:
        - A recipe resolves to its fixed graph; a collection to a bare load:
            ```python
            >>> from earthlens.openeo import Catalog
            >>> cat = Catalog()
            >>> cat.is_recipe("sentinel-2-l2a-ndvi-monthly")
            True
            >>> cat.get_collection("sentinel-2-l2a").collection_id
            'SENTINEL2_L2A'

            ```
        - Resolving normalises both layers to one shape:
            ```python
            >>> from earthlens.openeo import Catalog
            >>> resolved = Catalog().resolve("sentinel-1-grd")
            >>> resolved.collection_id, resolved.is_recipe
            ('SENTINEL1_GRD', False)

            ```
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _catalog_kind: str = "openEO catalog"

    datasets: dict[str, Collection] = Field(default_factory=dict)
    recipes: dict[str, Recipe] = Field(default_factory=dict)
    available_collections: list[str] = Field(default_factory=list)
    available_processes: list[str] = Field(default_factory=list)

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
            self.available_processes = loaded.available_processes
        if not self.available_datasets:
            self.available_datasets = list(self.available_collections)
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the openEO catalog from disk (cached).

        Args:
            catalog_path: Catalog directory or single `*.yaml` file. Defaults to
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: Propagated from the loader.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        collections, recipes, av_cols, av_procs = _load_catalog_data(catalog_path)
        return cls(
            datasets=dict(collections),
            recipes=dict(recipes),
            available_collections=list(av_cols),
            available_processes=list(av_procs),
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

    def get_recipe(self, recipe_key: str) -> Recipe:
        """Return the :class:`Recipe` for `recipe_key` (did-you-mean on miss).

        Args:
            recipe_key: Recipe key.

        Returns:
            The matching :class:`Recipe`.

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

    def _collection_id_has_cloud_cover(self, collection_id: str) -> bool:
        """Whether any curated collection with this id exposes `eo:cloud_cover`.

        Recipes reference a raw `collection_id` (not a logical key), so this
        reverse-looks-up the curated collection to inherit its `cloud_cover`
        flag. Returns `False` when the id is not curated.

        Args:
            collection_id: The UPPERCASE openEO collection id a recipe loads.

        Returns:
            `True` when a curated collection with that id sets `cloud_cover`.
        """
        return any(
            col.collection_id == collection_id and col.cloud_cover
            for col in self.datasets.values()
        )

    def is_recipe(self, key: str) -> bool:
        """Return whether `key` names a curated recipe (vs a plain collection).

        Args:
            key: A logical collection or recipe key.

        Returns:
            `True` when `key` is a recipe.
        """
        return key in self.recipes

    def resolve(self, key: str) -> ResolvedGraph:
        """Resolve a collection-or-recipe key to a uniform :class:`ResolvedGraph`.

        A recipe resolves to its fixed graph + bands + output format; a plain
        collection resolves to an empty graph (a bare load → clip → save) with
        its default bands.

        Args:
            key: A logical collection or recipe key.

        Returns:
            The normalised :class:`ResolvedGraph` the backend builds from.

        Raises:
            ValueError: If `key` is neither a known recipe nor collection
                (message suggests the closest key across both layers).

        Examples:
            - A recipe carries its graph steps:
                ```python
                >>> from earthlens.openeo import Catalog
                >>> g = Catalog().resolve("sentinel-2-l2a-ndvi-monthly")
                >>> g.is_recipe, g.collection_id
                (True, 'SENTINEL2_L2A')
                >>> [next(iter(step)) for step in g.graph]
                ['mask_scl_dilation', 'ndvi', 'aggregate_temporal_period']

                ```
        """
        if key in self.recipes:
            recipe = self.recipes[key]
            return ResolvedGraph(
                key=key,
                collection_id=recipe.base_collection,
                bands=list(recipe.bands),
                graph=list(recipe.graph),
                output_format=recipe.output_format,
                is_recipe=True,
                supports_cloud_cover=self._collection_id_has_cloud_cover(
                    recipe.base_collection
                ),
            )
        if key in self.datasets:
            collection = self.datasets[key]
            return ResolvedGraph(
                key=key,
                collection_id=collection.collection_id,
                bands=collection.effective_bands,
                graph=[],
                output_format=None,
                is_recipe=False,
                supports_cloud_cover=collection.cloud_cover,
            )
        import difflib

        known = sorted(set(self.recipes) | set(self.datasets))
        close = difflib.get_close_matches(key, known, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"{key!r} is not a known openEO collection or recipe. "
            f"Known keys: {known}.{hint}"
        )
