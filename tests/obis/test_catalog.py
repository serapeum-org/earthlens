"""Unit tests for the OBIS species catalog and name resolution."""

from __future__ import annotations

import pytest

from earthlens.obis import Catalog


@pytest.mark.obis
class TestCatalogLoad:
    """The bundled catalog loads with the expected dict-like surface."""

    def test_friendly_keys_present(self):
        """Known friendly species resolve to their scientific names."""
        cat = Catalog()
        assert "blue-whale" in cat
        assert cat["blue-whale"].scientific_name == "Balaenoptera musculus"


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
