"""Unit tests for the GBIF taxon catalog and key resolution."""

from __future__ import annotations

import pytest

from earthlens.gbif import Catalog


@pytest.mark.gbif
class TestCatalogLoad:
    """The bundled catalog loads with the expected dict-like surface."""

    def test_friendly_keys_present(self):
        """Known friendly taxa resolve to their backbone keys."""
        cat = Catalog()
        assert "birds" in cat
        assert cat["birds"].taxon_key == 212
        assert cat["mammals"].taxon_key == 359


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
