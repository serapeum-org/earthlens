"""Tests for the IUCN catalog-tooling handlers (`earthlens.iucn.cli`).

Moved out of core's CLI test suite when the IUCN handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.iucn.cli as iucn_cli
from earthlens.iucn import Catalog

pytestmark = pytest.mark.cli


class TestProber:
    """Tests for the offline IUCN country prober."""

    def test_returns_country_row(self):
        """The IUCN probe returns the curated name + region."""
        result = iucn_cli.prober(Catalog(), "KE")
        assert result == {"KE": {"name": "Kenya", "region": "Africa"}}

    def test_raises_on_unknown_country(self):
        """An unknown country code raises with a clear message."""
        with pytest.raises(ValueError, match="unknown IUCN country"):
            iucn_cli.prober(Catalog(), "XX")


class TestRefresherAndValidator:
    """The curated-universe refresher and the ISO2 validator."""

    def test_iso2_codes_are_universe(self):
        """iucn refresher returns the curated ISO2 country codes (including 'NO')."""
        grouped = iucn_cli.refresher(Catalog())
        assert "KE" in grouped["iucn"] and "NO" in grouped["iucn"], "Norway preserved"

    def test_validator_clean_on_bundled_catalog(self):
        """The shipped IUCN catalog lints clean."""
        checked, issues = iucn_cli.validator(Catalog())
        assert checked > 0 and issues == [], f"clean catalog: {issues}"


class TestEmitter:
    """The offline countries-row emitter."""

    def test_seeds_country_row(self):
        """An ISO2 code + name/region seed a `countries:` row."""
        row = iucn_cli.emitter(Catalog(), "KE", key="KE", name="Kenya", region="Africa")
        assert row == {"name": "Kenya", "region": "Africa"}
