"""Tests for the HANZE catalog-tooling validator (`earthlens.hanze.cli`).

Moved out of core's CLI test suite when the HANZE validator moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.hanze.cli import validator

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the HANZE structural lint."""

    def test_flags_missing_top_level_blocks(self):
        """A catalog missing record / geometry / files / columns flags each."""
        catalog = SimpleNamespace(
            datasets={"River": SimpleNamespace(description="")},
            record=None,
            geometry=None,
            files={},
            columns={},
        )
        checked, issues = validator(catalog)
        joined = " ".join(issues)
        assert checked == 1
        assert "River: missing description" in joined
        assert "record: missing pinned Zenodo record id" in joined
        assert "geometry: missing shapefile member_stem" in joined
        assert "files: missing required file 'events'" in joined
        assert "columns: missing required key 'regions_nuts3'" in joined
