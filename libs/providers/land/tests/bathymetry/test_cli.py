"""Tests for the bathymetry catalog-tooling validator (`earthlens.bathymetry.cli`).

Moved out of core's CLI test suite when the validator moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.bathymetry.cli import validator

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the bathymetry structural lint."""

    def test_flags_missing_endpoint_and_band(self):
        """A row missing its endpoint and band is flagged for each."""
        catalog = SimpleNamespace(
            available_datasets=["bad"],
            datasets={"bad": SimpleNamespace(endpoint="", dataset_id="X", variable="")},
        )
        checked, issues = validator(catalog)
        assert checked == 1
        assert any("missing endpoint" in i for i in issues)
        assert any("missing variable" in i for i in issues)

    def test_flags_id_absent_from_index(self):
        """A curated id missing from the available_datasets index is flagged."""
        catalog = SimpleNamespace(
            available_datasets=["other"],
            datasets={
                "row": SimpleNamespace(
                    endpoint="https://x/erddap", dataset_id="X", variable="z"
                )
            },
        )
        _checked, issues = validator(catalog)
        assert any("available_datasets" in i for i in issues)
