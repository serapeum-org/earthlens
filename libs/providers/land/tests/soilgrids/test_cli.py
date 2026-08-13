"""Tests for the SoilGrids catalog-tooling validator (`earthlens.soilgrids.cli`).

Moved out of core's CLI test suite when the validator moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.soilgrids.cli import validator

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the soilgrids structural lint."""

    def test_flags_non_isric_endpoint_and_missing_mean(self):
        """A row with a non-ISRIC endpoint and no mean quantile is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "clay": SimpleNamespace(
                    endpoint="https://example.com/wcs",
                    depths=["0-5cm"],
                    quantiles=["Q0.5"],
                )
            }
        )
        checked, issues = validator(catalog)
        assert checked == 1
        assert any("endpoint host is not" in i for i in issues)
        assert any("mean" in i for i in issues)

    def test_flags_spoofed_isric_host(self):
        """A look-alike host (maps.isric.org.evil.com) is rejected, not accepted."""
        catalog = SimpleNamespace(
            datasets={
                "clay": SimpleNamespace(
                    endpoint="https://maps.isric.org.evil.com/wcs",
                    depths=["0-5cm"],
                    quantiles=["mean"],
                )
            }
        )
        _checked, issues = validator(catalog)
        assert any("endpoint host is not" in i for i in issues)

    def test_flags_missing_endpoint_and_depths(self):
        """A row missing its endpoint and depths is flagged for each."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(endpoint="", depths=[], quantiles=["mean"])
            }
        )
        _checked, issues = validator(catalog)
        assert any("missing endpoint" in i for i in issues)
        assert any("missing depths" in i for i in issues)
