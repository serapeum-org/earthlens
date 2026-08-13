"""Tests for the NREL catalog-tooling handlers (`earthlens.nrel.cli`).

Moved out of core's CLI test suite when the NREL handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import earthlens.nrel.cli as nrel_cli

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the nrel structural lint."""

    def test_good_rows_pass(self):
        """A row with source, endpoint, and columns reports no issues."""
        catalog = SimpleNamespace(
            datasets={
                "nsrdb-psm3": SimpleNamespace(
                    source="nsrdb", endpoint="/api/x.csv", columns=["time", "GHI"]
                )
            }
        )
        checked, issues = nrel_cli.validator(catalog)
        assert checked == 1
        assert issues == []

    def test_flags_missing_source_and_columns(self):
        """A row missing its source and columns is flagged for each."""
        catalog = SimpleNamespace(
            datasets={"bad": SimpleNamespace(source="", endpoint="/x.csv", columns=[])}
        )
        _checked, issues = nrel_cli.validator(catalog)
        assert any("missing source" in i for i in issues)
        assert any("missing columns" in i for i in issues)
