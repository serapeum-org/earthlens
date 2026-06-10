from __future__ import annotations

import pytest

from earthlens.base.spatial import (
    crop_to_aoi,
    estimate_pixel_dims,
    normalize_aoi,
    resolve_aoi,
)

# The bbox every form below describes: lon in [-75, -74], lat in [4, 5].
EXPECTED = ([4.0, 5.0], [-75.0, -74.0])


class TestNormalizeAoiBbox:
    """Bounding-box forms of the aoi= input."""

    def test_bbox_list_is_read_as_wsen(self):
        """A 4-list is interpreted as W, S, E, N."""
        assert normalize_aoi([-75.0, 4.0, -74.0, 5.0]) == EXPECTED

    def test_bbox_tuple_with_ints_casts_to_float(self):
        """A 4-tuple of ints is cast to float pairs."""
        assert normalize_aoi((-75, 4, -74, 5)) == EXPECTED

    def test_bbox_dict_min_lon_spelling(self):
        """The min_lon / min_lat spelling is accepted."""
        aoi = {"min_lon": -75, "min_lat": 4, "max_lon": -74, "max_lat": 5}
        assert normalize_aoi(aoi) == EXPECTED

    def test_bbox_dict_lonmin_spelling(self):
        """The eodag lonmin / latmin spelling is accepted."""
        aoi = {"lonmin": -75, "latmin": 4, "lonmax": -74, "latmax": 5}
        assert normalize_aoi(aoi) == EXPECTED

    def test_bbox_dict_compass_spelling(self):
        """The west / south / east / north spelling is accepted."""
        aoi = {"west": -75, "south": 4, "east": -74, "north": 5}
        assert normalize_aoi(aoi) == EXPECTED

    def test_bbox_dict_missing_edge_raises(self):
        """A bbox mapping missing an edge raises ValueError."""
        with pytest.raises(ValueError, match="missing 'max_lat'"):
            normalize_aoi({"min_lon": -75, "min_lat": 4, "max_lon": -74})


class TestNormalizeAoiPoint:
    """Point + buffer form of the aoi= input."""

    def test_point_with_buffer_grows_to_square(self):
        """A (lon, lat) point with buffer becomes a square box."""
        assert normalize_aoi((-74.5, 4.5), buffer=0.5) == EXPECTED

    def test_point_without_buffer_raises(self):
        """A point without buffer is rejected."""
        with pytest.raises(ValueError, match="requires buffer"):
            normalize_aoi((-74.5, 4.5))

    def test_point_buffer_clamps_latitude_to_pole(self):
        """A buffered point near the pole clamps latitude to 90."""
        lat_lim, _ = normalize_aoi((0.0, 89.8), buffer=0.5)
        assert lat_lim == [89.3, 90.0]

    def test_point_buffer_clamps_longitude_to_dateline(self):
        """A buffered point near the dateline clamps longitude to 180."""
        _, lon_lim = normalize_aoi((179.8, 0.0), buffer=0.5)
        assert lon_lim == [179.3, 180.0]


class TestNormalizeAoiGeometry:
    """Geometry forms of the aoi= input."""

    def test_wkt_polygon(self):
        """A WKT polygon is reduced to its envelope."""
        wkt = "POLYGON ((-75 4, -74 4, -74 5, -75 5, -75 4))"
        assert normalize_aoi(wkt) == EXPECTED

    def test_geojson_geometry_mapping(self):
        """A GeoJSON Polygon mapping is reduced to its envelope."""
        geom = {
            "type": "Polygon",
            "coordinates": [[[-75, 4], [-74, 4], [-74, 5], [-75, 5], [-75, 4]]],
        }
        assert normalize_aoi(geom) == EXPECTED

    def test_geojson_feature_mapping(self):
        """A GeoJSON Feature is unwrapped to its geometry envelope."""
        feature = {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-75, 4], [-74, 4], [-74, 5], [-75, 5], [-75, 4]]],
            },
        }
        assert normalize_aoi(feature) == EXPECTED

    def test_geojson_bbox_member(self):
        """A mapping carrying a precomputed GeoJSON bbox is honoured."""
        assert normalize_aoi({"type": "Polygon", "bbox": [-75, 4, -74, 5]}) == EXPECTED

    def test_geojson_3d_bbox_member(self):
        """A 6-element (3-D) GeoJSON bbox drops the elevation ordinates."""
        aoi = {"type": "Polygon", "bbox": [-75, 4, 0, -74, 5, 100]}
        assert normalize_aoi(aoi) == EXPECTED

    def test_feature_collection(self):
        """A FeatureCollection envelopes all its features."""
        aoi = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Point", "coordinates": [-75, 4]},
                },
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Point", "coordinates": [-74, 5]},
                },
            ],
        }
        assert normalize_aoi(aoi) == EXPECTED

    def test_geometry_collection(self):
        """A GeometryCollection envelopes all its geometries."""
        aoi = {
            "type": "GeometryCollection",
            "geometries": [
                {"type": "Point", "coordinates": [-75, 4]},
                {"type": "Point", "coordinates": [-74, 5]},
            ],
        }
        assert normalize_aoi(aoi) == EXPECTED


class TestNormalizeAoiMalformedGeojson:
    """GeoJSON mappings that cannot define an area."""

    def test_empty_feature_collection_raises(self):
        """An empty FeatureCollection cannot define an aoi."""
        with pytest.raises(ValueError, match="empty FeatureCollection"):
            normalize_aoi({"type": "FeatureCollection", "features": []})

    def test_geometry_without_coordinates_raises(self):
        """A geometry with empty coordinates has no positions."""
        with pytest.raises(ValueError, match="no coordinates"):
            normalize_aoi({"type": "Polygon", "coordinates": []})

    def test_unrecognised_mapping_raises(self):
        """A typed mapping with no geometry payload is rejected."""
        with pytest.raises(ValueError, match="not a recognised GeoJSON"):
            normalize_aoi({"type": "Nonsense"})

    def test_shapely_geometry_via_geo_interface(self):
        """A shapely geometry is reduced through __geo_interface__."""
        shapely = pytest.importorskip("shapely")
        assert normalize_aoi(shapely.geometry.box(-75, 4, -74, 5)) == EXPECTED

    def test_geodataframe_total_bounds(self):
        """A GeoDataFrame is reduced to its total_bounds."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        gdf = gpd.GeoDataFrame(geometry=[shapely.geometry.box(-75, 4, -74, 5)])
        assert normalize_aoi(gdf) == EXPECTED

    def test_projected_geodataframe_is_reprojected(self):
        """A non-4326 GeoDataFrame is reprojected to lon/lat before its bbox."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        gdf = gpd.GeoDataFrame(
            geometry=[shapely.geometry.box(-75, 4, -74, 5)], crs="EPSG:4326"
        )
        lat_lim, lon_lim = normalize_aoi(gdf.to_crs(3857))
        assert lat_lim == pytest.approx([4.0, 5.0]), f"got {lat_lim}"
        assert lon_lim == pytest.approx([-75.0, -74.0]), f"got {lon_lim}"


class TestNormalizeAoiErrors:
    """Malformed aoi= inputs."""

    def test_inverted_bbox_raises(self):
        """A bbox with min > max raises ValueError."""
        with pytest.raises(ValueError, match="inverted bounds"):
            normalize_aoi([-74.0, 5.0, -75.0, 4.0])

    def test_wrong_length_sequence_raises(self):
        """A 3-element sequence is neither a bbox nor a point."""
        with pytest.raises(ValueError, match="4 values"):
            normalize_aoi([1.0, 2.0, 3.0])

    def test_unsupported_type_raises(self):
        """An integer is not a recognised aoi."""
        with pytest.raises(TypeError, match="unsupported aoi type"):
            normalize_aoi(42)


class TestEstimatePixelDims:
    """The bbox-to-pixel-grid pre-flight estimate and its guards."""

    def test_nominal_grid(self):
        """A small box at a coarse scale is a handful of pixels."""
        assert estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 90.0) == (124, 124)

    def test_non_positive_scale_raises(self):
        """A non-positive scale is rejected."""
        with pytest.raises(ValueError, match="scale_m must be positive"):
            estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 0.0)

    def test_inverted_longitude_raises(self):
        """east < west is rejected."""
        with pytest.raises(ValueError, match=r"east .* < west"):
            estimate_pixel_dims(31.1, 30.0, 31.0, 30.1, 90.0)

    def test_inverted_latitude_raises(self):
        """north < south is rejected."""
        with pytest.raises(ValueError, match=r"north .* < south"):
            estimate_pixel_dims(31.0, 30.1, 31.1, 30.0, 90.0)


# A non-rectangular polygon whose envelope is the EXPECTED bbox.
_TRIANGLE_WKT = "POLYGON ((-75 4, -74 4, -74.5 5, -75 4))"


class TestResolveAoiGeometry:
    """resolve_aoi returns the same bbox as normalize_aoi plus a polygon mask."""

    def test_bbox_list_has_no_geometry(self):
        """A plain bbox is rectangular, so no polygon mask is produced."""
        lat_lim, lon_lim, geom = resolve_aoi([-75.0, 4.0, -74.0, 5.0])
        assert (lat_lim, lon_lim) == EXPECTED, f"bbox mismatch: {lat_lim}, {lon_lim}"
        assert geom is None, f"a bbox should yield no mask, got {geom!r}"

    def test_point_with_buffer_has_no_geometry(self):
        """A buffered point is a square box, so no polygon mask is produced."""
        _, _, geom = resolve_aoi((-74.5, 4.5), buffer=0.5)
        assert geom is None, f"a point box should yield no mask, got {geom!r}"

    def test_wkt_polygon_yields_geodataframe_mask(self):
        """A WKT polygon yields its envelope bbox plus a GeoDataFrame mask."""
        pytest.importorskip("geopandas")
        lat_lim, lon_lim, geom = resolve_aoi(_TRIANGLE_WKT)
        assert (lat_lim, lon_lim) == EXPECTED, f"envelope mismatch: {lat_lim}"
        assert geom is not None and geom.crs.to_epsg() == 4326, f"bad mask: {geom!r}"

    def test_wkt_bbox_polygon_is_still_a_mask(self):
        """Even a rectangular WKT polygon is carried as a mask (it is a Polygon)."""
        pytest.importorskip("geopandas")
        _, _, geom = resolve_aoi("POLYGON ((-75 4, -74 4, -74 5, -75 5, -75 4))")
        assert geom is not None, "a Polygon geometry should always be a mask"

    def test_geojson_polygon_yields_mask(self):
        """A GeoJSON Polygon mapping yields a GeoDataFrame mask."""
        pytest.importorskip("geopandas")
        gj = {
            "type": "Polygon",
            "coordinates": [[[-75, 4], [-74, 4], [-74.5, 5], [-75, 4]]],
        }
        _, _, geom = resolve_aoi(gj)
        assert geom is not None, "a GeoJSON Polygon should yield a mask"

    def test_geojson_point_yields_no_mask(self):
        """A GeoJSON Point has no area, so no polygon mask is produced."""
        gj = {"type": "Point", "coordinates": [-74.5, 4.5], "bbox": [-75, 4, -74, 5]}
        _, _, geom = resolve_aoi(gj)
        assert geom is None, f"a Point should yield no mask, got {geom!r}"

    def test_geodataframe_polygon_yields_mask(self):
        """A GeoDataFrame of polygons is carried through as the mask."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        gdf = gpd.GeoDataFrame(
            geometry=[shapely.geometry.box(-75, 4, -74, 5)], crs="EPSG:4326"
        )
        _, _, geom = resolve_aoi(gdf)
        assert geom is not None and geom.crs.to_epsg() == 4326, f"bad mask: {geom!r}"

    def test_geoseries_polygon_wrapped_as_geodataframe(self):
        """A GeoSeries mask is wrapped into a single-CRS GeoDataFrame."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        gs = gpd.GeoSeries([shapely.geometry.box(-75, 4, -74, 5)], crs="EPSG:4326")
        _, _, geom = resolve_aoi(gs)
        assert isinstance(geom, gpd.GeoDataFrame), f"want GeoDataFrame, got {geom!r}"

    def test_projected_geodataframe_mask_is_reprojected(self):
        """A projected mask is reprojected to WGS84 before being carried."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        gdf = gpd.GeoDataFrame(
            geometry=[shapely.geometry.box(-75, 4, -74, 5)], crs="EPSG:4326"
        )
        _, _, geom = resolve_aoi(gdf.to_crs(3857))
        assert geom.crs.to_epsg() == 4326, f"mask not reprojected: {geom.crs}"


class _FakeCropDataset:
    """A stand-in pyramids Dataset that records how crop() was called."""

    def __init__(self):
        self.call = None

    def crop(self, mask=None, touch=True, *, bbox=None, epsg=None):
        self.call = {"mask": mask, "bbox": bbox, "epsg": epsg, "touch": touch}
        return self


class _ExtentStub:
    """A SpatialExtent-like object carrying only an optional geometry."""

    def __init__(self, geometry=None):
        self.geometry = geometry


class TestCropToAoi:
    """crop_to_aoi dispatches to a polygon mask or a bbox crop."""

    def test_no_geometry_crops_to_bbox(self):
        """With no geometry, the bbox path is taken with the given touch/epsg."""
        ds = _FakeCropDataset()
        crop_to_aoi(ds, _ExtentStub(), bbox=[-75, 4, -74, 5], touch=False)
        assert ds.call["bbox"] == [-75, 4, -74, 5], f"bad bbox: {ds.call}"
        assert ds.call["mask"] is None, f"mask should be unused: {ds.call}"
        assert ds.call["touch"] is False, f"touch not forwarded: {ds.call}"

    def test_geometry_crops_to_mask(self):
        """With a geometry, the mask path is taken and touch is forced True."""
        ds = _FakeCropDataset()
        sentinel = object()
        space = _ExtentStub(geometry=sentinel)
        crop_to_aoi(ds, space, bbox=[-75, 4, -74, 5], touch=False)
        assert ds.call["mask"] is sentinel, f"mask not used: {ds.call}"
        assert ds.call["bbox"] is None, f"bbox should be unused: {ds.call}"
        assert ds.call["touch"] is True, f"mask path keeps touching cells: {ds.call}"

    def test_plain_object_without_geometry_attr_uses_bbox(self):
        """A space object lacking a geometry attribute falls back to the bbox."""
        ds = _FakeCropDataset()
        crop_to_aoi(ds, object(), bbox=[-75, 4, -74, 5])
        assert ds.call["bbox"] == [-75, 4, -74, 5], f"bad bbox: {ds.call}"
