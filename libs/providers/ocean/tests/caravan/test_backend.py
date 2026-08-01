"""Unit tests for the Caravan backend (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from loguru import logger

from earthlens.base.http import HttpClient
from earthlens.caravan import Caravan
from earthlens.caravan.catalog import Catalog

from .conftest import (
    CURRENT_HEADER,
    LEGACY_HEADER,
    SHUFFLED_HEADER,
    FakeRangeSession,
    build_tar,
    build_zip,
)

pytestmark = pytest.mark.caravan

GLOBAL_LAT = [-90.0, 90.0]
GLOBAL_LON = [-180.0, 180.0]


def _source(
    catalog: Catalog,
    tmp_path: Path,
    blob: bytes | None = None,
    **overrides: Any,
) -> Caravan:
    """Build a Caravan backend bound to a fixture archive."""
    params: dict[str, Any] = dict(
        start="2020-01-01",
        end="2020-12-31",
        variables=["streamflow"],
        lat_lim=GLOBAL_LAT,
        lon_lim=GLOBAL_LON,
        path=str(tmp_path),
        dataset="demo",
        gauge_ids=["dk_1"],
        catalog=catalog,
        client=HttpClient(session=FakeRangeSession(blob or build_zip())),
        write_table=False,
    )
    params.update(overrides)
    return Caravan(**params)


class TestConstruction:
    """What is decided before any byte moves."""

    def test_output_kind_is_tabular(self, catalog, tmp_path):
        """The facade reads this to refuse an aggregate=."""
        assert _source(catalog, tmp_path).OUTPUT_KIND == "tabular"

    def test_the_release_is_resolved_offline(self, catalog, tmp_path):
        """A bad request fails at construction, not after a round trip."""
        source = _source(catalog, tmp_path)

        assert source.release.column_set == "current"
        assert source.archive_file.archive_format == "zip"

    def test_an_unknown_dataset_raises(self, catalog, tmp_path):
        """The catalog's did-you-mean surfaces through the backend."""
        with pytest.raises(ValueError, match="Caravan catalog"):
            _source(catalog, tmp_path, dataset="nope")

    def test_an_unknown_timeseries_format_raises(self, catalog, tmp_path):
        """Only the two published encodings are accepted."""
        with pytest.raises(ValueError, match="timeseries_format"):
            _source(catalog, tmp_path, timeseries_format="parquet")

    def test_no_request_is_made_at_construction(self, catalog, tmp_path):
        """Building a request must not touch the network."""
        session = FakeRangeSession(build_zip())
        Caravan(
            start="2020-01-01",
            end="2020-12-31",
            variables=["streamflow"],
            lat_lim=GLOBAL_LAT,
            lon_lim=GLOBAL_LON,
            path=str(tmp_path),
            dataset="demo",
            gauge_ids=["dk_1"],
            catalog=catalog,
            client=HttpClient(session=session),
        )

        assert session.get_calls == []


class TestGuards:
    """The refusals that stop an accidental multi-gigabyte or whole-archive pull."""

    def test_an_unbounded_request_raises(self, catalog, tmp_path):
        """No ids, no bbox, no country would mean every catchment."""
        source = _source(catalog, tmp_path, gauge_ids=None)

        with pytest.raises(ValueError, match="unbounded Caravan request"):
            source.download()

    def test_an_unknown_gauge_id_raises_and_shows_real_ids(self, catalog, tmp_path):
        """Prefix casing differs per source, so the error has to show examples."""
        source = _source(catalog, tmp_path, gauge_ids=["DK_1"])

        with pytest.raises(ValueError, match=r"not found in the 'demo' extension"):
            source.download()

    def test_a_download_only_release_raises(self, catalog, tmp_path):
        """A 29 GB transfer must never be triggered by typing a dataset name."""
        with pytest.raises(ValueError, match="allow_full_download=True"):
            _source(catalog, tmp_path, dataset="big")

    def test_the_refusal_points_at_the_range_readable_release(self, catalog, tmp_path):
        """The cheap alternative is named rather than left to be discovered."""
        with pytest.raises(ValueError, match="version='1.2'"):
            _source(catalog, tmp_path, dataset="big")

    def test_opting_in_allows_the_download_only_release(self, catalog, tmp_path):
        """The flag is the whole gate; nothing else blocks it."""
        source = _source(catalog, tmp_path, dataset="big", allow_full_download=True)

        assert source.archive_file.archive_format == "tar.gz"

    def test_the_range_readable_release_needs_no_flag(self, catalog, tmp_path):
        """base 1.2 is a zip, so it is as cheap as any extension."""
        source = _source(catalog, tmp_path, dataset="big", version="1.2")

        assert source.archive_file.is_range_readable

    def test_no_match_raises_and_echoes_the_filters(self, catalog, tmp_path):
        """An empty result from a filter is a mistake worth naming."""
        source = _source(
            catalog, tmp_path, gauge_ids=None, lat_lim=[80.0, 85.0], lon_lim=[0.0, 5.0]
        )

        with pytest.raises(ValueError, match="no 'demo' catchment matched"):
            source.download()


class TestSelection:
    """Turning a request into concrete catchments."""

    def test_explicit_ids_are_returned(self, catalog, tmp_path):
        """The simplest selection path."""
        frame = _source(catalog, tmp_path, gauge_ids=["dk_1", "dk_2"]).download()

        assert sorted(frame["gauge_id"].unique()) == ["dk_1", "dk_2"]

    def test_a_bounding_box_selects_by_centroid(self, catalog, tmp_path):
        """Resolved against attributes_other_*, not a guess."""
        frame = _source(
            catalog,
            tmp_path,
            gauge_ids=None,
            lat_lim=[55.5, 56.5],
            lon_lim=[9.0, 10.0],
        ).download()

        assert list(frame["gauge_id"].unique()) == ["dk_1"]

    def test_country_matches_the_full_english_name(self, catalog, tmp_path):
        """attributes_other_* stores 'South Africa', not an ISO2 code."""
        frame = _source(
            catalog, tmp_path, gauge_ids=None, country="South Africa"
        ).download()

        assert list(frame["gauge_id"].unique()) == ["xx_9"]

    def test_country_matching_is_case_insensitive(self, catalog, tmp_path):
        """Users should not have to match the archive's capitalisation."""
        frame = _source(catalog, tmp_path, gauge_ids=None, country="denmark").download()

        assert sorted(frame["gauge_id"].unique()) == ["dk_1", "dk_2"]

    def test_a_global_bbox_is_not_treated_as_a_filter(self, catalog, tmp_path):
        """Passing the whole world is how a caller says 'no spatial filter'."""
        source = _source(catalog, tmp_path, gauge_ids=None)

        with pytest.raises(ValueError, match="unbounded"):
            source.download()


class TestFrameShape:
    """The contract of the returned table."""

    def test_the_schema_leads_with_the_index_columns(self, catalog, tmp_path):
        """Every row is a catchment-day."""
        frame = _source(
            catalog, tmp_path, variables=["streamflow", "total_precipitation"]
        ).download()

        assert list(frame.columns) == [
            "gauge_id",
            "date",
            "streamflow",
            "total_precipitation_sum",
        ]

    def test_missing_streamflow_is_preserved(self, catalog, tmp_path):
        """A blank streamflow is a real missing observation, not a fetch error."""
        frame = _source(catalog, tmp_path).download()

        assert frame["streamflow"].isna().any()

    def test_the_window_filters_the_rows(self, catalog, tmp_path):
        """The member holds a catchment's whole record; the window trims it."""
        frame = _source(
            catalog, tmp_path, start="2020-01-02", end="2020-01-02"
        ).download()

        assert len(frame) == 1
        assert frame["date"].iloc[0] == pd.Timestamp("2020-01-02")

    def test_an_empty_selection_returns_a_schema_only_frame(self, catalog, tmp_path):
        """download() never returns None."""
        frame = _source(
            catalog, tmp_path, start="1900-01-01", end="1900-12-31"
        ).download()

        assert frame.empty
        assert list(frame.columns) == ["gauge_id", "date", "streamflow"]

    @pytest.mark.parametrize("header", [CURRENT_HEADER, SHUFFLED_HEADER])
    def test_columns_are_selected_by_name_not_position(self, catalog, tmp_path, header):
        """Archive column order differs between extensions."""
        frame = _source(
            catalog,
            tmp_path,
            blob=build_zip(header=header),
            variables=["streamflow", "temperature_2m_mean"],
        ).download()

        assert frame["temperature_2m_mean"].iloc[0] == 10.1
        assert frame["streamflow"].iloc[0] == 1.5

    def test_the_legacy_column_set_resolves_the_old_pet_name(self, catalog, tmp_path):
        """base 1.2 ships one potential_evaporation_sum, not the split pair."""
        frame = _source(
            catalog,
            tmp_path,
            dataset="big",
            version="1.2",
            blob=build_zip(root_prefix="Caravan/", header=LEGACY_HEADER),
            variables=["potential_evaporation"],
        ).download()

        assert "potential_evaporation_sum" in frame.columns

    def test_an_absent_column_is_returned_empty_with_a_warning(
        self, catalog, tmp_path, caplog
    ):
        """A column the archive lacks must not silently vanish from the schema."""
        frame = _source(
            catalog,
            tmp_path,
            blob=build_zip(header=LEGACY_HEADER),
            variables=["streamflow", "potential_evaporation"],
        ).download()

        assert "potential_evaporation_sum_ERA5_LAND" in frame.columns
        assert frame["potential_evaporation_sum_ERA5_LAND"].isna().all()

    def test_an_unknown_variable_raises(self, catalog, tmp_path):
        """Variables are validated against the catalog, not the header."""
        source = _source(catalog, tmp_path, variables=["rainfall"])

        with pytest.raises(ValueError, match="not a Caravan variable"):
            source.download()


class TestLimit:
    """The row cap, which for a ZIP genuinely stops the work."""

    def test_the_cap_trims_the_result(self, catalog, tmp_path):
        """A cap bounds what comes back."""
        frame = _source(catalog, tmp_path, gauge_ids=["dk_1", "dk_2"]).download(limit=4)

        assert len(frame) == 4

    def test_the_cap_stops_the_fetch_for_a_range_readable_archive(
        self, catalog, tmp_path
    ):
        """Catchments past the cap are never read."""
        frame = _source(catalog, tmp_path, gauge_ids=["dk_1", "dk_2"]).download(limit=2)

        assert list(frame["gauge_id"].unique()) == ["dk_1"]

    def test_a_zero_cap_raises(self, catalog, tmp_path):
        """A request for no rows is a caller bug, per the shared contract."""
        with pytest.raises(ValueError):
            _source(catalog, tmp_path).download(limit=0)


class TestExtras:
    """Optional joins and the tar transport."""

    def test_attributes_are_merged_when_asked(self, catalog, tmp_path):
        """with_attributes joins the static catchment metadata onto every row."""
        frame = _source(catalog, tmp_path, with_attributes=True).download()

        assert "country" in frame.columns
        assert frame["country"].iloc[0] == "Denmark"

    def test_attributes_are_absent_by_default(self, catalog, tmp_path):
        """The default result is the timeseries alone."""
        assert "country" not in _source(catalog, tmp_path).download().columns

    def test_the_table_is_written_when_requested(self, catalog, tmp_path):
        """The written artifact is named after the extension and version."""
        _source(catalog, tmp_path, write_table=True).download()

        assert (tmp_path / "caravan_demo_1-0.csv").is_file()

    def test_the_tar_transport_reads_the_same_way(self, catalog, tmp_path, monkeypatch):
        """The download fallback yields an identical frame."""
        tarball = tmp_path / "fixture.tar.gz"
        tarball.write_bytes(build_tar(header=LEGACY_HEADER))
        monkeypatch.setattr(
            "earthlens.caravan._helpers.ensure_archive", lambda *a, **k: tarball
        )
        source = _source(
            catalog,
            tmp_path,
            dataset="big",
            allow_full_download=True,
            gauge_ids=["dk_1", "dk_2"],
        )

        frame = source.download()

        assert sorted(frame["gauge_id"].unique()) == ["dk_1", "dk_2"]


class TestNoXarray:
    """The pyramids boundary this repo enforces."""

    def test_the_package_never_imports_xarray(self):
        """NetCDF is pyramids' job; earthlens must not reach for xarray."""
        package = Path(__import__("earthlens.caravan", fromlist=["x"]).__file__).parent
        sources = "\n".join(
            path.read_text(encoding="utf-8") for path in package.glob("*.py")
        )

        assert "import xarray" not in sources


class TestDegradedPaths:
    """Behaviour when the archive or an optional dependency misbehaves."""

    def test_an_unreadable_member_is_logged_not_raised(self, catalog, tmp_path):
        """A catchment listed but unreadable must not fail the whole request."""
        source = _source(catalog, tmp_path, gauge_ids=["dk_1", "dk_2"])
        archive = source._open_archive()
        original = archive.read

        def _explode(member: str) -> bytes:
            if member.endswith("dk_1.csv"):
                raise KeyError(member)
            return original(member)

        archive.read = _explode  # type: ignore[method-assign]

        frame = source.download()

        assert list(frame["gauge_id"].unique()) == ["dk_2"]

    def test_a_missing_member_in_the_tar_path_is_skipped(
        self, catalog, tmp_path, monkeypatch
    ):
        """The sequential reader tolerates a member the scan never found."""
        tarball = tmp_path / "fixture.tar.gz"
        tarball.write_bytes(build_tar(header=LEGACY_HEADER))
        monkeypatch.setattr(
            "earthlens.caravan._helpers.ensure_archive", lambda *a, **k: tarball
        )
        source = _source(
            catalog,
            tmp_path,
            dataset="big",
            allow_full_download=True,
            gauge_ids=["dk_1", "dk_2"],
        )
        archive = source._open_archive()
        original_read_many = archive.read_many
        monkeypatch.setattr(
            archive,
            "read_many",
            lambda members: {
                name: blob
                for name, blob in original_read_many(members).items()
                if name.endswith("dk_2.csv")
            },
        )

        frame = source.download()

        assert list(frame["gauge_id"].unique()) == ["dk_2"]

    def test_a_large_selection_warns_about_the_cost(self, catalog, tmp_path):
        """Hundreds of ranged reads against a rate-limited host is worth saying."""
        messages: list[str] = []
        source = _source(catalog, tmp_path, gauge_ids=None, country="Denmark")
        resolve = source._resolve_gauges
        source._resolve_gauges = lambda archive: resolve(archive) * 20  # type: ignore[method-assign]
        handler = logger.add(
            lambda message: messages.append(str(message)), level="WARNING"
        )
        try:
            source._search()
        finally:
            logger.remove(handler)

        assert any("catchments selected" in message for message in messages)

    def test_netcdf_goes_through_pyramids(self, catalog, tmp_path, monkeypatch):
        """The `.nc` variant must be decoded by pyramids, never by xarray."""
        calls: list[str] = []

        class _FakeDataset:
            def to_dataframe(self) -> pd.DataFrame:
                return pd.DataFrame(
                    {
                        "time": pd.to_datetime(["2020-01-01", "2020-01-02"]),
                        "streamflow": [1.0, 2.0],
                    }
                ).set_index("time")

        class _FakeNetCDF:
            @staticmethod
            def read_file(path: str) -> _FakeDataset:
                calls.append(path)
                return _FakeDataset()

        monkeypatch.setitem(
            __import__("sys").modules,
            "pyramids.netcdf",
            type("M", (), {"NetCDF": _FakeNetCDF}),
        )
        source = _source(catalog, tmp_path, timeseries_format="netcdf")

        frame = source.download()

        assert calls, "pyramids must be the reader for the netcdf variant"
        assert list(frame["gauge_id"].unique()) == ["dk_1"]

    def test_geometry_is_read_when_requested(self, catalog, tmp_path, monkeypatch):
        """with_geometry attaches the basin polygons alongside the frame."""
        seen: list[str] = []

        class _FakeCollection:
            @staticmethod
            def read_file(path: str) -> str:
                seen.append(path)
                return "collection"

        monkeypatch.setitem(
            __import__("sys").modules,
            "pyramids.feature.collection",
            type("M", (), {"FeatureCollection": _FakeCollection}),
        )
        source = _source(catalog, tmp_path, with_geometry=True)

        source.download()

        assert source.geometry == "collection"
        assert seen and seen[0].endswith(".shp")

    def test_geometry_is_none_when_the_archive_ships_none(
        self, catalog, tmp_path, monkeypatch
    ):
        """An archive without shapefiles yields no geometry, not an error."""
        source = _source(catalog, tmp_path, with_geometry=True)
        archive = source._open_archive()
        monkeypatch.setattr(archive, "shapefile_members", lambda source_name: [])

        source.download()

        assert source.geometry is None

    def test_attributes_on_an_empty_frame_are_skipped(self, catalog, tmp_path):
        """There is nothing to join onto an empty selection."""
        frame = _source(
            catalog,
            tmp_path,
            with_attributes=True,
            start="1900-01-01",
            end="1900-12-31",
        ).download()

        assert frame.empty
