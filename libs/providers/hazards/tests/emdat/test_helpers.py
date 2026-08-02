"""Tests for the EM-DAT stateless helpers."""

from __future__ import annotations

import warnings
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from earthlens.emdat import Catalog, _helpers

from .conftest import FakeHttp


@pytest.fixture
def events_row():
    """The shipped `emdat:events` catalog row."""
    return Catalog().get("emdat:events")


@pytest.fixture
def points_row():
    """The shipped `gdis:points` catalog row."""
    return Catalog().get("gdis:points")


@pytest.fixture
def polygons_row():
    """The shipped `gdis:polygons` catalog row."""
    return Catalog().get("gdis:polygons")


@pytest.mark.emdat
class TestDataverseResolution:
    """Resolving the archive file out of a Dataverse version listing."""

    def test_listing_requests_the_latest_version(
        self, dataverse_listing: dict[str, Any]
    ) -> None:
        """The listing call targets the `:latest` version endpoint."""
        http = FakeHttp(dataverse_listing)
        files = _helpers.dataverse_file_listing(
            http, "https://example.invalid", "doi:10.0000/TEST"
        )
        assert len(files) == 3
        assert ":latest" in http.calls[0][1]

    def test_trailing_slash_does_not_double_up(
        self, dataverse_listing: dict[str, Any]
    ) -> None:
        """A base URL with a trailing slash still builds one clean path."""
        http = FakeHttp(dataverse_listing)
        _helpers.dataverse_file_listing(
            http, "https://example.invalid/", "doi:10.0000/TEST"
        )
        assert "//api" not in http.calls[0][1]

    def test_missing_data_key_yields_no_files(self) -> None:
        """A payload without `data` is an empty listing, not a crash."""
        assert _helpers.dataverse_file_listing(FakeHttp({}), "https://x", "doi") == []

    def test_picks_the_archive(
        self, dataverse_listing: dict[str, Any], events_row
    ) -> None:
        """The pattern selects the workbook, not its siblings."""
        file_id, name = _helpers.pick_dataverse_file(
            dataverse_listing["data"], events_row
        )
        assert (file_id, name) == (1, "990101_emdat_archive.xlsx")

    def test_no_match_names_what_was_present(self, events_row) -> None:
        """An unmatched pattern reports the files that were there."""
        listing = [{"dataFile": {"id": 9, "filename": "readme.txt"}}]
        with pytest.raises(ValueError, match="readme.txt"):
            _helpers.pick_dataverse_file(listing, events_row)

    def test_ambiguous_match_rejected(self, events_row) -> None:
        """Two matching files is a catalog bug, not a silent first-wins."""
        listing = [
            {"dataFile": {"id": 1, "filename": "a_emdat_archive.xlsx"}},
            {"dataFile": {"id": 2, "filename": "b_emdat_archive.xlsx"}},
        ]
        with pytest.raises(ValueError, match="matched 2 files"):
            _helpers.pick_dataverse_file(listing, events_row)

    def test_download_url_shape(self) -> None:
        """The access URL points at the numeric datafile id."""
        url = _helpers.dataverse_download_url("https://example.invalid/", 42)
        assert url == "https://example.invalid/api/access/datafile/42"


@pytest.mark.emdat
class TestExtractMember:
    """Pulling one named file out of a granule archive."""

    def test_extracts_the_named_member(
        self, gdis_csv_zip: Path, tmp_path: Path
    ) -> None:
        """The requested member lands in the destination directory."""
        dest = tmp_path / "out"
        dest.mkdir()
        got = _helpers.extract_member(
            gdis_csv_zip, "pend-gdis-1960-2018-disasterlocations.csv", dest
        )
        assert got.is_file()
        assert got.parent == dest

    def test_existing_member_is_not_rewritten(
        self, gdis_csv_zip: Path, tmp_path: Path
    ) -> None:
        """A second call reuses the extracted file instead of unpacking again."""
        dest = tmp_path / "out"
        dest.mkdir()
        member = "pend-gdis-1960-2018-disasterlocations.csv"
        first = _helpers.extract_member(gdis_csv_zip, member, dest)
        first.write_text("sentinel", encoding="utf-8")
        second = _helpers.extract_member(gdis_csv_zip, member, dest)
        assert second.read_text(encoding="utf-8") == "sentinel"

    def test_unknown_member_lists_the_archive(
        self, gdis_csv_zip: Path, tmp_path: Path
    ) -> None:
        """A missing member names what the archive actually holds."""
        with pytest.raises(ValueError, match="codebook"):
            _helpers.extract_member(gdis_csv_zip, "absent.csv", tmp_path)

    def test_nested_member_leaves_no_empty_directory(self, tmp_path: Path) -> None:
        """Flattening a nested member cleans up the directory it came from."""
        archive = tmp_path / "nested2.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("inner/data.csv", "a,b\n1,2\n")
        dest = tmp_path / "out2"
        dest.mkdir()
        _helpers.extract_member(archive, "inner/data.csv", dest)
        assert not (dest / "inner").exists()

    def test_non_empty_source_directory_is_left_alone(self, tmp_path: Path) -> None:
        """A directory still holding something else survives the flattening."""
        archive = tmp_path / "nested3.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("inner/data.csv", "a,b\n1,2\n")
        dest = tmp_path / "out3"
        (dest / "inner").mkdir(parents=True)
        (dest / "inner" / "keep.txt").write_text("mine", encoding="utf-8")

        got = _helpers.extract_member(archive, "inner/data.csv", dest)

        assert got == dest / "data.csv"
        assert (dest / "inner" / "keep.txt").read_text(encoding="utf-8") == "mine"

    def test_nested_member_is_flattened(self, tmp_path: Path) -> None:
        """A member stored under a directory still lands directly in dest."""
        archive = tmp_path / "nested.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("inner/data.csv", "a,b\n1,2\n")
        dest = tmp_path / "out"
        dest.mkdir()
        got = _helpers.extract_member(archive, "inner/data.csv", dest)
        assert got == dest / "data.csv"
        assert got.is_file()


@pytest.mark.emdat
class TestHazardFilterSql:
    """The attribute filter absorbs the shipped trailing-space spelling."""

    def test_matches_bare_and_padded(self) -> None:
        """Each hazard is matched both bare and space-suffixed."""
        sql = _helpers.hazard_filter_sql("disastertype", ["flood"])
        assert sql == "disastertype LIKE 'flood' OR disastertype LIKE 'flood '"

    def test_uses_like_so_the_match_ignores_case(self) -> None:
        """LIKE, not `=` — a re-issued file may capitalise the value differently."""
        sql = _helpers.hazard_filter_sql("disastertype", ["flood"])
        assert " = " not in sql
        assert "LIKE" in sql

    def test_wildcards_are_escaped(self) -> None:
        """A `%` or `_` in a hazard name is escaped, not left as a wildcard."""
        sql = _helpers.hazard_filter_sql("t", ["a%b_c"])
        assert r"a\%b\_c" in sql

    def test_multiple_hazards(self) -> None:
        """Every requested hazard contributes both spellings."""
        sql = _helpers.hazard_filter_sql("disastertype", ["flood", "storm"])
        assert sql.count("LIKE") == 4

    def test_quotes_are_escaped(self) -> None:
        """A quote in a hazard name is doubled, not left to break the SQL."""
        sql = _helpers.hazard_filter_sql("t", ["o'brien"])
        assert "o''brien" in sql


@pytest.mark.emdat
class TestCountryAndCombinedFilters:
    """The ISO3 attribute filter and the clause combiner."""

    def test_country_is_upper_cased(self) -> None:
        """A lower-case ISO3 is normalised to the stored spelling."""
        assert _helpers.country_filter_sql("iso3", " tst ") == "iso3 LIKE 'TST'"

    def test_country_quotes_are_escaped(self) -> None:
        """A quote in the code is doubled rather than breaking the SQL."""
        assert "O''B" in _helpers.country_filter_sql("iso3", "o'b")

    def test_combine_joins_with_and(self) -> None:
        """Several fragments are parenthesised and ANDed together."""
        assert _helpers.combine_filters("a = 1", "b = 2") == "(a = 1) AND (b = 2)"

    def test_combine_drops_empty_clauses(self) -> None:
        """A `None` fragment contributes nothing."""
        assert _helpers.combine_filters(None, "b = 2") == "(b = 2)"

    def test_combine_of_nothing_is_none(self) -> None:
        """No requested filters means no `WHERE` clause at all."""
        assert _helpers.combine_filters(None, None) is None


@pytest.mark.emdat
class TestEventYears:
    """Recovering an event year from whichever column the row provides."""

    def test_reads_a_year_column(self, gdis_csv_frame, points_row) -> None:
        """A distribution with a year column is read directly."""
        years = _helpers.event_years(gdis_csv_frame, points_row)
        assert years.tolist()[:3] == [2009, 1995, 2009]

    def test_derives_year_from_id_prefix(self, polygons_row) -> None:
        """Without a year column the 4-digit id prefix is used."""
        frame = pd.DataFrame({"disasterno": ["2009-0631", "1995-0100"]})
        assert _helpers.event_years(frame, polygons_row).tolist() == [2009, 1995]

    def test_unparseable_year_becomes_na(self, polygons_row) -> None:
        """A malformed id yields a missing year rather than raising."""
        frame = pd.DataFrame({"disasterno": ["notayear"]})
        assert _helpers.event_years(frame, polygons_row).isna().all()

    def test_missing_source_column_yields_na(self, points_row) -> None:
        """A frame lacking the year column yields an all-missing series."""
        frame = pd.DataFrame({"other": [1, 2]})
        result = _helpers.event_years(frame, points_row)
        assert len(result) == 2
        assert result.isna().all()


@pytest.mark.emdat
class TestFilterFrame:
    """The shared hazard / country / year / bbox filter."""

    def test_no_filters_keeps_everything(self, gdis_csv_frame, points_row) -> None:
        """An unfiltered request returns every row."""
        out = _helpers.filter_frame(gdis_csv_frame, points_row)
        assert len(out) == len(gdis_csv_frame)

    def test_hazard_filter(self, gdis_csv_frame, points_row) -> None:
        """Only the requested disaster type survives."""
        out = _helpers.filter_frame(gdis_csv_frame, points_row, hazards=["flood"])
        assert set(out["disastertype"]) == {"flood"}

    def test_hazard_filter_is_case_insensitive(
        self, gdis_csv_frame, points_row
    ) -> None:
        """Stored values are compared stripped and lower-cased."""
        frame = gdis_csv_frame.copy()
        frame.loc[0, "disastertype"] = " FLOOD "
        out = _helpers.filter_frame(frame, points_row, hazards=["flood"])
        assert len(out) == 3

    def test_country_filter_is_case_insensitive(
        self, gdis_csv_frame, points_row
    ) -> None:
        """An ISO3 code matches regardless of the caller's casing."""
        out = _helpers.filter_frame(gdis_csv_frame, points_row, country="tst")
        assert set(out["iso3"]) == {"TST"}

    def test_year_window_both_bounds(self, gdis_csv_frame, points_row) -> None:
        """An inclusive window keeps only the years inside it."""
        out = _helpers.filter_frame(gdis_csv_frame, points_row, year_range=(2009, 2009))
        assert set(out["year"]) == {2009}

    def test_year_window_open_start(self, gdis_csv_frame, points_row) -> None:
        """A `None` lower bound means "from the beginning"."""
        out = _helpers.filter_frame(gdis_csv_frame, points_row, year_range=(None, 2000))
        assert set(out["year"]) == {1995}

    def test_year_window_open_end(self, gdis_csv_frame, points_row) -> None:
        """A `None` upper bound means "to the end"."""
        out = _helpers.filter_frame(gdis_csv_frame, points_row, year_range=(2010, None))
        assert set(out["year"]) == {2011}

    def test_bbox_filter(self, gdis_csv_frame, points_row) -> None:
        """Only rows whose coordinates fall inside the box survive."""
        out = _helpers.filter_frame(
            gdis_csv_frame, points_row, bbox=(20.0, 10.0, 21.0, 11.0)
        )
        assert set(out["id"]) == {1, 4}

    def test_bbox_drops_rows_without_coordinates(
        self, gdis_csv_frame, points_row
    ) -> None:
        """A row with no coordinates cannot satisfy a bbox."""
        out = _helpers.filter_frame(
            gdis_csv_frame, points_row, bbox=(-180.0, -90.0, 180.0, 90.0)
        )
        assert 5 not in set(out["id"])

    def test_bbox_warns_about_ungeocoded_rows(self, gdis_csv_frame, points_row) -> None:
        """A bbox that discards uncoordinated rows says so rather than hiding it."""
        with pytest.warns(_helpers.UngeocodedRowsWarning, match="cannot satisfy"):
            _helpers.filter_frame(
                gdis_csv_frame, points_row, bbox=(-180.0, -90.0, 180.0, 90.0)
            )

    def test_the_warning_counts_only_matching_rows(
        self, gdis_csv_frame, points_row
    ) -> None:
        """The count reflects the rows the other filters kept, not the table."""
        with pytest.warns(_helpers.UngeocodedRowsWarning) as caught:
            _helpers.filter_frame(
                gdis_csv_frame,
                points_row,
                hazards=["flood"],
                bbox=(-180.0, -90.0, 180.0, 90.0),
            )
        assert "1 of 3 matching row(s)" in str(caught[0].message)

    def test_bbox_does_not_warn_when_every_row_is_located(
        self, gdis_csv_frame, points_row
    ) -> None:
        """A fully-geocoded table triggers no warning."""
        located = gdis_csv_frame.dropna(subset=["latitude", "longitude"])
        with warnings.catch_warnings():
            warnings.simplefilter("error", _helpers.UngeocodedRowsWarning)
            _helpers.filter_frame(
                located, points_row, bbox=(-180.0, -90.0, 180.0, 90.0)
            )

    def test_filters_compose(self, gdis_csv_frame, points_row) -> None:
        """Several filters narrow the result together."""
        out = _helpers.filter_frame(
            gdis_csv_frame,
            points_row,
            hazards=["flood"],
            country="TST",
            year_range=(2009, 2009),
        )
        assert out["disasterno"].tolist() == ["2009-0001"]

    def test_index_is_reset(self, gdis_csv_frame, points_row) -> None:
        """The surviving rows are renumbered from zero."""
        out = _helpers.filter_frame(gdis_csv_frame, points_row, country="OTH")
        assert out.index.tolist() == [0]

    def test_missing_column_skips_that_filter(self, points_row) -> None:
        """A filter whose column is absent is skipped, not an error."""
        frame = pd.DataFrame({"unrelated": [1, 2]})
        out = _helpers.filter_frame(frame, points_row, hazards=["flood"], country="TST")
        assert len(out) == 2

    def test_events_schema_filters_too(self, events_frame, events_row) -> None:
        """The same routine handles the archive's differently-named columns."""
        out = _helpers.filter_frame(
            events_frame, events_row, hazards=["flood"], country="TST"
        )
        assert set(out["Disaster Type"]) == {"Flood"}


@pytest.mark.emdat
class TestPointsToFeatureCollection:
    """Building point features from a table's coordinate columns."""

    def test_builds_points_in_wgs84(self, gdis_csv_frame, points_row) -> None:
        """The result is a point collection in EPSG:4326."""
        collection = _helpers.points_to_feature_collection(gdis_csv_frame, points_row)
        assert collection.crs == "EPSG:4326"
        assert set(collection.geometry.geom_type) == {"Point"}

    def test_rows_without_coordinates_are_dropped(
        self, gdis_csv_frame, points_row
    ) -> None:
        """A row that cannot be placed is not emitted as a feature."""
        collection = _helpers.points_to_feature_collection(gdis_csv_frame, points_row)
        assert len(collection) == len(gdis_csv_frame) - 1

    def test_attributes_survive(self, gdis_csv_frame, points_row) -> None:
        """Every non-coordinate attribute rides along."""
        collection = _helpers.points_to_feature_collection(gdis_csv_frame, points_row)
        assert {"disasterno", "country", "adm1"} <= set(collection.columns)

    def test_missing_coordinate_columns_is_an_error(self, points_row) -> None:
        """A table without the named coordinates cannot become points."""
        with pytest.raises(ValueError, match="cannot build point features"):
            _helpers.points_to_feature_collection(
                pd.DataFrame({"other": [1]}), points_row
            )

    def test_empty_input_yields_empty_collection(
        self, gdis_csv_frame, points_row
    ) -> None:
        """An empty table produces an empty collection, not an error."""
        collection = _helpers.points_to_feature_collection(
            gdis_csv_frame.iloc[0:0], points_row
        )
        assert len(collection) == 0
