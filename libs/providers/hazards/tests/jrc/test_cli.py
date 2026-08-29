"""Unit tests for the JRC datasets-CLI catalog validator."""

from __future__ import annotations

import pytest

from earthlens.jrc import Catalog
from earthlens.jrc.cli import validator

pytestmark = pytest.mark.jrc


class _Row:
    """A minimal catalog row carrying only a `kind`."""

    def __init__(self, kind):
        self.kind = kind


class _FakeCatalog:
    """A catalog stub exposing just the `datasets` map the linter walks."""

    def __init__(self, datasets):
        self.datasets = datasets


class TestValidator:
    """Tests for the kind-aware JRC catalog validator."""

    def test_bundled_catalog_is_clean(self):
        """The shipped catalog lints clean across all four datasets."""
        checked, issues = validator(Catalog())
        assert checked == 4, f"expected 4 datasets checked, got {checked}"
        assert issues == [], f"bundled catalog should lint clean, got {issues}"

    def test_unknown_kind_is_reported(self):
        """A row whose kind has no required-field set is reported, not skipped."""
        checked, issues = validator(_FakeCatalog({"odd": _Row("not_a_kind")}))
        assert checked == 1, f"expected 1 dataset checked, got {checked}"
        assert len(issues) == 1, f"expected one issue, got {issues}"
        assert "unknown kind" in issues[0], (
            f"issue should name the unknown kind, got: {issues[0]}"
        )

    def test_missing_required_field_is_reported(self):
        """A row missing a field its kind requires is reported per field."""
        checked, issues = validator(_FakeCatalog({"bare": _Row("sea_level_gridded")}))
        assert checked == 1, f"expected 1 dataset checked, got {checked}"
        assert issues, "a row with no base_url/product/glob should raise issues"
        assert all("bare:" in issue for issue in issues), (
            f"issues should name the row, got: {issues}"
        )
