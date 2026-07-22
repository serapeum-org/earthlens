"""Unit tests for the GBIF taxon catalog and key resolution."""

from __future__ import annotations

import pytest

from earthlens.gbif import Catalog, Taxon
from earthlens.gbif import catalog as catalog_module


@pytest.mark.gbif
class TestCatalogLoad:
    """The bundled catalog loads with the expected dict-like surface."""

    def test_friendly_keys_present(self):
        """Known friendly taxa resolve to their backbone keys."""
        cat = Catalog()
        assert "birds" in cat
        assert cat["birds"].taxon_key == 212
        assert cat["mammals"].taxon_key == 359

    def test_load_classmethod_and_cache(self):
        """`load` reads from disk and a second call hits the parse cache."""
        catalog_module.clear_catalog_cache()
        first = Catalog.load()
        second = Catalog.load()
        assert first["birds"].taxon_key == second["birds"].taxon_key == 212

    def test_supplied_datasets_skip_disk(self):
        """Passing `datasets=` skips the disk read."""
        cat = Catalog(datasets={"x": Taxon(taxon_key=1)})
        assert list(cat) == ["x"]

    def test_missing_block_raises(self, tmp_path, monkeypatch):
        """A YAML without a `taxa:` block raises a clear ValueError."""
        empty = tmp_path / "empty.yaml"
        empty.write_text("other: {}\n", encoding="utf-8")
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", empty)
        catalog_module.clear_catalog_cache()
        with pytest.raises(ValueError, match="empty 'taxa:' block"):
            Catalog()
        catalog_module.clear_catalog_cache()

    def test_invalid_row_raises(self, tmp_path, monkeypatch):
        """A taxon row missing its required key raises a validation ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("taxa:\n  birds:\n    title: no key\n", encoding="utf-8")
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


@pytest.mark.gbif
class TestResolveTaxonKey:
    """`resolve_taxon_key` accepts friendly keys, raw ints, and name lookups."""

    def test_friendly_key(self):
        """A friendly key resolves via the catalog."""
        assert Catalog().resolve_taxon_key("birds") == 212

    def test_raw_int_passthrough(self):
        """A raw integer key passes straight through."""
        assert Catalog().resolve_taxon_key(212) == 212

    def test_digit_string(self):
        """A digit string is parsed to its integer key."""
        assert Catalog().resolve_taxon_key("359") == 359

    def test_taxon_name_lookup(self, fake_gbif):
        """A `taxon:<name>` selector resolves live via name_backbone (usage.key)."""
        assert Catalog().resolve_taxon_key("taxon:Panthera leo") == 5219404
        # The name must reach name_backbone's `scientificName` (passed positionally),
        # not vanish into **kwargs as a stray `name=` (which real pygbif rejects).
        assert fake_gbif.species.calls == ["Panthera leo"]

    def test_taxon_name_lookup_legacy_shape(self, fake_gbif):
        """The legacy flat `usageKey` shape is read when `usage` is absent."""
        fake_gbif.species.result = {"usageKey": 999}
        assert Catalog().resolve_taxon_key("taxon:Whatever") == 999

    def test_taxon_name_no_match_raises(self, fake_gbif):
        """A name with no backbone match raises a clear ValueError."""
        fake_gbif.species.result = {}
        with pytest.raises(ValueError, match="no backbone taxon"):
            Catalog().resolve_taxon_key("taxon:Nonexistent")

    def test_unknown_friendly_key_did_you_mean(self):
        """An unknown friendly key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'birds'"):
            Catalog().resolve_taxon_key("bird")


class TestAvailableDatasetsIndex:
    """Tests for the informational `available_datasets:` index."""

    def test_index_loads_from_yaml(self):
        """The bundled catalog ships a non-empty available_datasets index."""
        cat = Catalog()
        assert len(cat.available_datasets) >= 30, "richer GBIF taxonomic index"

    def test_index_carries_kingdom_keys(self):
        """The index includes the eight GBIF backbone kingdoms."""
        cat = Catalog()
        for kingdom in ("Animalia", "Plantae", "Fungi", "Bacteria"):
            assert f"kingdom:{kingdom}" in cat.available_datasets, kingdom

    def test_index_carries_class_keys(self):
        """The index includes vertebrate animal classes."""
        cat = Catalog()
        for cls in ("Aves", "Mammalia", "Reptilia", "Amphibia"):
            assert f"class:{cls}" in cat.available_datasets, cls


class TestModelPostInitSkipsLoadWhenProvided:
    """Tests the model_post_init inner false-branch — preset available_datasets is preserved when the YAML is loaded."""

    def test_preset_available_datasets_preserved_during_yaml_load(self):
        """With empty `datasets`, model_post_init loads the YAML but keeps preset available_datasets."""
        cat = Catalog(available_datasets=["preset:held"])
        assert cat.available_datasets == ["preset:held"], "preset survived YAML load"
        assert len(cat.datasets) > 0, "YAML still loaded for datasets"
