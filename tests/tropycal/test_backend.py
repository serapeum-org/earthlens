"""Tests for the TropicalCyclone backend (fake tropycal SDK, no network)."""

from __future__ import annotations

import sys

import geopandas as gpd
import pandas as pd
import pytest

from earthlens.tropycal import TropicalCyclone
from earthlens.tropycal.events import POINT_COLUMNS, RECON_COLUMNS, TRACK_COLUMNS

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
        written = list(tmp_path.glob("tropycal_besttrack_north_atlantic_point.gpkg"))
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


def _recon_backend(tmp_path, **overrides):
    """Build a recon-product TropicalCyclone over the Gulf/2005 window."""
    kwargs = dict(
        start="2005-08-01",
        end="2005-09-01",
        variables=["AL122005"],
        lat_lim=[18, 31],
        lon_lim=[-98, -80],
        source="hurdat",
        product="recon",
        basin="north_atlantic",
        path=str(tmp_path),
    )
    kwargs.update(overrides)
    return TropicalCyclone(**kwargs)


class TestProductValidation:
    """Tests for the product / recon_product selectors."""

    def test_unknown_product_rejected(self, tmp_path):
        """An unknown product is rejected."""
        with pytest.raises(ValueError, match="product must be one of"):
            _backend(tmp_path, product="bogus")

    def test_bad_recon_product_rejected(self, tmp_path):
        """An unknown recon_product is rejected."""
        with pytest.raises(ValueError, match="recon_product must be one of"):
            _recon_backend(tmp_path, recon_product="bogus")

    def test_recon_requires_storm_variables(self, tmp_path):
        """product='recon' with empty variables raises (storm-keyed)."""
        with pytest.raises(ValueError, match="storm identifier"):
            _recon_backend(tmp_path, variables=[])

    def test_besttrack_is_default(self, tmp_path):
        """product defaults to besttrack (basin-keyed)."""
        assert _backend(tmp_path)._product == "besttrack"


class TestReconProduct:
    """Tests for product='recon' (storm-keyed aircraft observations)."""

    def test_search_one_product_per_storm(self, tmp_path):
        """recon _search emits one product per storm id with basin/source meta."""
        products = _recon_backend(tmp_path, variables=["AL122005", "AL132005"])._search()
        assert [p.id for p in products] == ["AL122005", "AL132005"]
        assert products[0].metadata["basin"] == "north_atlantic"
        assert products[0].metadata["recon_product"] == "hdobs"

    def test_download_returns_recon_points(self, tmp_path, fake_recon):
        """recon download returns a FeatureCollection of obs points."""
        result = _recon_backend(tmp_path).download()
        assert isinstance(result, gpd.GeoDataFrame)
        assert set(RECON_COLUMNS).issubset(result.columns)
        assert result.crs.to_epsg() == 4326
        assert len(result) == 3
        assert set(result["storm_id"]) == {"AL122005"}
        assert set(result["recon_product"]) == {"hdobs"}

    def test_download_writes_storm_named_file(self, tmp_path, fake_recon):
        """recon download writes one file named by storm + recon_product."""
        _recon_backend(tmp_path).download()
        assert list(tmp_path.glob("tropycal_recon_AL122005_hdobs.gpkg"))

    def test_recon_product_variant(self, tmp_path, fake_recon):
        """recon_product='dropsondes' is stamped on the output rows."""
        result = _recon_backend(tmp_path, recon_product="dropsondes").download()
        assert set(result["recon_product"]) == {"dropsondes"}

    def test_multi_storm_recon_concat(self, tmp_path, fake_recon):
        """A multi-storm recon request unions each storm's obs (per-storm id)."""
        fake_recon.storms["AL132005"] = fake_recon.storms["AL122005"]
        result = _recon_backend(tmp_path, variables=["AL122005", "AL132005"]).download()
        assert len(result) == 6
        assert set(result["storm_id"]) == {"AL122005", "AL132005"}

    def test_recon_ignores_geometry_track(self, tmp_path, fake_recon):
        """recon always yields obs points, even when geometry='track' is passed."""
        result = _recon_backend(tmp_path, geometry="track").download()
        assert set(RECON_COLUMNS).issubset(result.columns)
        assert result.geometry.iloc[0].geom_type == "Point"

    def test_no_recon_data_empty(self, tmp_path, fake_recon):
        """A storm with no recon data yields an empty recon FC, no file."""
        fake_recon.recon_frame = None
        result = _recon_backend(tmp_path).download()
        assert len(result) == 0
        assert set(RECON_COLUMNS).issubset(result.columns)
        assert list(tmp_path.glob("*.gpkg")) == []

    def test_obs_outside_window_or_bbox_dropped(self, tmp_path, fake_recon, make_recon_obs_frame):
        """recon obs outside the window/bbox are filtered out."""
        fake_recon.recon_frame = make_recon_obs_frame(
            times=["2005-08-28 12:00", "2005-08-28 12:10", "2010-01-01 00:00"],
            lons=[-85.0, -60.0, -85.0],  # 2nd east of box, 3rd out of window
        )
        result = _recon_backend(tmp_path).download()
        assert len(result) == 1

    def test_unresolvable_storm_empty(self, tmp_path, fake_recon):
        """A storm id the dataset cannot resolve yields an empty recon FC."""
        fake_recon.storms = {}  # the fake get_storm will KeyError
        result = _recon_backend(tmp_path, variables=["NOPE"]).download()
        assert len(result) == 0

    def test_recon_builder_error_empty(self, tmp_path, fake_recon, monkeypatch):
        """A recon sub-product that errors is logged and yields an empty FC."""
        monkeypatch.setattr(sys.modules["tropycal.recon"], "hdobs", _boom_builder)
        result = _recon_backend(tmp_path).download()
        assert len(result) == 0

    def test_recon_missing_extra_importerror(self, tmp_path, fake_tropycal, monkeypatch):
        """A failing tropycal.recon import surfaces a friendly ImportError."""
        monkeypatch.setitem(sys.modules, "tropycal.recon", None)
        with pytest.raises(ImportError, match=r"earthlens\[tropycal\]"):
            _recon_backend(tmp_path).download()


def _ships_backend(tmp_path, **overrides):
    """Build a ships-product TropicalCyclone for one storm + forecast cycle."""
    kwargs = dict(
        start="2022-09-20",
        end="2022-10-01",
        variables=["AL092022"],
        lat_lim=[-90, 90],
        lon_lim=[-180, 180],
        source="hurdat",
        product="ships",
        basin="north_atlantic",
        ships_time="2022-09-27 00:00",
        path=str(tmp_path),
    )
    kwargs.update(overrides)
    return TropicalCyclone(**kwargs)


class TestShipsProduct:
    """Tests for product='ships' (tabular SHIPS forecast guidance)."""

    def test_output_kind_tabular(self, tmp_path):
        """A ships instance declares OUTPUT_KIND='tabular'."""
        assert _ships_backend(tmp_path).OUTPUT_KIND == "tabular"

    def test_ships_requires_time(self, tmp_path):
        """product='ships' without ships_time is rejected."""
        with pytest.raises(ValueError, match="ships_time"):
            _ships_backend(tmp_path, ships_time=None)

    def test_ships_empty_variables_rejected(self, tmp_path):
        """product='ships' is storm-keyed: empty variables is rejected."""
        with pytest.raises(ValueError, match="storm-keyed"):
            _ships_backend(tmp_path, variables=[])

    def test_multi_storm_ships_concat(self, tmp_path, fake_ships):
        """A multi-storm ships request stacks each storm's guidance table."""
        fake_ships.storms["AL102022"] = fake_ships.storms["AL092022"]
        result = _ships_backend(tmp_path, variables=["AL092022", "AL102022"]).download()
        assert len(result) == 6
        assert set(result["storm_id"]) == {"AL092022", "AL102022"}

    def test_download_returns_dataframe(self, tmp_path, fake_ships):
        """ships download returns a DataFrame with storm_id/forecast_init + fhr."""
        result = _ships_backend(tmp_path).download()
        assert isinstance(result, pd.DataFrame)
        assert not isinstance(result, gpd.GeoDataFrame)
        assert {"storm_id", "forecast_init", "fhr", "vmax_noland_kt"}.issubset(result.columns)
        assert len(result) == 3
        assert set(result["storm_id"]) == {"AL092022"}

    def test_download_writes_csv(self, tmp_path, fake_ships):
        """ships download writes a per-storm CSV named by storm + cycle."""
        _ships_backend(tmp_path).download()
        assert list(tmp_path.glob("tropycal_ships_AL092022_20220927T00.csv"))

    def test_no_ships_data_empty_frame(self, tmp_path, fake_ships):
        """A cycle with no SHIPS guidance yields an empty DataFrame, no file."""
        fake_ships.ships_frame = None
        result = _ships_backend(tmp_path).download()
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(tmp_path.glob("*.csv")) == []

    def test_aggregate_rejected_for_tabular(self, tmp_path, fake_ships):
        """A non-None aggregate is rejected for the tabular ships product too."""
        with pytest.raises(NotImplementedError, match="not supported"):
            _ships_backend(tmp_path).download(aggregate=object())


def _realtime_backend(tmp_path, **overrides):
    """Build a realtime-product TropicalCyclone (whole-earth, no window)."""
    kwargs = dict(
        start="2026-01-01",
        end="2026-12-31",
        variables=[],
        lat_lim=[-90, 90],
        lon_lim=[-180, 180],
        product="realtime",
        path=str(tmp_path),
    )
    kwargs.update(overrides)
    return TropicalCyclone(**kwargs)


class TestRealtimeProduct:
    """Tests for product='realtime' (live active storms, vector)."""

    def test_output_kind_vector(self, tmp_path):
        """A realtime instance is vector."""
        assert _realtime_backend(tmp_path).OUTPUT_KIND == "vector"

    def test_empty_variables_allowed(self, tmp_path):
        """realtime allows empty variables (means: all active storms)."""
        assert _realtime_backend(tmp_path).vars == []

    def test_download_all_active(self, tmp_path, fake_realtime):
        """realtime download maps every active storm's current track to points."""
        result = _realtime_backend(tmp_path).download()
        assert isinstance(result, gpd.GeoDataFrame)
        assert set(POINT_COLUMNS).issubset(result.columns)
        assert len(result) == 3
        assert set(result["source"]) == {"realtime"}

    def test_download_selects_requested_id(self, tmp_path, fake_realtime):
        """A requested active id is selected; a non-active id yields nothing."""
        assert len(_realtime_backend(tmp_path, variables=["AL012026"]).download()) == 3
        assert len(_realtime_backend(tmp_path, variables=["ZZ999999"]).download()) == 0

    def test_no_active_storms_empty(self, tmp_path, fake_realtime):
        """When nothing is active (off-season), an empty FC is returned."""
        fake_realtime.active_ids = []
        result = _realtime_backend(tmp_path).download()
        assert len(result) == 0
        assert set(POINT_COLUMNS).issubset(result.columns)

    def test_unreadable_active_storm_skipped(self, tmp_path, fake_realtime):
        """An active storm that can't be read is skipped (empty, not fatal)."""
        fake_realtime.active_ids = ["GHOST"]  # not in state.storms -> KeyError
        result = _realtime_backend(tmp_path).download()
        assert len(result) == 0

    def test_missing_extra_importerror(self, tmp_path, fake_tropycal, monkeypatch):
        """A failing tropycal.realtime import surfaces a friendly ImportError."""
        monkeypatch.setitem(sys.modules, "tropycal.realtime", None)
        with pytest.raises(ImportError, match=r"earthlens\[tropycal\]"):
            _realtime_backend(tmp_path).download()


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


def _boom_builder(*args, **kwargs):
    """A recon sub-product builder stand-in that raises (decode failure)."""
    raise RuntimeError("recon decode failed")
