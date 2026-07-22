"""Unit tests for `earthlens.fdsn.events` (obspy.Catalog -> FeatureCollection)."""

from __future__ import annotations

from typing import Callable

import pytest
from earthlens.fdsn.events import (
    ATTRIBUTE_COLUMNS,
    EVENT_CRS,
    catalog_to_fc,
    concat_fcs,
    empty_fc,
)
from geopandas import GeoDataFrame
from obspy.core.event import Catalog, Event


@pytest.mark.fdsn
class TestCatalogToFc:
    """`catalog_to_fc` maps an obspy catalog to a vector FeatureCollection."""

    def test_row_per_event(self, make_event: Callable[..., Event]):
        """One feature is produced per event in the catalog."""
        cat = Catalog(events=[make_event(), make_event(lon=10.0, lat=20.0)])
        fc = catalog_to_fc(cat, "USGS")
        assert len(fc) == 2, f"expected 2 rows, got {len(fc)}"

    def test_schema_columns_present(self, make_catalog: Callable[..., Catalog]):
        """The output carries every attribute column plus geometry."""
        fc = catalog_to_fc(make_catalog(), "USGS")
        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"
        assert "geometry" in fc.columns, "geometry column must be present"

    def test_crs_is_wgs84(self, make_catalog: Callable[..., Catalog]):
        """The collection is tagged EPSG:4326."""
        fc = catalog_to_fc(make_catalog(), "USGS")
        assert fc.crs.to_epsg() == 4326, f"expected EPSG:4326, got {fc.crs}"

    def test_is_geodataframe(self, make_catalog: Callable[..., Catalog]):
        """The result is a GeoDataFrame subclass (pyramids FeatureCollection)."""
        fc = catalog_to_fc(make_catalog(), "USGS")
        assert isinstance(fc, GeoDataFrame), (
            "FeatureCollection must subclass GeoDataFrame"
        )

    def test_depth_metres_to_km(self, make_event: Callable[..., Event]):
        """obspy depth (metres) is converted to kilometres."""
        fc = catalog_to_fc(Catalog(events=[make_event(depth_m=25000.0)]), "USGS")
        assert float(fc["depth_km"].iloc[0]) == 25.0, (
            f"25000 m should map to 25 km, got {fc['depth_km'].iloc[0]}"
        )

    def test_provider_column_stamped(self, make_catalog: Callable[..., Catalog]):
        """The provider key is recorded on every row."""
        fc = catalog_to_fc(make_catalog(), "EMSC")
        assert (fc["provider"] == "EMSC").all(), "every row must carry the provider key"

    def test_geometry_from_lon_lat(self, make_event: Callable[..., Event]):
        """The Point geometry is built from origin longitude/latitude."""
        fc = catalog_to_fc(Catalog(events=[make_event(lon=12.5, lat=42.0)]), "USGS")
        point = fc.geometry.iloc[0]
        assert (point.x, point.y) == (12.5, 42.0), f"unexpected point {point}"

    def test_magnitude_fields(self, make_event: Callable[..., Event]):
        """Magnitude value and type are taken from the preferred magnitude."""
        fc = catalog_to_fc(
            Catalog(events=[make_event(mag=6.3, mag_type="Mww")]), "USGS"
        )
        assert float(fc["magnitude"].iloc[0]) == 6.3
        assert fc["magnitude_type"].iloc[0] == "Mww"

    def test_preferred_origin_fallback_to_preferred(
        self, make_event: Callable[..., Event]
    ):
        """The preferred origin wins over a decoy first origin."""
        fc = catalog_to_fc(
            Catalog(events=[make_event(lon=12.5, lat=42.0, extra_first_origin=True)]),
            "USGS",
        )
        assert float(fc["longitude"].iloc[0]) == 12.5, (
            "preferred origin coordinates must be used, not origins[0]"
        )

    def test_no_magnitude_yields_na(self, make_event: Callable[..., Event]):
        """An event without magnitudes maps to a missing magnitude value."""
        fc = catalog_to_fc(Catalog(events=[make_event(with_magnitude=False)]), "USGS")
        assert fc["magnitude"].isna().iloc[0], "no-magnitude event should be NA"

    def test_origin_less_event_has_null_geometry(
        self, make_event: Callable[..., Event]
    ):
        """An event with no usable origin yields a null geometry, not POINT(nan nan)."""
        event = Event(magnitudes=[], origins=[], event_type="earthquake")
        fc = catalog_to_fc(Catalog(events=[event]), "USGS")
        assert len(fc) == 1, "the row is still emitted"
        assert fc.geometry.iloc[0] is None, "missing-origin geometry must be null"

    def test_mixed_origin_presence(self, make_event: Callable[..., Event]):
        """A normal event keeps its Point while an origin-less one is null."""
        normal = make_event(lon=12.5, lat=42.0)
        missing = Event(magnitudes=[], origins=[], event_type="earthquake")
        fc = catalog_to_fc(Catalog(events=[normal, missing]), "USGS")
        assert fc.geometry.iloc[0] is not None and fc.geometry.iloc[1] is None

    def test_empty_catalog_returns_empty_fc(self):
        """An empty catalog maps to an empty FeatureCollection with the schema."""
        fc = catalog_to_fc(Catalog(events=[]), "USGS")
        assert len(fc) == 0, "empty catalog must yield zero rows"
        assert "geometry" in fc.columns
        assert fc.crs.to_epsg() == 4326


@pytest.mark.fdsn
class TestEmptyFc:
    """`empty_fc` returns a schema-correct, zero-row FeatureCollection."""

    def test_zero_rows(self):
        """The empty collection has no rows."""
        assert len(empty_fc()) == 0, "empty_fc must have zero rows"

    def test_columns_and_dtypes(self):
        """Every declared column is present with its dtype."""
        fc = empty_fc()
        for column, dtype in ATTRIBUTE_COLUMNS.items():
            assert column in fc.columns, f"missing column {column!r}"
            assert str(fc[column].dtype) == dtype, (
                f"{column!r} dtype {fc[column].dtype} != declared {dtype}"
            )

    def test_crs_is_wgs84(self):
        """The empty collection is tagged EPSG:4326."""
        assert empty_fc().crs.to_epsg() == 4326


@pytest.mark.fdsn
class TestConcatFcs:
    """`concat_fcs` unions per-provider collections."""

    def test_concat_two_nonempty(self, make_catalog: Callable[..., Catalog]):
        """Two single-event collections concatenate to two rows."""
        a = catalog_to_fc(make_catalog(), "USGS")
        b = catalog_to_fc(make_catalog(), "EMSC")
        merged = concat_fcs([a, b])
        assert len(merged) == 2, f"expected 2 rows, got {len(merged)}"
        assert merged.crs.to_epsg() == 4326

    def test_skips_empty_inputs(self, make_catalog: Callable[..., Catalog]):
        """Empty collections are dropped from the union."""
        merged = concat_fcs([empty_fc(), catalog_to_fc(make_catalog(), "USGS")])
        assert len(merged) == 1, f"expected 1 row, got {len(merged)}"

    def test_all_empty_returns_empty(self):
        """An all-empty (or empty) list returns a schema-correct empty FC."""
        merged = concat_fcs([empty_fc(), empty_fc()])
        assert len(merged) == 0
        assert merged.crs.to_epsg() == 4326

    def test_empty_list_returns_empty(self):
        """An empty input list returns an empty FeatureCollection."""
        assert len(concat_fcs([])) == 0


@pytest.mark.fdsn
def test_event_crs_constant():
    """The module CRS constant is WGS84."""
    assert EVENT_CRS == "EPSG:4326"
