"""Tests for the storm-frame -> FeatureCollection mapper."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from earthlens.tropycal import events

pytestmark = pytest.mark.tropycal

_WINDOW = (dt.datetime(2005, 8, 1), dt.datetime(2005, 9, 1))
_BBOX = (18.0, 31.0, -98.0, -80.0)


def _frame(
    times=("2005-08-25 00:00", "2005-08-25 06:00", "2005-08-28 12:00"),
    lats=(25.4, 25.9, 26.5),
    lons=(-80.3, -81.0, -86.0),
    vmax=(45.0, 80.0, 150.0),
    mslp=(997.0, 985.0, 902.0),
    types_=("TS", "HU", "HU"),
    storm_id="AL122005",
    name="KATRINA",
    basin="north_atlantic",
    ace=20.0,
):
    """Build a per-fix storm frame in the to_dataframe(attrs_as_columns=True) shape."""
    n = len(times)
    return pd.DataFrame(
        {
            "time": pd.to_datetime(list(times)),
            "lat": list(lats),
            "lon": list(lons),
            "vmax": list(vmax),
            "mslp": list(mslp),
            "type": list(types_),
            "wmo_basin": [basin] * n,
            "id": [storm_id] * n,
            "name": [name] * n,
            "ace": [ace] * n,
        }
    )


class TestSaffirSimpsonCategory:
    """Tests for saffir_simpson_category."""

    @pytest.mark.parametrize(
        "vmax_kt, expected",
        [
            (0, 0),
            (33, 0),
            (63, 0),
            (64, 1),
            (82, 1),
            (83, 2),
            (95, 2),
            (96, 3),
            (112, 3),
            (113, 4),
            (136, 4),
            (137, 5),
            (200, 5),
        ],
    )
    def test_thresholds(self, vmax_kt, expected):
        """Each SSHWS wind boundary maps to the right category."""
        assert events.saffir_simpson_category(vmax_kt) == expected

    def test_none_and_nan(self):
        """A missing wind maps to category 0."""
        assert events.saffir_simpson_category(None) == 0
        assert events.saffir_simpson_category(float("nan")) == 0


class TestFrameToFcPoint:
    """Tests for frame_to_fc in point mode."""

    def test_point_schema_and_geometry(self):
        """Point mode yields one row per fix with the point schema + EPSG:4326."""
        fc = events.frame_to_fc(
            [_frame()], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 3
        assert set(events.POINT_COLUMNS).issubset(fc.columns)
        assert fc.crs.to_epsg() == 4326
        assert all(isinstance(g, Point) for g in fc.geometry)

    def test_category_is_derived(self):
        """Point rows carry a Saffir-Simpson category derived from vmax."""
        fc = events.frame_to_fc(
            [_frame()], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert list(fc["category"]) == [0, 1, 5]

    def test_source_and_id_columns(self):
        """Point rows carry the requested source and the storm id/name."""
        fc = events.frame_to_fc(
            [_frame()], geometry="point", window=_WINDOW, bbox=_BBOX, source="ibtracs"
        )
        assert set(fc["source"]) == {"ibtracs"}
        assert set(fc["storm_id"]) == {"AL122005"}
        assert set(fc["name"]) == {"KATRINA"}

    def test_bbox_filters_fixes(self):
        """A fix outside the bbox is dropped at the fix level (G4)."""
        frame = _frame(
            lons=(-80.3, -81.0, -60.0),  # third fix east of the box
        )
        fc = events.frame_to_fc(
            [frame], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 2

    def test_window_filters_fixes(self):
        """A fix outside the time window is dropped at the fix level (G4)."""
        frame = _frame(
            times=("2005-08-25 00:00", "2005-08-25 06:00", "2005-10-01 00:00"),
        )
        fc = events.frame_to_fc(
            [frame], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 2

    def test_no_match_returns_empty(self):
        """A storm wholly outside the window yields an empty point FC."""
        frame = _frame(
            times=("2010-01-01 00:00", "2010-01-01 06:00", "2010-01-02 00:00")
        )
        fc = events.frame_to_fc(
            [frame], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 0
        assert set(events.POINT_COLUMNS).issubset(fc.columns)

    def test_empty_frame_skipped(self):
        """An empty input frame contributes nothing."""
        fc = events.frame_to_fc(
            [pd.DataFrame()],
            geometry="point",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        assert len(fc) == 0

    def test_in_window_but_out_of_box_skipped(self):
        """A storm with in-window fixes all outside the bbox is skipped."""
        frame = _frame(lons=(-60.0, -55.0, -50.0))  # all east of the box
        fc = events.frame_to_fc(
            [frame], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 0


class TestFrameToFcTrack:
    """Tests for frame_to_fc in track mode."""

    def test_track_schema_and_linestring(self):
        """Track mode yields one LineString row per storm with the track schema."""
        fc = events.frame_to_fc(
            [_frame()], geometry="track", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 1
        assert set(events.TRACK_COLUMNS).issubset(fc.columns)
        assert isinstance(fc.geometry.iloc[0], LineString)

    def test_track_summary_attributes(self):
        """The track row summarises max wind, min pressure, max category, and ACE."""
        fc = events.frame_to_fc(
            [_frame()], geometry="track", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        row = fc.iloc[0]
        assert row["max_vmax_kt"] == 150.0
        assert row["min_mslp_hpa"] == 902.0
        assert int(row["max_category"]) == 5
        assert row["ace"] == 20.0

    def test_track_linestring_clipped_to_window(self):
        """The track LineString is built only from in-window fixes (G4)."""
        frame = _frame(
            times=("2005-08-25 00:00", "2005-08-26 00:00", "2005-10-01 00:00"),
        )
        fc = events.frame_to_fc(
            [frame], geometry="track", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc.geometry.iloc[0].coords) == 2

    def test_track_single_fix_skipped(self):
        """A storm with fewer than two in-window fixes forms no LineString."""
        frame = _frame(
            times=("2005-08-25 00:00", "2005-10-01 00:00", "2005-10-02 00:00"),
        )
        fc = events.frame_to_fc(
            [frame], geometry="track", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 0

    def test_track_included_when_any_fix_in_box(self):
        """A storm is included in track mode when any fix falls in the window+bbox."""
        frame = _frame(
            lons=(-86.0, -60.0, -55.0),  # only first fix in the box
        )
        fc = events.frame_to_fc(
            [frame], geometry="track", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 1


class TestEmptyFc:
    """Tests for empty_fc."""

    def test_point_schema(self):
        """empty_fc('point') has the point columns, zero rows, EPSG:4326."""
        fc = events.empty_fc("point")
        assert len(fc) == 0
        assert set(events.POINT_COLUMNS).issubset(fc.columns)
        assert fc.crs.to_epsg() == 4326

    def test_track_schema(self):
        """empty_fc('track') has the track columns and zero rows."""
        fc = events.empty_fc("track")
        assert len(fc) == 0
        assert set(events.TRACK_COLUMNS).issubset(fc.columns)


class TestFirstHelper:
    """Tests for the _first sequence helper."""

    def test_none(self):
        """_first returns None for a None input."""
        assert events._first(None) is None

    def test_empty_series(self):
        """_first returns None for an empty Series."""
        assert events._first(pd.Series([], dtype="float64")) is None

    def test_scalar_passthrough(self):
        """_first returns a non-Series scalar unchanged."""
        assert events._first("AL122005") == "AL122005"

    def test_series_first_element(self):
        """_first returns the first element of a populated Series."""
        assert events._first(pd.Series([1, 2, 3])) == 1


class TestPrepareFrame:
    """Tests for the _prepare_frame normalization helper."""

    def test_missing_time_column_raises(self):
        """A frame with neither 'time' nor 'date' raises a clear KeyError."""
        bad = pd.DataFrame({"lat": [1.0], "lon": [2.0]})
        with pytest.raises(KeyError, match="no 'time' or 'date' column"):
            events.frame_to_fc(
                [bad], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
            )

    def test_date_column_accepted(self):
        """A frame using a 'date' column instead of 'time' is accepted."""
        frame = _frame().rename(columns={"time": "date"})
        fc = events.frame_to_fc(
            [frame], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        assert len(fc) == 3


def _recon_frame(
    times=("2005-08-28 12:00", "2005-08-28 12:10"),
    lats=(25.0, 25.1),
    lons=(-85.0, -85.1),
):
    """Build a fake recon hdobs.data-shaped frame."""
    return pd.DataFrame(
        {
            "time": pd.to_datetime(list(times)),
            "lat": list(lats),
            "lon": list(lons),
            "wspd": [120.0, 125.0][: len(times)],
            "p_sfc": [945.0, 942.0][: len(times)],
            "temp": [22.0, 22.5][: len(times)],
        }
    )


class TestReconToFc:
    """Tests for the recon observation mapper."""

    def test_maps_obs_points(self):
        """recon_to_fc maps obs to points with the recon schema + EPSG:4326."""
        fc = events.recon_to_fc(
            _recon_frame(),
            storm_id="AL122005",
            recon_product="hdobs",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        assert len(fc) == 2
        assert set(events.RECON_COLUMNS).issubset(fc.columns)
        assert fc.crs.to_epsg() == 4326
        assert list(fc["wspd_kt"]) == [120.0, 125.0]
        assert set(fc["storm_id"]) == {"AL122005"}

    def test_none_frame_empty(self):
        """A None frame (no recon data) returns an empty recon FC."""
        fc = events.recon_to_fc(
            None,
            storm_id="X",
            recon_product="hdobs",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        assert len(fc) == 0
        assert set(events.RECON_COLUMNS).issubset(fc.columns)

    def test_window_bbox_filter(self):
        """Obs outside the window/bbox are dropped."""
        frame = _recon_frame(
            times=("2005-08-28 12:00", "2010-01-01 00:00"), lons=(-85.0, -85.1)
        )
        fc = events.recon_to_fc(
            frame,
            storm_id="X",
            recon_product="hdobs",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        assert len(fc) == 1

    def test_all_filtered_empty(self):
        """When every obs is filtered out, an empty recon FC is returned."""
        frame = _recon_frame(lons=(-60.0, -55.0))  # both east of the box
        fc = events.recon_to_fc(
            frame,
            storm_id="X",
            recon_product="hdobs",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        assert len(fc) == 0

    def test_nan_met_field_kept_as_nan(self):
        """An obs with a missing met field is kept; the column is NaN."""
        frame = _recon_frame()
        frame.loc[0, "wspd"] = float("nan")
        fc = events.recon_to_fc(
            frame,
            storm_id="X",
            recon_product="hdobs",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        assert len(fc) == 2
        assert pd.isna(fc["wspd_kt"].iloc[0])

    def test_nan_position_obs_dropped(self):
        """An obs with a NaN lat/lon is dropped by the bbox filter."""
        frame = _recon_frame()
        frame.loc[0, "lat"] = float("nan")
        fc = events.recon_to_fc(
            frame,
            storm_id="X",
            recon_product="hdobs",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        assert len(fc) == 1

    def test_concat_recon_fcs(self):
        """concat_recon_fcs unions per-storm recon collections; empty fallback."""
        a = events.recon_to_fc(
            _recon_frame(),
            storm_id="A",
            recon_product="hdobs",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        merged = events.concat_recon_fcs([a, events.empty_recon_fc()])
        assert len(merged) == 2
        empty = events.concat_recon_fcs([events.empty_recon_fc()])
        assert len(empty) == 0
        assert set(events.RECON_COLUMNS).issubset(empty.columns)


class TestColumnHelper:
    """Tests for the _column accessor."""

    def test_present_column(self):
        """_column returns the column when present."""
        out = events._column(pd.DataFrame({"vmax": [1, 2]}), "vmax")
        assert list(out) == [1, 2]

    def test_missing_column_all_null(self):
        """_column returns an all-null Series of the right length when absent."""
        out = events._column(pd.DataFrame({"a": [1, 2, 3]}), "id")
        assert len(out) == 3
        assert out.isna().all()


class TestConcatFcs:
    """Tests for concat_fcs."""

    def test_union(self):
        """concat_fcs row-unions non-empty collections."""
        a = events.frame_to_fc(
            [_frame()], geometry="point", window=_WINDOW, bbox=_BBOX, source="hurdat"
        )
        b = events.frame_to_fc(
            [_frame(storm_id="AL132005", name="OPHELIA")],
            geometry="point",
            window=_WINDOW,
            bbox=_BBOX,
            source="hurdat",
        )
        merged = events.concat_fcs([a, b], "point")
        assert len(merged) == 6

    def test_all_empty_returns_empty(self):
        """concat_fcs over only-empty inputs returns a schema-correct empty FC."""
        merged = events.concat_fcs([events.empty_fc("track")], "track")
        assert len(merged) == 0
        assert set(events.TRACK_COLUMNS).issubset(merged.columns)


def _btk_frame(
    times=("2026-09-02 12:00", "2026-09-02 18:00"),
    lats=(13.0, 13.3),
    lons=(-153.0, -153.7),
    vmax=(120.0, 140.0),
    mslp=(945.0, 927.0),
    types_=("HU", "HU"),
    storm_id="EP122026",
    name="LOWELL",
    basin="east_pacific",
    r34_ne=(70.0, 80.0),
    r64_ne=(25.0, 30.0),
):
    """Build a folded NHC best-track frame (track fields + two radii columns)."""
    n = len(times)
    data = {
        "id": [storm_id] * n,
        "name": [name] * n,
        "time": pd.to_datetime(list(times)),
        "lat": list(lats),
        "lon": list(lons),
        "vmax": list(vmax),
        "mslp": list(mslp),
        "type": list(types_),
        "wmo_basin": [basin] * n,
        "r34_ne": list(r34_ne),
        "r64_ne": list(r64_ne),
    }
    return pd.DataFrame(data)


_BTK_WINDOW = (dt.datetime(2026, 8, 25), dt.datetime(2026, 9, 11))
_BTK_BBOX = (-5.0, 40.0, -178.0, -75.0)


class TestBtkFrameToFc:
    """Tests for btk_frame_to_fc (NHC operational best-track point mode)."""

    def test_maps_points_with_radii(self):
        """In-window fixes map to points carrying the full radii schema."""
        fc = events.btk_frame_to_fc(
            [_btk_frame()], window=_BTK_WINDOW, bbox=_BTK_BBOX, source="nhc"
        )
        assert len(fc) == 2
        assert set(events.RADII_COLUMNS).issubset(fc.columns)
        peak = fc.loc[fc["vmax_kt"].idxmax()]
        assert peak["r34_ne"] == 80.0
        assert peak["r64_ne"] == 30.0
        assert int(peak["category"]) == 5
        assert set(fc["source"]) == {"nhc"}

    def test_absent_threshold_is_nan(self):
        """A radii column the frame never carries degrades to NaN, not an error."""
        fc = events.btk_frame_to_fc(
            [_btk_frame()], window=_BTK_WINDOW, bbox=_BTK_BBOX, source="nhc"
        )
        assert fc["r50_sw"].isna().all()

    def test_out_of_window_returns_empty_schema(self):
        """A storm wholly outside the window yields an empty but typed FC."""
        fc = events.btk_frame_to_fc(
            [_btk_frame(times=("2020-01-01 00:00", "2020-01-01 06:00"))],
            window=_BTK_WINDOW,
            bbox=_BTK_BBOX,
            source="nhc",
        )
        assert len(fc) == 0
        assert set(events.BTK_POINT_COLUMNS).issubset(fc.columns)


class TestEmptyBtkFc:
    """Tests for the empty NHC best-track collection."""

    def test_schema_and_crs(self):
        """empty_btk_fc carries the radii columns and EPSG:4326."""
        fc = events.empty_btk_fc()
        assert len(fc) == 0
        assert set(events.RADII_COLUMNS).issubset(fc.columns)
        assert fc.crs.to_epsg() == 4326


class TestConcatBtkFcs:
    """Tests for concat_btk_fcs."""

    def test_union(self):
        """concat_btk_fcs row-unions non-empty NHC best-track collections."""
        a = events.btk_frame_to_fc(
            [_btk_frame()], window=_BTK_WINDOW, bbox=_BTK_BBOX, source="nhc"
        )
        b = events.btk_frame_to_fc(
            [_btk_frame(storm_id="EP112026", name="KARINA")],
            window=_BTK_WINDOW,
            bbox=_BTK_BBOX,
            source="nhc",
        )
        merged = events.concat_btk_fcs([a, b])
        assert len(merged) == 4

    def test_all_empty_returns_empty(self):
        """concat_btk_fcs over only-empty inputs returns a schema-correct empty FC."""
        merged = events.concat_btk_fcs([events.empty_btk_fc()])
        assert len(merged) == 0
        assert set(events.RADII_COLUMNS).issubset(merged.columns)
