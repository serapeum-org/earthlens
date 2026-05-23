"""Tests for the TropicalCyclone backend (fake tropycal SDK, no network)."""

from __future__ import annotations

import sys

import geopandas as gpd
import pandas as pd
import pytest

from earthlens.tropycal import TropicalCyclone
from earthlens.tropycal.events import POINT_COLUMNS, TRACK_COLUMNS

pytestmark = pytest.mark.tropycal


def _backend(tmp_path, **overrides):
    """Build a TropicalCyclone over the Gulf bbox / 2005 window with overrides."""
    kwargs = dict(
        start="2005-08-01",
        end="2005-09-01",
        variables=["north_atlantic"],
        lat_lim=[18, 31],
        lon_lim=[-98, -80],
        source="hurdat",
        path=str(tmp_path),
    )
    kwargs.update(overrides)
    return TropicalCyclone(**kwargs)


class TestInit:
    """Tests for TropicalCyclone construction and validation."""

    def test_output_kind_is_vector(self):
        """The backend declares OUTPUT_KIND == 'vector'."""
        assert TropicalCyclone.OUTPUT_KIND == "vector"

    def test_empty_variables_defaults_to_north_atlantic(self, tmp_path):
        """An empty variables list defaults to ['north_atlantic']."""
        backend = _backend(tmp_path, variables=[])
        assert backend.vars == ["north_atlantic"]

    def test_dict_variables_rejected(self, tmp_path):
        """A mapping variables value is a TypeError (basins are a list)."""
        with pytest.raises(TypeError, match="basin codes"):
            _backend(tmp_path, variables={"north_atlantic": ["vmax"]})

    def test_bad_source_rejected(self, tmp_path):
        """An unknown source is rejected (no jtwc in tropycal 1.4)."""
        with pytest.raises(ValueError, match="source must be one of"):
            _backend(tmp_path, source="jtwc")

    def test_bad_geometry_rejected(self, tmp_path):
        """An unknown geometry mode is rejected."""
        with pytest.raises(ValueError, match="geometry must be"):
            _backend(tmp_path, geometry="blob")

    def test_bad_file_format_rejected(self, tmp_path):
        """An unknown file_format is rejected."""
        with pytest.raises(ValueError, match="file_format must be"):
            _backend(tmp_path, file_format="csv")

    def test_extents_populated(self, tmp_path):
        """Construction populates the spatial and temporal extents."""
        backend = _backend(tmp_path)
        assert backend.space.south == 18.0
        assert backend.time.start_date.year == 2005
        assert backend.time.resolution == "all"


class TestSearch:
    """Tests for _search (per-basin product + (basin, source) validation)."""

    def test_one_product_per_basin(self, tmp_path):
        """_search emits one product per requested basin carrying the source."""
        backend = _backend(tmp_path, variables=["north_atlantic", "east_pacific"])
        products = backend._search()
        assert [p.id for p in products] == ["north_atlantic", "east_pacific"]
        assert all(p.metadata["source"] == "hurdat" for p in products)

    def test_invalid_basin_source_pair_raises(self, tmp_path):
        """hurdat does not serve west_pacific, so _search raises ValueError."""
        backend = _backend(tmp_path, variables=["west_pacific"], source="hurdat")
        with pytest.raises(ValueError, match="does not serve basin"):
            backend._search()

    def test_unknown_basin_raises(self, tmp_path):
        """An unknown basin code raises via the catalog did-you-mean."""
        backend = _backend(tmp_path, variables=["atlantis"])
        with pytest.raises(ValueError, match="Tropycal basin catalog"):
            backend._search()


class TestDownload:
    """Tests for download() end-to-end against the fake SDK."""

    def test_returns_feature_collection(self, tmp_path, fake_tropycal):
        """download() returns a GeoDataFrame with the point schema + EPSG:4326."""
        result = _backend(tmp_path).download()
        assert isinstance(result, gpd.GeoDataFrame)
        assert set(POINT_COLUMNS).issubset(result.columns)
        assert result.crs.to_epsg() == 4326
        assert len(result) == 3

    def test_track_mode(self, tmp_path, fake_tropycal):
        """geometry='track' returns one LineString row per storm."""
        result = _backend(tmp_path, geometry="track").download()
        assert len(result) == 1
        assert set(TRACK_COLUMNS).issubset(result.columns)

    def test_writes_file(self, tmp_path, fake_tropycal):
        """download() writes one vector file per basin under path."""
        _backend(tmp_path).download()
        written = list(tmp_path.glob("tropycal_north_atlantic_point.gpkg"))
        assert len(written) == 1

    def test_empty_result_writes_nothing(self, tmp_path, fake_tropycal):
        """A window with no storms yields an empty FC and writes no file."""
        result = _backend(tmp_path, start="2010-01-01", end="2010-02-01").download()
        assert len(result) == 0
        assert set(POINT_COLUMNS).issubset(result.columns)
        assert list(tmp_path.glob("*.gpkg")) == []

    def test_trackdataset_memoised_per_basin_source(self, tmp_path, fake_tropycal):
        """Repeated downloads reuse one (basin, source) TrackDataset load (G3)."""
        backend = _backend(tmp_path, start="2004-01-01", end="2006-12-31")
        fake_tropycal.add_storm(2004, _shifted(fake_tropycal, "AL012004"))
        fake_tropycal.add_storm(2006, _shifted(fake_tropycal, "AL012006"))
        backend.download()
        backend.download()
        assert fake_tropycal.construction_count == 1

    def test_two_basins_two_constructions(self, tmp_path, fake_tropycal):
        """Two basins build two TrackDatasets (one per (basin, source))."""
        backend = _backend(tmp_path, variables=["north_atlantic", "east_pacific"])
        backend.download()
        assert fake_tropycal.construction_count == 2

    def test_storm_type_filter(self, tmp_path, fake_tropycal):
        """storm_type keeps only fixes of that type."""
        result = _backend(tmp_path, storm_type="HU").download()
        assert set(result["storm_type"]) == {"HU"}
        assert len(result) == 2

    def test_min_category_filter(self, tmp_path, fake_tropycal):
        """min_category drops fixes below the Saffir-Simpson floor."""
        result = _backend(tmp_path, min_category=1).download()
        assert len(result) == 2
        assert all(c >= 1 for c in result["category"])

    def test_aggregate_guard(self, tmp_path, fake_tropycal):
        """A non-None aggregate is rejected with NotImplementedError."""
        with pytest.raises(NotImplementedError, match="not supported"):
            _backend(tmp_path).download(aggregate=object())

    def test_api_composes_search_fetch(self, tmp_path, fake_tropycal):
        """_api() returns the per-basin FeatureCollection list (C3 hook)."""
        collections = _backend(tmp_path)._api()
        assert len(collections) == 1
        assert len(collections[0]) == 3

    def test_empty_storm_frame_skipped(self, tmp_path, fake_tropycal):
        """A storm whose frame is empty is skipped in the fetch loop."""
        fake_tropycal.seasons[2005].append("EMPTY")
        fake_tropycal.storms["EMPTY"] = pd.DataFrame(
            columns=["time", "lat", "lon", "vmax", "mslp", "type", "wmo_basin", "id", "name", "ace"]
        )
        result = _backend(tmp_path).download()
        assert len(result) == 3

    def test_season_with_no_storms_skipped(self, tmp_path, fake_tropycal, warnings_log):
        """A year tropycal cannot serve is logged and skipped, not fatal."""
        backend = _backend(tmp_path, start="2005-01-01", end="2005-12-31")
        result = backend.download()
        assert len(result) == 3


class _RaisingSeason:
    """A TrackDataset whose get_season raises (simulates an unservable year)."""

    def get_season(self, year):
        raise RuntimeError("no data for that season")


class _RaisingStorm:
    """A TrackDataset whose get_storm raises (simulates an unreadable storm)."""

    def get_storm(self, storm_id):
        raise RuntimeError("corrupt storm record")


class TestErrorSkips:
    """Tests for the per-season / per-storm skip-on-error paths."""

    def test_season_error_returns_empty_list(self, warnings_log):
        """A season that raises is logged and skipped (returns [])."""
        ids = TropicalCyclone._season_storm_ids(_RaisingSeason(), 2005)
        assert ids == []
        assert any("season 2005 skipped" in m for m in warnings_log)

    def test_storm_error_returns_none(self, warnings_log):
        """A storm that raises is logged and skipped (returns None)."""
        frame = TropicalCyclone._storm_frame(_RaisingStorm(), "AL122005")
        assert frame is None
        assert any("AL122005" in m for m in warnings_log)


class TestAggregateBasins:
    """Tests for the `both` / `all` aggregate basin codes."""

    def test_all_basin_ibtracs(self, tmp_path, fake_tropycal):
        """variables=['all'] with ibtracs resolves, loads, and maps storms."""
        backend = _backend(tmp_path, variables=["all"], source="ibtracs")
        result = backend.download()
        assert len(result) == 3
        assert fake_tropycal.constructions == [("all", "ibtracs")]

    def test_both_basin_hurdat(self, tmp_path, fake_tropycal):
        """variables=['both'] with hurdat resolves and loads the aggregate basin."""
        backend = _backend(tmp_path, variables=["both"], source="hurdat")
        result = backend.download()
        assert len(result) == 3
        assert fake_tropycal.constructions == [("both", "hurdat")]

    def test_all_rejects_hurdat(self, tmp_path):
        """`all` is ibtracs-only, so source='hurdat' is rejected."""
        backend = _backend(tmp_path, variables=["all"], source="hurdat")
        with pytest.raises(ValueError, match="does not serve basin"):
            backend._search()

    def test_both_rejects_ibtracs(self, tmp_path):
        """`both` is hurdat-only, so source='ibtracs' is rejected."""
        backend = _backend(tmp_path, variables=["both"], source="ibtracs")
        with pytest.raises(ValueError, match="does not serve basin"):
            backend._search()


class TestMissingExtra:
    """Tests for the friendly missing-[tropycal]-extra error."""

    def test_download_without_extra_raises_importerror(self, tmp_path, monkeypatch):
        """A failing tropycal import surfaces as a friendly ImportError.

        Forces `import tropycal.tracks` to fail (via sys.modules) so the test
        holds whether or not the [tropycal] extra is installed in the env — the
        dev / wheel-test envs install [all], which includes tropycal.
        """
        monkeypatch.setitem(sys.modules, "tropycal", None)
        monkeypatch.setitem(sys.modules, "tropycal.tracks", None)
        backend = _backend(tmp_path)
        with pytest.raises(ImportError, match=r"earthlens\[tropycal\]"):
            backend.download()


def _shifted(state, storm_id):
    """Clone the default seeded storm frame under a new storm id."""
    frame = state.storms["AL122005"].copy()
    frame["id"] = storm_id
    return frame
