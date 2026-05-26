"""Unit tests for `earthlens.openeo.catalog` (two-layer collection/recipe load)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.openeo import catalog as catalog_mod
from earthlens.openeo.catalog import (
    Catalog,
    Recipe,
    ResolvedGraph,
    _load_catalog_data,
    clear_catalog_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the module parse cache around each catalog test."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


@pytest.mark.openeo
class TestBundledCatalog:
    """The shipped catalog loads, resolves, and indexes correctly."""

    def test_collections_and_recipes_present(self):
        """Both curated layers load with the expected keys."""
        cat = Catalog()
        assert "sentinel-2-l2a" in cat.datasets
        assert "sentinel-2-l2a-ndvi-monthly" in cat.recipes

    def test_get_collection_id(self):
        """A collection resolves its UPPERCASE openEO id."""
        assert Catalog().get_collection("sentinel-2-l2a").collection_id == (
            "SENTINEL2_L2A"
        )

    def test_is_recipe(self):
        """`is_recipe` distinguishes recipes from collections."""
        cat = Catalog()
        assert cat.is_recipe("sentinel-2-l2a-ndvi-monthly")
        assert not cat.is_recipe("sentinel-2-l2a")

    def test_resolve_recipe_carries_graph(self):
        """Resolving a recipe yields its ordered graph steps."""
        g = Catalog().resolve("sentinel-2-l2a-ndvi-monthly")
        assert g.is_recipe and g.collection_id == "SENTINEL2_L2A"
        assert [next(iter(s)) for s in g.graph] == [
            "mask_scl_dilation",
            "ndvi",
            "aggregate_temporal_period",
        ]

    def test_resolve_collection_empty_graph(self):
        """Resolving a collection yields default bands and no graph."""
        g = Catalog().resolve("sentinel-1-grd")
        assert g.graph == [] and g.bands == ["VV", "VH"] and not g.is_recipe

    def test_available_index_covers_curated_ids(self):
        """Every curated collection id is a member of the available index."""
        cat = Catalog()
        curated = {c.collection_id for c in cat.datasets.values()}
        curated |= {r.base_collection for r in cat.recipes.values()}
        assert curated.issubset(set(cat.available_collections))

    def test_available_processes_populated(self):
        """The available-processes index is non-empty (refreshed from live)."""
        assert len(Catalog().available_processes) > 50

    def test_get_catalog_returns_collections(self):
        """`get_catalog` returns the curated collection map."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_effective_bands_falls_back_to_all(self):
        """A collection with no default_bands falls back to all bands."""
        from earthlens.openeo.catalog import Collection

        col = Collection(collection_id="X", bands=["a", "b"])
        assert col.effective_bands == ["a", "b"]


@pytest.mark.openeo
class TestDidYouMean:
    """Unknown keys raise ValueError with a closest-match hint."""

    def test_resolve_unknown_key(self):
        """Resolving an unknown key suggests the closest collection/recipe."""
        with pytest.raises(ValueError, match="not a known openEO collection"):
            Catalog().resolve("sentinel2")

    def test_get_recipe_unknown(self):
        """An unknown recipe key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="not a known recipe"):
            Catalog().get_recipe("ndvi")


@pytest.mark.openeo
class TestRecipeValidation:
    """`Recipe` rejects malformed graph steps; `extra='forbid'` holds."""

    def test_multi_key_step_rejected(self):
        """A graph step with two processes is rejected."""
        with pytest.raises(ValueError, match="single"):
            Recipe(base_collection="X", graph=[{"a": {}, "b": {}}])

    def test_unknown_field_rejected(self):
        """An unknown recipe field is rejected (extra='forbid')."""
        with pytest.raises(ValueError):
            Recipe(base_collection="X", bogus=1)


@pytest.mark.openeo
class TestLoaderFromDisk:
    """The multi-file loader merges, de-dups, caches, and errors cleanly."""

    def test_single_file_load(self, tmp_path: Path):
        """A single YAML file is a valid catalog source."""
        path = tmp_path / "cat.yaml"
        path.write_text(
            "collections:\n  k:\n    collection_id: K\n", encoding="utf-8"
        )
        collections, recipes, _, _ = _load_catalog_data(path)
        assert collections["k"].collection_id == "K" and recipes == {}

    def test_duplicate_collection_key_errors(self, tmp_path: Path):
        """A collection key declared in two files is a load-time error."""
        (tmp_path / "a.yaml").write_text(
            "collections:\n  k:\n    collection_id: A\n", encoding="utf-8"
        )
        (tmp_path / "b.yaml").write_text(
            "collections:\n  k:\n    collection_id: B\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="declared in two catalog files"):
            _load_catalog_data(tmp_path)

    def test_duplicate_recipe_key_errors(self, tmp_path: Path):
        """A recipe key declared in two files is a load-time error."""
        (tmp_path / "a.yaml").write_text(
            "recipes:\n  r:\n    base_collection: A\n", encoding="utf-8"
        )
        (tmp_path / "b.yaml").write_text(
            "recipes:\n  r:\n    base_collection: B\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="declared in two catalog files"):
            _load_catalog_data(tmp_path)

    def test_empty_catalog_errors(self, tmp_path: Path):
        """A catalog with no collections or recipes is rejected."""
        (tmp_path / "a.yaml").write_text("available_collections: [X]\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no 'collections:' or 'recipes:'"):
            _load_catalog_data(tmp_path)

    def test_invalid_collection_row_errors(self, tmp_path: Path):
        """An invalid collection row reports its key and file."""
        (tmp_path / "a.yaml").write_text(
            "collections:\n  k:\n    resolution: not-a-number\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="invalid collection 'k'"):
            _load_catalog_data(tmp_path)

    def test_invalid_recipe_row_errors(self, tmp_path: Path):
        """An invalid recipe row reports its key and file."""
        (tmp_path / "a.yaml").write_text(
            "recipes:\n  r:\n    graph: not-a-list\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="invalid recipe 'r'"):
            _load_catalog_data(tmp_path)

    def test_missing_path_errors(self, tmp_path: Path):
        """A non-existent catalog path is rejected."""
        with pytest.raises(ValueError, match="does not exist"):
            _load_catalog_data(tmp_path / "absent")

    def test_cache_returns_same_object(self, tmp_path: Path):
        """A second load of an unchanged path hits the parse cache."""
        path = tmp_path / "cat.yaml"
        path.write_text("collections:\n  k:\n    collection_id: K\n", encoding="utf-8")
        first = _load_catalog_data(path)
        second = _load_catalog_data(path)
        assert first is second

    def test_load_classmethod_uses_default_path(self):
        """`Catalog.load()` reads the bundled catalog directory."""
        assert Catalog.load().get_collection("sentinel-2-l2a").collection_id == (
            "SENTINEL2_L2A"
        )

    def test_in_memory_catalog_skips_disk(self):
        """Passing datasets= builds an in-memory catalog without disk reads."""
        from earthlens.openeo.catalog import Collection

        cat = Catalog(datasets={"k": Collection(collection_id="K")})
        assert set(cat.datasets) == {"k"}

    def test_resolved_graph_defaults(self):
        """A ResolvedGraph defaults to an empty, non-recipe graph."""
        g = ResolvedGraph(key="k", collection_id="K")
        assert g.graph == [] and g.is_recipe is False


@pytest.mark.openeo
def test_catalog_path_is_directory():
    """The bundled CATALOG_PATH points at the catalog directory."""
    assert catalog_mod.CATALOG_PATH.is_dir()
