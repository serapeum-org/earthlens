"""Tests for the OBIS catalog-tooling handlers (`earthlens.obis.cli`).

Moved out of core's CLI test suite when the OBIS refresh/probe/validate/emit
handlers moved into this distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.obis.cli as obis_cli
from earthlens.obis import Catalog

pytestmark = pytest.mark.cli


class TestProber:
    """Tests for the offline OBIS species prober."""

    def test_returns_dispatch_row(self):
        """The OBIS probe returns the curated scientific_name + title."""
        result = obis_cli.prober(Catalog(), "blue-whale")
        assert result["blue-whale"]["scientific_name"] == "Balaenoptera musculus"

    def test_raises_on_unknown_species(self):
        """An unknown species raises with a clear message."""
        with pytest.raises(ValueError, match="unknown OBIS species"):
            obis_cli.prober(Catalog(), "nope")


class TestRefresherAndValidator:
    """The curated-universe refresher and the scientific-name validator."""

    def test_refresher_unions_index_and_aliases(self):
        """The refresh axis is the union of available index + friendly aliases."""
        grouped = obis_cli.refresher(Catalog())
        assert grouped["obis"], "curated universe is non-empty"

    def test_validator_clean_on_bundled_catalog(self):
        """The shipped OBIS catalog lints clean."""
        checked, issues = obis_cli.validator(Catalog())
        assert checked > 0, f"clean catalog: {issues}"
        assert issues == [], f"clean catalog: {issues}"


class TestEmitter:
    """The offline species-row emitter."""

    def test_seeds_species_row(self):
        """A scientific name + title seed a `species:` row."""
        row = obis_cli.emitter(
            Catalog(), "Mola mola", key="ocean-sunfish", title="Ocean sunfish"
        )
        assert row == {"scientific_name": "Mola mola", "title": "Ocean sunfish"}
