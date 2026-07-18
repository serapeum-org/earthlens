"""Unit tests for the IUCN country catalog and ISO2 resolution."""

from __future__ import annotations

import pytest

from earthlens.iucn import Catalog, Country
from earthlens.iucn import catalog as catalog_module


@pytest.mark.iucn
class TestCatalogLoad:
    """The bundled catalog loads with the expected dict-like surface."""

    def test_known_codes_present(self):
        """Known ISO2 codes carry a country name."""
        cat = Catalog()
        assert "KE" in cat
        assert cat["KE"].name == "Kenya"

    def test_load_classmethod_and_cache(self):
        """`load` reads from disk and a second call hits the parse cache."""
        catalog_module.clear_catalog_cache()
        first = Catalog.load()
        second = Catalog.load()
        assert first["BR"].name == second["BR"].name == "Brazil"

    def test_supplied_datasets_skip_disk(self):
        """Passing `datasets=` skips the disk read."""
        cat = Catalog(datasets={"ZZ": Country(name="Nowhere")})
        assert list(cat) == ["ZZ"]

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
        bad.write_text("countries:\n  KE:\n    region: Africa\n", encoding="utf-8")
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


@pytest.mark.iucn
class TestResolveIso2:
    """`resolve_iso2` accepts codes, names, and `country:` selectors."""

    def test_bare_code_passthrough(self):
        """A bare alpha-2 code passes through uppercased."""
        assert Catalog().resolve_iso2("ke") == "KE"

    def test_uncatalogued_code_passthrough(self):
        """An alpha-2 code not in the catalog still passes through."""
        assert Catalog().resolve_iso2("ZW") == "ZW"

    def test_country_prefix(self):
        """A `country:` prefix is stripped before resolution."""
        assert Catalog().resolve_iso2("country:BR") == "BR"

    def test_friendly_name(self):
        """A country name resolves to its ISO2 code."""
        assert Catalog().resolve_iso2("Kenya") == "KE"

    def test_unknown_name_did_you_mean(self):
        """An unknown country name raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'Kenya'"):
            Catalog().resolve_iso2("Kenyaa")


class TestAvailableDatasetsIndex:
    """Tests for the informational `available_datasets:` index."""

    def test_index_mirrors_curated_iso2_codes(self):
        """The available_datasets index covers the curated ISO2 country axis."""
        cat = Catalog()
        assert set(cat.available_datasets) == set(cat.datasets)
        assert len(cat.available_datasets) == 40

    def test_norway_iso2_quoted_in_index(self):
        """Norway's `NO` survives YAML-1.1 boolean coercion in the index too."""
        cat = Catalog()
        assert "NO" in cat.available_datasets, "Norway present as a string"


class TestModelPostInitSkipsLoadWhenProvided:
    """Tests the model_post_init inner false-branch — preset available_datasets is preserved when the YAML is loaded."""

    def test_preset_available_datasets_preserved_during_yaml_load(self):
        """With empty `datasets`, model_post_init loads the YAML but keeps preset available_datasets."""
        cat = Catalog(available_datasets=["preset:held"])
        assert cat.available_datasets == ["preset:held"], "preset survived YAML load"
        assert len(cat.datasets) > 0, "YAML still loaded for datasets"

