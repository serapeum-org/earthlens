"""Tests for the GBIF catalog-tooling handlers (`earthlens.gbif.cli`).

Moved out of core's CLI test suite when the GBIF handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.gbif.cli as gbif_cli
from earthlens.gbif import Catalog

pytestmark = pytest.mark.cli


class TestProber:
    """Tests for the offline GBIF taxon prober."""

    def test_returns_dispatch_row(self):
        """The light probe returns the catalog's recorded taxon_key + rank."""
        result = gbif_cli.prober(Catalog(), "birds")
        assert result == {
            "birds": {"taxon_key": 212, "title": "Aves — birds", "rank": "class"}
        }

    def test_raises_on_unknown_taxon(self):
        """An unknown taxon raises with a clear message."""
        with pytest.raises(ValueError, match="unknown GBIF taxon"):
            gbif_cli.prober(Catalog(), "nope")


class TestRefresherAndValidator:
    """The curated-universe refresher and the taxon-key validator."""

    def test_includes_friendly_aliases_and_index(self):
        """gbif's refresh axis is the union of friendly aliases + available index."""
        grouped = gbif_cli.refresher(Catalog())
        ids = set(grouped["gbif"])
        assert {"birds", "mammals", "kingdom:Animalia"} <= ids, "union returned"

    def test_validator_clean_on_bundled_catalog(self):
        """The shipped GBIF catalog lints clean."""
        checked, issues = gbif_cli.validator(Catalog())
        assert checked > 0, f"clean catalog: {issues}"
        assert issues == [], f"clean catalog: {issues}"


class TestEmitter:
    """The offline taxa-row emitter."""

    def test_seeds_taxa_row(self):
        """A backbone taxonKey + title/rank seed a `taxa:` row."""
        row = gbif_cli.emitter(
            Catalog(), "212", key="birds", title="Birds", rank="class"
        )
        assert row == {"taxon_key": 212, "title": "Birds", "rank": "class"}
