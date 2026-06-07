"""Unit tests for `earthlens.cli.query`."""

from __future__ import annotations

import pytest

from earthlens.cli.query import (
    DEFAULT_PROVIDER_PRIORITY,
    PRIORITY_ENV_VAR,
    apply_filters,
    exact_first,
    facet_counts,
    free_text,
    is_exact,
    match_rows,
    parse_filters,
    provider_priority,
    provider_rank,
    sort_rows,
)
from earthlens.cli.table import CatalogRow

pytestmark = pytest.mark.cli


def _row(provider, dataset_id, title="", cadence="", resolution="", lic=""):
    """Build a CatalogRow with sensible defaults for tests."""
    return CatalogRow(provider, dataset_id, title, cadence, resolution, lic)


class TestProviderPriority:
    """Tests for provider_priority."""

    def test_default_when_unset(self, monkeypatch):
        """Without the env override, the default precedence is returned."""
        monkeypatch.delenv(PRIORITY_ENV_VAR, raising=False)
        assert provider_priority() == list(DEFAULT_PROVIDER_PRIORITY)

    def test_env_override(self, monkeypatch):
        """The env var (comma-separated) overrides the default order."""
        monkeypatch.setenv(PRIORITY_ENV_VAR, "chc, gee ,s3")
        assert provider_priority() == ["chc", "gee", "s3"], "parsed + trimmed"


class TestProviderRank:
    """Tests for provider_rank."""

    def test_listed_provider_uses_index(self):
        """A listed provider ranks by its position."""
        assert provider_rank("gee", ["ecmwf", "gee", "chc"]) == 1

    def test_unlisted_sorts_last(self):
        """An unlisted provider ranks after every listed one."""
        order = ["ecmwf", "chc"]
        assert provider_rank("worldpop", order) > provider_rank("chc", order)


class TestSortRows:
    """Tests for sort_rows."""

    def test_priority_then_provider_then_id(self):
        """Rows order by precedence, then provider id, then dataset id."""
        rows = [
            _row("chc", "z"),
            _row("ecmwf", "b"),
            _row("ecmwf", "a"),
        ]
        ordered = sort_rows(rows, priority=["ecmwf", "chc"])
        assert [(r.provider, r.dataset_id) for r in ordered] == [
            ("ecmwf", "a"),
            ("ecmwf", "b"),
            ("chc", "z"),
        ]

    def test_does_not_mutate_input(self):
        """Sorting returns a new list; the input order is preserved."""
        rows = [_row("chc", "a"), _row("ecmwf", "b")]
        original = list(rows)
        sort_rows(rows)
        assert rows == original, "input list left untouched"


class TestIsExact:
    """Tests for is_exact."""

    def test_case_insensitive_equality(self):
        """An id match is case-insensitive and whitespace-trimmed."""
        assert is_exact(_row("s3", "ERA5"), " era5 ") is True

    def test_non_match(self):
        """A partial id is not an exact match."""
        assert is_exact(_row("ecmwf", "reanalysis-era5"), "era5") is False


class TestMatchRows:
    """Tests for match_rows."""

    def test_breadth_first_default(self):
        """The default match spans id and title substrings."""
        rows = [
            _row("s3", "era5", "ERA5"),
            _row("ecmwf", "x", "ERA5-Land"),
            _row("c", "y", "z"),
        ]
        assert [r.provider for r in match_rows(rows, "era5")] == ["s3", "ecmwf"]

    def test_exact_only(self):
        """exact=True keeps only literal id matches."""
        rows = [_row("s3", "era5"), _row("ecmwf", "reanalysis-era5")]
        assert [r.provider for r in match_rows(rows, "era5", exact=True)] == ["s3"]

    def test_no_match_returns_empty(self):
        """A query that matches nothing yields an empty list."""
        assert match_rows([_row("s3", "era5")], "nope") == []


class TestExactFirst:
    """Tests for exact_first."""

    def test_exact_floated_to_top(self):
        """Exact id matches lead, partials follow, each precedence-ordered."""
        rows = [
            _row("ecmwf", "reanalysis-era5-land"),
            _row("s3", "era5"),
            _row("gee", "ERA5/something"),
        ]
        ordered = exact_first(rows, "era5", priority=["ecmwf", "s3", "gee"])
        assert ordered[0].dataset_id == "era5", "exact id wins the top slot"


class TestFreeText:
    """Tests for free_text."""

    def test_blank_matches_all(self):
        """A blank query returns every row."""
        rows = [_row("a", "x"), _row("b", "y")]
        assert len(free_text(rows, "   ")) == 2

    def test_matches_search_blob(self):
        """The query matches across provider / id / title."""
        rows = [_row("chc", "chirps", "Rainfall"), _row("s3", "era5", "ERA5")]
        assert [r.provider for r in free_text(rows, "rain")] == ["chc"]


class TestParseFilters:
    """Tests for parse_filters."""

    def test_parses_known_facets(self):
        """`facet=value` tokens parse into a mapping (last wins)."""
        assert parse_filters(["provider=chc", "cadence=daily"]) == {
            "provider": "chc",
            "cadence": "daily",
        }

    def test_missing_equals_raises(self):
        """A token with no `=` is rejected."""
        with pytest.raises(ValueError, match="facet=value"):
            parse_filters(["providerchc"])

    def test_unknown_facet_raises(self):
        """An unknown facet name is rejected with the valid choices."""
        with pytest.raises(ValueError, match="unknown filter facet"):
            parse_filters(["colour=blue"])


class TestApplyFilters:
    """Tests for apply_filters."""

    def test_and_semantics(self):
        """Every filter must match (logical AND), case-insensitively."""
        rows = [
            _row("chc", "a", cadence="daily"),
            _row("chc", "b", cadence="monthly"),
        ]
        kept = apply_filters(rows, {"provider": "CHC", "cadence": "daily"})
        assert [r.dataset_id for r in kept] == ["a"]

    def test_empty_filters_keep_all(self):
        """No filters keep every row."""
        rows = [_row("chc", "a"), _row("gee", "b")]
        assert len(apply_filters(rows, {})) == 2


class TestFacetCounts:
    """Tests for facet_counts."""

    def test_counts_sorted_desc(self):
        """Counts group by value, sorted by descending count then value."""
        rows = [_row("chc", "a"), _row("chc", "b"), _row("gee", "c")]
        assert facet_counts(rows, "provider") == [("chc", 2), ("gee", 1)]

    def test_empty_values_skipped(self):
        """Rows with an empty facet value are not counted."""
        rows = [_row("chc", "a", cadence="daily"), _row("chc", "b")]
        assert facet_counts(rows, "cadence") == [("daily", 1)]
