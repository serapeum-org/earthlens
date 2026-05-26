"""Tests for the FIRMS CSV -> FeatureCollection mapper."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest

from earthlens.firms.events import (
    ATTRIBUTE_COLUMNS,
    concat,
    csv_to_fc,
    empty_fc,
)

pytestmark = pytest.mark.firms


def _viirs_frame() -> pd.DataFrame:
    """One VIIRS detection row with the categorical confidence token."""
    return pd.DataFrame(
        {
            "latitude": [34.0],
            "longitude": [-118.0],
            "acq_date": ["2024-08-01"],
            "acq_time": [1325],
            "satellite": ["N"],
            "confidence": ["l"],
            "bright_ti4": [320.0],
            "bright_ti5": [295.0],
            "frp": [12.5],
            "daynight": ["D"],
        }
    )


def _modis_frame() -> pd.DataFrame:
    """One MODIS detection row with numeric 0-100 confidence."""
    return pd.DataFrame(
        {
            "latitude": [34.0],
            "longitude": [-118.0],
            "acq_date": ["2024-08-01"],
            "acq_time": [5],
            "satellite": ["Terra"],
            "confidence": [85],
            "brightness": [330.0],
            "bright_t31": [290.0],
            "frp": [40.0],
            "daynight": ["N"],
        }
    )


def test_viirs_categorical_confidence_normalised():
    """VIIRS l/n/h map to 25/60/90 in confidence_pct; raw kept."""
    fc = csv_to_fc(_viirs_frame(), "VIIRS_SNPP_NRT", "VIIRS")
    assert fc["confidence"].iloc[0] == "l"
    assert float(fc["confidence_pct"].iloc[0]) == 25.0
    assert float(fc["brightness_k"].iloc[0]) == 320.0


def test_modis_numeric_confidence_passthrough():
    """MODIS numeric confidence passes through; brightness from `brightness`."""
    fc = csv_to_fc(_modis_frame(), "MODIS_NRT", "MODIS")
    assert float(fc["confidence_pct"].iloc[0]) == 85.0
    assert float(fc["brightness_k"].iloc[0]) == 330.0


def test_acq_time_integer_hhmm_parsed():
    """Integer HHMM acq_time is zero-padded into the UTC datetime."""
    viirs = csv_to_fc(_viirs_frame(), "VIIRS_SNPP_NRT", "VIIRS")
    assert viirs["acq_datetime"].iloc[0].strftime("%H:%M") == "13:25"
    modis = csv_to_fc(_modis_frame(), "MODIS_NRT", "MODIS")
    assert modis["acq_datetime"].iloc[0].strftime("%H:%M") == "00:05"
    assert str(viirs["acq_datetime"].dtype) == "datetime64[ns, UTC]"


def test_geometry_and_crs():
    """Geometry is Point(lon, lat) at EPSG:4326."""
    fc = csv_to_fc(_viirs_frame(), "VIIRS_SNPP_NRT", "VIIRS")
    assert fc.crs.to_epsg() == 4326
    assert (fc.geometry.iloc[0].x, fc.geometry.iloc[0].y) == (-118.0, 34.0)
    assert isinstance(fc, gpd.GeoDataFrame)


def test_min_confidence_filters_on_pct():
    """min_confidence drops a VIIRS l=25 row at threshold 50."""
    fc = csv_to_fc(_viirs_frame(), "VIIRS_SNPP_NRT", "VIIRS", min_confidence=50)
    assert len(fc) == 0
    assert set(ATTRIBUTE_COLUMNS).issubset(fc.columns)


def test_day_night_filter():
    """day_night keeps only matching rows."""
    fc = csv_to_fc(_modis_frame(), "MODIS_NRT", "MODIS", day_night="D")
    assert len(fc) == 0
    kept = csv_to_fc(_modis_frame(), "MODIS_NRT", "MODIS", day_night="N")
    assert len(kept) == 1


def test_empty_frame_returns_schema_only_fc():
    """An empty input frame yields a schema-correct empty collection."""
    fc = csv_to_fc(pd.DataFrame(), "MODIS_NRT", "MODIS")
    assert len(fc) == 0
    assert set(ATTRIBUTE_COLUMNS).issubset(fc.columns)
    assert fc.crs.to_epsg() == 4326


def test_missing_brightness_column_degrades_to_nan():
    """A frame lacking the family brightness column degrades to NaN."""
    frame = _viirs_frame().drop(columns=["bright_ti4"])
    fc = csv_to_fc(frame, "VIIRS_SNPP_NRT", "VIIRS")
    assert pd.isna(fc["brightness_k"].iloc[0])


def test_concat_unions_collections():
    """concat merges populated collections and stays schema-stable."""
    a = csv_to_fc(_viirs_frame(), "VIIRS_SNPP_NRT", "VIIRS")
    b = csv_to_fc(_modis_frame(), "MODIS_NRT", "MODIS")
    merged = concat([a, b, empty_fc()])
    assert len(merged) == 2
    assert merged.crs.to_epsg() == 4326


def test_concat_all_empty_is_empty():
    """concat of only empties returns a schema-only empty collection."""
    merged = concat([empty_fc(), empty_fc()])
    assert len(merged) == 0
    assert set(ATTRIBUTE_COLUMNS).issubset(merged.columns)


def test_missing_confidence_column_degrades_to_nan():
    """A frame without a confidence column yields NaN confidence_pct."""
    frame = _modis_frame().drop(columns=["confidence"])
    fc = csv_to_fc(frame, "MODIS_NRT", "MODIS")
    assert pd.isna(fc["confidence_pct"].iloc[0])


def test_missing_satellite_column_degrades_to_na():
    """A frame without a satellite column yields a null satellite."""
    frame = _viirs_frame().drop(columns=["satellite"])
    fc = csv_to_fc(frame, "VIIRS_SNPP_NRT", "VIIRS")
    assert pd.isna(fc["satellite"].iloc[0])


def test_goes_numeric_confidence_passthrough():
    """GOES confidence is numeric and passes through unscaled."""
    frame = _viirs_frame().assign(confidence=[0.967])
    fc = csv_to_fc(frame, "GOES_NRT", "GOES")
    assert float(fc["confidence_pct"].iloc[0]) == 0.967
    assert float(fc["brightness_k"].iloc[0]) == 320.0  # bright_ti4 source


def test_landsat_categorical_confidence_and_missing_columns():
    """LANDSAT maps l/m/h and degrades frp/brightness (no such columns) to NaN."""
    frame = pd.DataFrame(
        {
            "latitude": [46.6],
            "longitude": [-68.4],
            "acq_date": ["2026-05-22"],
            "acq_time": [1524],
            "satellite": ["L8"],
            "confidence": ["M"],
            "daynight": ["D"],
        }
    )
    fc = csv_to_fc(frame, "LANDSAT_NRT", "LANDSAT")
    assert float(fc["confidence_pct"].iloc[0]) == 60.0
    assert pd.isna(fc["frp"].iloc[0])
    assert pd.isna(fc["brightness_k"].iloc[0])
    assert fc["confidence"].iloc[0] == "M"
