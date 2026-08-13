"""Tests for the WDPA catalog-tooling handlers (`earthlens.wdpa.cli`).

Moved out of core's CLI test suite when the WDPA handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.wdpa.cli as wdpa_cli
from earthlens.wdpa import Catalog

pytestmark = pytest.mark.cli


class TestProber:
    """Tests for the offline WDPA country prober."""

    def test_returns_country_row(self):
        """The WDPA probe returns the curated name + region."""
        result = wdpa_cli.prober(Catalog(), "KEN")
        assert result == {"KEN": {"name": "Kenya", "region": "Africa"}}

    def test_raises_on_unknown_country(self):
        """An unknown country code raises with a clear message."""
        with pytest.raises(ValueError, match="unknown WDPA country"):
            wdpa_cli.prober(Catalog(), "XYZ")


class TestRefresherAndValidator:
    """The curated-universe refresher and the ISO3 validator."""

    def test_iso3_codes_are_universe(self):
        """wdpa refresher returns the curated ISO3 country codes."""
        grouped = wdpa_cli.refresher(Catalog())
        assert "KEN" in grouped["wdpa"] and "USA" in grouped["wdpa"]

    def test_validator_clean_on_bundled_catalog(self):
        """The shipped WDPA catalog lints clean."""
        checked, issues = wdpa_cli.validator(Catalog())
        assert checked > 0 and issues == [], f"clean catalog: {issues}"


class TestEmitter:
    """The offline countries-row emitter."""

    def test_seeds_country_row(self):
        """An ISO3 code + name/region seed a `countries:` row."""
        row = wdpa_cli.emitter(
            Catalog(), "KEN", key="KEN", name="Kenya", region="Africa"
        )
        assert row == {"name": "Kenya", "region": "Africa"}
