"""Unit tests for `earthlens.cli.table`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.cli.table import (
    FACET_NAMES,
    CatalogRow,
    CatalogTable,
    build_table,
    clear_table_cache,
    _facet_token,
    _first_token,
    _format_number,
    _to_row,
)

pytestmark = pytest.mark.cli


class TestFormatNumber:
    """Tests for _format_number."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (30.0, "30"),
            (0.05, "0.05"),
            (1113.2, "1113.2"),
            (5, "5"),
        ],
    )
    def test_renders_without_trailing_zero(self, value, expected):
        """Integer-valued floats drop the `.0`; real fractions are kept.

        Args:
            value: The numeric input.
            expected: Its rendered token.
        """
        assert _format_number(value) == expected, f"{value} -> {expected}"


class TestFacetToken:
    """Tests for _facet_token."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, ""),
            (True, ""),
            ("  daily ", "daily"),
            (0.05, "0.05"),
            (30.0, "30"),
            (SimpleNamespace(interval=1, unit="day"), "day"),
            (SimpleNamespace(interval=16, unit="day"), "16 day"),
            (SimpleNamespace(interval=None, unit="month"), "month"),
            ([0.05], "0.05"),
            (["daily", "daily", "monthly"], "daily, monthly"),
            (SimpleNamespace(foo="bar"), ""),
        ],
    )
    def test_token_extraction(self, value, expected):
        """Each record-attribute shape reduces to a clean token (or '').

        Args:
            value: A raw attribute value of one of the supported shapes.
            expected: The token it should collapse to.
        """
        assert _facet_token(value) == expected, f"{value!r} -> {expected!r}"

    def test_list_dedupes_preserving_order(self):
        """A list of values is de-duplicated while preserving first-seen order."""
        assert _facet_token(["b", "a", "b"]) == "b, a", "dedupe keeps order"


class TestFirstToken:
    """Tests for _first_token."""

    def test_returns_first_non_empty(self):
        """The first attribute yielding a token wins."""
        record = SimpleNamespace(cadence="", temporal_resolution="daily")
        assert _first_token(record, ("cadence", "temporal_resolution")) == "daily"

    def test_empty_when_none_present(self):
        """No matching attribute yields the empty string."""
        record = SimpleNamespace(other="x")
        assert _first_token(record, ("cadence", "frequency")) == ""


class TestCatalogRow:
    """Tests for CatalogRow."""

    def test_search_text_is_lowercased_blob(self):
        """search_text joins provider/id/title and lower-cases them."""
        row = CatalogRow("ECMWF", "ERA5-Single", "Hourly ERA5", "", "", "")
        assert row.search_text == "ecmwf era5-single hourly era5", "lowercased blob"

    @pytest.mark.parametrize(
        "facet, expected",
        [
            ("provider", "ecmwf"),
            ("cadence", "1 day"),
            ("resolution", "0.25"),
            ("license", ""),
            ("instrument", ""),
        ],
    )
    def test_facet_lookup(self, facet, expected):
        """facet() returns the named column, '' for absent/unknown facets.

        Args:
            facet: Facet name requested.
            expected: Value expected for the fixture row.
        """
        row = CatalogRow("ecmwf", "era5", "ERA5", "1 day", "0.25", "")
        assert row.facet(facet) == expected, f"facet({facet!r}) -> {expected!r}"

    def test_record_excluded_from_equality(self):
        """Two rows with equal columns are equal regardless of `record`."""
        a = CatalogRow("s3", "era5", "ERA5", "monthly", "", "", record=object())
        b = CatalogRow("s3", "era5", "ERA5", "monthly", "", "", record=object())
        assert a == b, "record participates in neither eq nor hash"

    def test_curated_defaults_true(self):
        """A row is curated unless explicitly marked otherwise."""
        assert CatalogRow("s3", "era5", "ERA5", "", "", "").curated is True
        assert CatalogRow("s3", "x", "", "", "", "", curated=False).curated is False


class TestCatalogTable:
    """Tests for CatalogTable."""

    @pytest.fixture
    def table(self):
        """A two-row table spanning two providers.

        Returns:
            A small hand-built :class:`CatalogTable`.
        """
        rows = (
            CatalogRow("chc", "chirps-daily", "", "daily", "0.05", ""),
            CatalogRow("gee", "ECMWF/ERA5/DAILY", "ERA5", "1 day", "", ""),
        )
        return CatalogTable(rows=rows, errors=(), providers=("chc", "gee"))

    def test_facet_values_sorted_distinct(self, table):
        """facet_values returns sorted, de-duplicated, non-empty values."""
        assert table.facet_values("provider") == ["chc", "gee"], "providers"
        assert table.facet_values("cadence") == ["1 day", "daily"], "cadences"

    def test_facet_values_drops_empty(self, table):
        """An all-empty facet returns no values."""
        assert table.facet_values("license") == [], "no licenses present"

    def test_present_facets_skips_empty(self, table):
        """present_facets lists only facets that carry at least one value."""
        assert table.present_facets() == ["provider", "cadence", "resolution"]

    def test_facet_names_constant(self):
        """The advertised facet columns are stable."""
        assert FACET_NAMES == ("provider", "cadence", "resolution", "license")


class TestToRow:
    """Tests for _to_row."""

    def test_extracts_facets_from_record(self):
        """_to_row pulls title and facet tokens off a raw adapter row."""
        record = SimpleNamespace(
            title="ERA5",
            cadence="monthly",
            spatial_resolution=0.25,
            license="proprietary",
        )
        raw = SimpleNamespace(
            provider="s3", dataset_id="era5", title="ERA5", record=record
        )
        row = _to_row(raw)
        assert row.provider == "s3", "provider carried over"
        assert row.cadence == "monthly", "cadence extracted"
        assert row.resolution == "0.25", "spatial_resolution extracted"
        assert row.license == "proprietary", "license extracted"


class TestBuildTable:
    """Tests for build_table and the process-lifetime cache."""

    def test_provider_scoped_build(self):
        """A scoped build loads only the requested backend."""
        table = build_table(providers=["chc"])
        assert table.providers == ("chc",), "only chc scanned"
        assert all(r.provider == "chc" for r in table.rows), "rows all chc"
        assert table.rows, "chc yields rows"

    def test_result_is_cached(self):
        """A second call with the same selection returns the cached object."""
        first = build_table(providers=["chc"])
        second = build_table(providers=["chc"])
        assert first is second, "same selection -> cached table"

    def test_refresh_rebuilds(self):
        """refresh=True rebuilds even when a cached table exists."""
        first = build_table(providers=["chc"])
        rebuilt = build_table(providers=["chc"], refresh=True)
        assert rebuilt is not first, "refresh bypasses the cache"
        assert rebuilt.rows, "rebuilt table still populated"

    def test_selection_keys_are_independent(self):
        """Different provider selections are cached separately."""
        chc = build_table(providers=["chc"])
        radar = build_table(providers=["radar"])
        assert chc is not radar, "distinct selections -> distinct tables"
        assert radar.providers == ("radar",), "radar selection scoped"

    def test_rows_are_curated_by_default(self):
        """A plain build yields only curated rows."""
        table = build_table(providers=["overture"])
        assert all(row.curated for row in table.rows), "curated rows only"

    def test_include_available_adds_uncurated_rows(self):
        """--include-available folds in extra id-only (curated=False) rows."""
        curated = build_table(providers=["overture"])
        widened = build_table(providers=["overture"], include_available=True)
        assert len(widened.rows) > len(curated.rows), "available index adds rows"
        extra = [row for row in widened.rows if not row.curated]
        assert extra, "the extra rows are flagged curated=False"
        assert all(row.title == "" for row in extra), "available rows are id-only"

    def test_include_available_is_cached_separately(self):
        """The include_available flag is part of the cache key."""
        plain = build_table(providers=["overture"])
        widened = build_table(providers=["overture"], include_available=True)
        assert plain is not widened, "distinct flag -> distinct cached table"


class TestClearTableCache:
    """Tests for clear_table_cache."""

    def test_clear_forces_rebuild(self):
        """After clearing, the next build returns a fresh object."""
        first = build_table(providers=["chc"])
        clear_table_cache()
        second = build_table(providers=["chc"])
        assert first is not second, "cache cleared -> rebuilt"
