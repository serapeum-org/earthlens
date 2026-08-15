"""Tests for the drought catalog-tooling handlers (`earthlens.drought.cli`).

Moved out of core's CLI test suite when the drought handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import earthlens.drought.cli as drought_cli

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the drought structural lint."""

    def test_clean_rows_pass(self):
        """A well-formed edo-wcs raster row and a usdm vector row report nothing."""
        catalog = SimpleNamespace(
            datasets={
                "edo-spaST": SimpleNamespace(
                    source="EDO",
                    endpoint="https://x/wcs",
                    output_kind="raster",
                    cadence="10day",
                    native_crs="EPSG:4326",
                    transport="edo-wcs",
                    coverage="spaST",
                    timescale="01",
                ),
                "usdm": SimpleNamespace(
                    source="USDM",
                    endpoint="https://x/{ymd}.json",
                    output_kind="vector",
                    cadence="weekly",
                    native_crs="EPSG:4326",
                    transport="usdm-geojson",
                    coverage=None,
                    timescale=None,
                ),
            }
        )
        checked, issues = drought_cli.validator(catalog)
        assert checked == 2
        assert issues == []

    def test_flags_edo_wcs_row_missing_coverage_and_timescale(self):
        """An edo-wcs row without a coverage or timescale is flagged for each."""
        catalog = SimpleNamespace(
            datasets={
                "edo-bad": SimpleNamespace(
                    source="EDO",
                    endpoint="https://x/wcs",
                    output_kind="raster",
                    cadence="10day",
                    native_crs="EPSG:4326",
                    transport="edo-wcs",
                    coverage=None,
                    timescale=None,
                )
            }
        )
        checked, issues = drought_cli.validator(catalog)
        assert checked == 1
        assert any("missing coverage" in i for i in issues)
        assert any("missing timescale" in i for i in issues)

    def test_flags_transport_output_kind_mismatch(self):
        """A usdm-geojson row declared raster (or edo-wcs declared vector) is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "usdm": SimpleNamespace(
                    source="USDM",
                    endpoint="https://x",
                    output_kind="raster",
                    cadence="weekly",
                    native_crs="EPSG:4326",
                    transport="usdm-geojson",
                    coverage=None,
                    timescale=None,
                )
            }
        )
        _checked, issues = drought_cli.validator(catalog)
        assert any("must be output_kind=vector" in i for i in issues)
