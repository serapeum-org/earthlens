"""Unit tests for the MSWEP gauge-metadata CSVs."""

from __future__ import annotations

import pytest

from earthlens.mswep.backend import MSWEP
from earthlens.mswep.catalog import Catalog

pytestmark = [pytest.mark.mswep, pytest.mark.unit]

#: The five files named verbatim in the MSWEP V3.16 Documentation, section 7.
DOCUMENTED = {
    "daily_station_locations.csv",
    "monthly_station_locations.csv",
    "daily_station_date_ranges.csv",
    "monthly_station_date_ranges.csv",
    "daily_station_reporting_times.csv",
}


@pytest.fixture
def source(share, tmp_path):
    """An MSWEP backend bound to the fake share."""
    return MSWEP(
        start="2020-04-25",
        end="2020-04-25",
        temporal_resolution="daily",
        folder_id=share.path_id("MSWEP_V315"),
        service=share,
        path=tmp_path,
    )


class TestCatalogEntries:
    """The catalog carries the folder and every documented file."""

    def test_folder_name(self):
        """The folder is named as the documentation spells it."""
        assert Catalog().gauge_metadata.folder == "Gauge_metadata"

    def test_all_five_files_registered(self):
        """Every file named in section 7 is present, and nothing invented."""
        assert set(Catalog().gauge_metadata.files) == DOCUMENTED

    def test_each_file_describes_itself(self):
        """A row explains what the file holds, not just its name."""
        for row in Catalog().gauge_metadata.files.values():
            assert len(row.description) > 40

    def test_reporting_times_documents_the_offset_convention(self):
        """The sign convention is recorded, since it is easy to invert."""
        row = Catalog().gauge_metadata.files["daily_station_reporting_times.csv"]
        assert "19:00 UTC" in row.description

    def test_monthly_locations_documents_the_grid_index(self):
        """The gridcell identifier scheme is recorded."""
        row = Catalog().gauge_metadata.files["monthly_station_locations.csv"]
        assert "720x1440" in row.description

    def test_date_ranges_warns_about_gaps(self):
        """The rows say the span does not imply continuous coverage."""
        row = Catalog().gauge_metadata.files["daily_station_date_ranges.csv"]
        assert "gaps" in row.description


class TestFolderLocation:
    """The folder sits directly under the version root (= folder_id)."""

    def test_found_under_the_version_root(self, source, share):
        """It resolves as a direct child of the shared root, beside Past / NRT."""
        expected = share.path_id("MSWEP_V315/Gauge_metadata")
        assert source.gauge_metadata_folder() == expected

    def test_absent_folder_names_the_root(self, drive, tmp_path):
        """A version without gauge metadata (e.g. v2.80) yields a clear error."""
        drive.add_tree("SHARE", {"MSWEP_V280": {"Past": {"Daily": []}}})
        source = MSWEP(
            start="2020-04-25",
            end="2020-04-25",
            temporal_resolution="daily",
            version="2.80",
            folder_id=drive.path_id("MSWEP_V280"),
            service=drive,
            path=tmp_path,
        )
        with pytest.raises(FileNotFoundError, match="MSWEP_V280"):
            source.gauge_metadata_folder()


class TestFetch:
    """Downloading the CSVs."""

    def test_fetches_all_five_by_default(self, source, tmp_path):
        """No argument fetches every documented file."""
        paths = source.fetch_gauge_metadata()
        assert {p.name for p in paths} == DOCUMENTED
        assert all(p.exists() for p in paths)

    def test_output_mirrors_the_folder(self, source, tmp_path):
        """Files land under `<path>/Gauge_metadata/`, as the share holds them."""
        paths = source.fetch_gauge_metadata(["daily_station_locations.csv"])
        assert paths[0].relative_to(tmp_path).as_posix() == (
            "Gauge_metadata/daily_station_locations.csv"
        )

    def test_selecting_a_subset(self, source):
        """A named subset fetches only those files."""
        paths = source.fetch_gauge_metadata(["monthly_station_locations.csv"])
        assert [p.name for p in paths] == ["monthly_station_locations.csv"]

    def test_unknown_name_is_rejected(self, source):
        """A file the catalog does not list raises, naming the known ones."""
        with pytest.raises(ValueError, match="not gauge-metadata files"):
            source.fetch_gauge_metadata(["stations.csv"])

    def test_absent_file_is_skipped_not_raised(self, drive, tmp_path):
        """A share missing one CSV yields the rest rather than failing."""
        drive.add_tree(
            "SHARE",
            {
                "MSWEP_V315": {
                    "Past": {"Daily": []},
                    "Gauge_metadata": ["daily_station_locations.csv"],
                }
            },
        )
        source = MSWEP(
            start="2020-04-25",
            end="2020-04-25",
            temporal_resolution="daily",
            folder_id=drive.path_id("MSWEP_V315"),
            service=drive,
            path=tmp_path,
        )
        paths = source.fetch_gauge_metadata()
        assert [p.name for p in paths] == ["daily_station_locations.csv"]

    def test_absent_file_is_logged(self, drive, tmp_path, loguru_messages):
        """The skip is reported, never silent."""
        drive.add_tree(
            "SHARE",
            {
                "MSWEP_V315": {
                    "Past": {"Daily": []},
                    "Gauge_metadata": ["daily_station_locations.csv"],
                }
            },
        )
        MSWEP(
            start="2020-04-25",
            end="2020-04-25",
            temporal_resolution="daily",
            folder_id=drive.path_id("MSWEP_V315"),
            service=drive,
            path=tmp_path,
        ).fetch_gauge_metadata()
        assert "absent from the share" in "".join(loguru_messages)

    def test_names_resolve_in_one_query(self, source, share):
        """All five resolve in a single chunked name query, not a listing."""
        before = len(share.list_calls)
        source.fetch_gauge_metadata()
        name_queries = [
            c
            for c in share.list_calls[before:]
            if "daily_station_locations.csv" in c["q"]
        ]
        assert len(name_queries) == 1

    def test_mswx_is_rejected(self, share, tmp_path):
        """Gauge metadata is MSWEP's; asking MSWX for it is an error."""
        source = MSWEP(
            start="2007-05-13",
            end="2007-05-13",
            product="mswx",
            variables=["Temp"],
            temporal_resolution="daily",
            folder_id=share.path_id("MSWX_V100"),
            service=share,
            path=tmp_path,
        )
        with pytest.raises(ValueError, match="published under MSWEP"):
            source.fetch_gauge_metadata()

    def test_does_not_disturb_the_raster_contract(self, source):
        """Fetching CSVs leaves `OUTPUT_KIND` alone."""
        source.fetch_gauge_metadata(["daily_station_locations.csv"])
        assert source.OUTPUT_KIND == "raster"
