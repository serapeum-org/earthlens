"""Unit tests for the OBIS species catalog and name resolution."""

from __future__ import annotations

import pytest

from earthlens.obis import Catalog, Species
from earthlens.obis import catalog as catalog_module


@pytest.mark.obis
class TestCatalogLoad:
    """The bundled catalog loads with the expected dict-like surface."""

    def test_friendly_keys_present(self):
        """Known friendly species resolve to their scientific names."""
        cat = Catalog()
        assert "blue-whale" in cat
        assert cat["blue-whale"].scientific_name == "Balaenoptera musculus"

    def test_load_classmethod_and_cache(self):
        """`load` reads from disk and a second call hits the parse cache."""
        catalog_module.clear_catalog_cache()
        first = Catalog.load()
        second = Catalog.load()
        assert first["blue-whale"].scientific_name == second["blue-whale"].scientific_name

    def test_supplied_datasets_skip_disk(self):
        """Passing `datasets=` skips the disk read."""
        cat = Catalog(datasets={"x": Species(scientific_name="Mola mola")})
        assert list(cat) == ["x"]

    def test_missing_block_raises(self, tmp_path, monkeypatch):
        """A YAML without a `species:` block raises a clear ValueError."""
        empty = tmp_path / "empty.yaml"
        empty.write_text("other: {}\n", encoding="utf-8")
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", empty)
        catalog_module.clear_catalog_cache()
        with pytest.raises(ValueError, match="empty 'species:' block"):
            Catalog()
        catalog_module.clear_catalog_cache()

    def test_invalid_row_raises(self, tmp_path, monkeypatch):
        """A species row missing its name raises a validation ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("species:\n  blue-whale:\n    title: no name\n", encoding="utf-8")
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


@pytest.mark.obis
class TestResolveScientificName:
    """`resolve_scientific_name` accepts friendly keys and explicit names."""

    def test_friendly_key(self):
        """A friendly key resolves via the catalog."""
        assert Catalog().resolve_scientific_name("common-dolphin") == "Delphinus delphis"

    def test_species_prefix_passthrough(self):
        """A `species:<name>` selector is passed through verbatim."""
        assert Catalog().resolve_scientific_name("species:Mola mola") == "Mola mola"

    def test_unknown_friendly_key_did_you_mean(self):
        """An unknown friendly key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'blue-whale'"):
            Catalog().resolve_scientific_name("blue-whal")
