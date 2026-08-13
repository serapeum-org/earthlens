"""Tests for the FLODIS catalog-tooling validator (`earthlens.flodis.cli`).

Moved out of core's CLI test suite when the FLODIS validator moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.flodis.cli import validator

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the FLODIS structural lint."""

    def test_flags_missing_row_fields_and_top_level_blocks(self):
        """A catalog missing per-table fields / record / columns flags each."""
        catalog = SimpleNamespace(
            datasets={
                "damages": SimpleNamespace(file="", description="", key_columns=())
            },
            record=None,
            columns={},
        )
        checked, issues = validator(catalog)
        joined = " ".join(issues)
        assert checked == 1
        assert "damages: missing description" in joined
        assert "record: missing pinned Zenodo record id" in joined
        assert "columns: missing required key 'disasterno'" in joined
        assert "columns: missing required key 'gid_1'" in joined
