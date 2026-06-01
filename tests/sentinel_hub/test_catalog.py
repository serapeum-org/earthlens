"""Unit tests for `earthlens.sentinel_hub.catalog` (collections + evalscripts)."""

from __future__ import annotations

import pytest

from earthlens.sentinel_hub import (
    EVALSCRIPTS_PATH,
    Catalog,
    EvalscriptRecipe,
    read_evalscript,
)
from earthlens.sentinel_hub.catalog import clear_catalog_cache

pytestmark = pytest.mark.sentinel_hub


@pytest.fixture
def catalog() -> Catalog:
    """A freshly loaded bundled catalog."""
    clear_catalog_cache()
    return Catalog()


class TestResolution:
    """Collection vs evalscript-recipe resolution."""

    def test_collection_resolves(self, catalog):
        """A collection key resolves to its DataCollection binding."""
        assert catalog.get_collection("sentinel-2-l2a").sh_collection == "SENTINEL2_L2A"

    def test_is_recipe(self, catalog):
        """A recipe key is recognised as a recipe."""
        assert catalog.is_recipe("sentinel-2-l2a-ndvi") is True
        assert catalog.is_recipe("sentinel-2-l2a") is False

    def test_recipe_resolves_to_evalscript(self, catalog):
        """A render recipe resolves to its bundled `.js` + kind."""
        r = catalog.resolve("sentinel-2-l2a-ndvi")
        assert (r.sh_collection, r.evalscript, r.kind) == (
            "SENTINEL2_L2A",
            "ndvi.js",
            "render",
        )
        assert r.is_recipe is True

    def test_collection_resolves_without_evalscript(self, catalog):
        """A plain collection resolves with no evalscript (needs explicit one)."""
        r = catalog.resolve("sentinel-2-l2a")
        assert r.evalscript is None
        assert r.is_recipe is False
        assert r.bands == ["B04", "B03", "B02"]

    def test_unknown_key_did_you_mean(self, catalog):
        """An unknown key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean"):
            catalog.resolve("sentinel-2-l2a-ndv")


class TestEvalscripts:
    """Every referenced `.js` exists and is a V3 evalscript."""

    def test_every_recipe_js_exists_and_is_v3(self, catalog):
        """Each recipe's evalscript exists and starts with the V3 marker."""
        for recipe in catalog.recipes.values():
            text = read_evalscript(recipe.evalscript)
            assert text.splitlines()[0].strip() == "//VERSION=3"

    def test_stats_recipes_declare_datamask(self, catalog):
        """Every `kind="stats"` recipe's evalscript declares a dataMask band."""
        stats = [r for r in catalog.recipes.values() if r.kind == "stats"]
        assert stats
        for recipe in stats:
            assert "dataMask" in read_evalscript(recipe.evalscript)

    def test_render_recipes_have_no_datamask_requirement(self, catalog):
        """Render recipes do not need a dataMask band."""
        renders = [r for r in catalog.recipes.values() if r.kind == "render"]
        assert renders

    def test_read_missing_evalscript_raises(self):
        """Reading an unknown evalscript raises a clear FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="not found"):
            read_evalscript("does_not_exist.js")

    def test_evalscripts_path_points_at_js_dir(self):
        """The evalscripts directory holds the bundled `.js` files."""
        assert any(EVALSCRIPTS_PATH.glob("*.js"))


class TestModels:
    """Model validation."""

    def test_recipe_rejects_unknown_kind(self):
        """An invalid recipe kind is rejected."""
        with pytest.raises(ValueError, match="recipe kind"):
            EvalscriptRecipe(base_collection="X", evalscript="x.js", kind="weird")

    def test_recipe_rejects_extra_field(self):
        """Unknown recipe fields are forbidden."""
        with pytest.raises(ValueError):
            EvalscriptRecipe(base_collection="X", evalscript="x.js", bogus=1)
