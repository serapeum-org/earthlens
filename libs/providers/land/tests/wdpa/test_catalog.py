"""Unit tests for the WDPA country catalog and ISO3 resolution."""

from __future__ import annotations

import pytest

from earthlens.wdpa import Catalog, Country
from earthlens.wdpa import catalog as catalog_module


@pytest.mark.wdpa
class TestCatalogLoad:
    """The bundled catalog loads with the expected dict-like surface."""

    def test_known_codes_present(self):
        """Known ISO3 codes carry a country name."""
        cat = Catalog()
        assert "KEN" in cat
        assert cat["KEN"].name == "Kenya"

    def test_load_classmethod_and_cache(self):
        """`load` reads from disk and a second call hits the parse cache."""
        catalog_module.clear_catalog_cache()
        first = Catalog.load()
        second = Catalog.load()
        assert first["BRA"].name == second["BRA"].name == "Brazil"

    def test_supplied_datasets_skip_disk(self):
        """Passing `datasets=` skips the disk read."""
        cat = Catalog(datasets={"ZZZ": Country(name="Nowhere")})
        assert list(cat) == ["ZZZ"]

    def test_missing_block_raises(self, tmp_path, monkeypatch):
        """A YAML without a `countries:` block raises a clear ValueError."""
        empty = tmp_path / "empty.yaml"
        empty.write_text("other: {}\n", encoding="utf-8")
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", empty)
        catalog_module.clear_catalog_cache()
        with pytest.raises(ValueError, match="empty 'countries:' block"):
            Catalog()
        catalog_module.clear_catalog_cache()

    def test_invalid_row_raises(self, tmp_path, monkeypatch):
        """A country row missing its required name raises a validation ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("countries:\n  KEN:\n    region: Africa\n", encoding="utf-8")
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", bad)
        catalog_module.clear_catalog_cache()
        with pytest.raises(ValueError, match="failed validation"):
            Catalog()
        catalog_module.clear_catalog_cache()

    def test_missing_file_triggers_filenotfound_branch(self, tmp_path, monkeypatch):
        """A nonexistent catalog path exercises the `path.stat() FileNotFoundError` branch."""
        missing = tmp_path / "definitely-missing.yaml"
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", missing)
        catalog_module.clear_catalog_cache()
        with pytest.raises((ValueError, FileNotFoundError)):
            Catalog()
        catalog_module.clear_catalog_cache()


@pytest.mark.wdpa
class TestResolveIso3:
    """`resolve_iso3` accepts codes, names, and `country:` selectors."""

    def test_bare_code_passthrough(self):
        """A bare alpha-3 code passes through uppercased."""
        assert Catalog().resolve_iso3("ken") == "KEN"

    def test_uncatalogued_code_passthrough(self):
        """An alpha-3 code not in the catalog still passes through."""
        assert Catalog().resolve_iso3("ZWE") == "ZWE"

    def test_country_prefix(self):
        """A `country:` prefix is stripped before resolution."""
        assert Catalog().resolve_iso3("country:BRA") == "BRA"

    def test_friendly_name(self):
        """A country name resolves to its ISO3 code."""
        assert Catalog().resolve_iso3("Kenya") == "KEN"

    def test_unknown_name_did_you_mean(self):
        """An unknown country name raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'Kenya'"):
            Catalog().resolve_iso3("Kenyaa")


class TestAvailableDatasetsIndex:
    """Tests for the informational `available_datasets:` index."""

    def test_index_mirrors_curated_iso3_codes(self):
        """The available_datasets index covers the curated ISO3 country axis."""
        cat = Catalog()
        assert set(cat.available_datasets) == set(cat.datasets)
        assert len(cat.available_datasets) == 40


class TestModelPostInitSkipsLoadWhenProvided:
    """Tests the model_post_init inner false-branch — preset available_datasets is preserved when the YAML is loaded."""

    def test_preset_available_datasets_preserved_during_yaml_load(self):
        """With empty `datasets`, model_post_init loads the YAML but keeps preset available_datasets."""
        cat = Catalog(available_datasets=["preset:held"])
        assert cat.available_datasets == ["preset:held"], "preset survived YAML load"
        assert len(cat.datasets) > 0, "YAML still loaded for datasets"
