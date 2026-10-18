"""Tests for ZonalHistogram, the per-zone categorical class-area sampler."""

from __future__ import annotations

from types import SimpleNamespace

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, Polygon

from earthlens.gee.sampling import histogram as histogram_module
from earthlens.gee.sampling.histogram import ZonalHistogram

_SQUARE = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
_SQUARE_2 = Polygon([(2, 2), (3, 2), (3, 3), (2, 3)])


class _FakeReducer:
    """Records combine/group chaining in place of an ee.Reducer."""

    def __init__(self, name: str):
        self.name = name
        self.ops: list = []

    def combine(self, reducer2, sharedInputs):  # noqa: N803 - ee kwarg
        self.ops.append(("combine", reducer2.name, sharedInputs))
        return self

    def group(self, groupField, groupName):  # noqa: N803 - ee kwargs
        self.ops.append(("group", groupField, groupName))
        return self


class _FakeStacked:
    """Records the reduceRegions call in place of the stacked ee.Image."""

    def __init__(self):
        self.call: dict | None = None

    def reduceRegions(self, collection, reducer, scale, tileScale):  # noqa: N802,N803
        self.call = {
            "collection": collection,
            "reducer": reducer,
            "scale": scale,
            "tileScale": tileScale,
        }
        return ("fc", scale)


class _FakePixelArea:
    """Stands in for `ee.Image.pixelArea()`; `addBands` yields the stacked image."""

    def __init__(self, stacked: _FakeStacked):
        self._stacked = stacked
        self.added = None

    def addBands(self, other):  # noqa: N802 - ee method name
        self.added = other
        return self._stacked


class _FakeImage:
    """The categorical input image; records the selected class band."""

    def __init__(self):
        self.selected = None

    def select(self, bands):
        self.selected = bands
        return ("classband", tuple(bands))


class _FakeConverter:
    """Returns a sentinel for `to_ee` in place of GeometryConverter."""

    def to_ee(self, gdf):
        return ("zones", len(gdf))


@pytest.fixture
def fake_ee(monkeypatch):
    """Patch the module's `ee` and `GeometryConverter` with recording fakes."""
    stacked = _FakeStacked()
    fake = SimpleNamespace(
        Image=SimpleNamespace(pixelArea=lambda: _FakePixelArea(stacked)),
        Reducer=SimpleNamespace(
            sum=lambda: _FakeReducer("sum"), count=lambda: _FakeReducer("count")
        ),
    )
    monkeypatch.setattr(histogram_module, "ee", fake)
    monkeypatch.setattr(histogram_module, "GeometryConverter", _FakeConverter)
    return stacked


def _zones(*polygons):
    """A GeoDataFrame of the given polygon zones."""
    return gpd.GeoDataFrame({"geometry": list(polygons)}, crs=4326)


class TestConstruction:
    """The frozen configuration a ZonalHistogram carries."""

    def test_defaults(self):
        """It defaults to raw counts, all-code labels, and tile_scale 1."""
        hist = ZonalHistogram(scale_m=30, class_band="lc")
        assert (hist.normalize, hist.class_labels, hist.tile_scale) == (
            "count",
            None,
            1.0,
        )

    @pytest.mark.parametrize("bad", [0.0, 20.0])
    def test_tile_scale_is_range_checked(self, bad):
        """A tile_scale outside [0.1, 16] is refused at construction."""
        with pytest.raises(ValidationError, match="tile_scale"):
            ZonalHistogram(scale_m=30, class_band="lc", tile_scale=bad)


class TestSample:
    """The server-side grouped reduction."""

    def test_it_groups_pixelarea_and_count_by_class(self, fake_ee):
        """sample builds the grouped sum+count reducer and passes the knobs."""
        hist = ZonalHistogram(scale_m=30, class_band="lc", tile_scale=2.0)
        result = hist.sample(_FakeImage(), _zones(_SQUARE))
        assert result == ("fc", 30)
        reducer = fake_ee.call["reducer"]
        assert ("combine", "count", True) in reducer.ops
        assert ("group", 1, "class") in reducer.ops
        assert fake_ee.call["tileScale"] == 2.0

    def test_a_non_polygon_zone_is_refused(self, fake_ee):
        """A point geometry is not a zone."""
        hist = ZonalHistogram(scale_m=30, class_band="lc")
        points = gpd.GeoDataFrame({"geometry": [Point(0, 0)]}, crs=4326)
        image = _FakeImage()
        with pytest.raises(ValueError, match="Point"):
            hist.sample(image, points)


class TestSampleToGdf:
    """Widening the per-zone groups into one column per class."""

    @pytest.fixture
    def downloaded(self, monkeypatch):
        """Patch FeatureDownloader.to_gdf to return a two-zone groups table."""
        from earthlens.gee.vector import download as download_module

        rows = gpd.GeoDataFrame(
            {
                "groups": [
                    [
                        {"class": 1, "sum": 100.0, "count": 10},
                        {"class": 2, "sum": 50.0, "count": 5},
                    ],
                    [],
                ],
                "geometry": [_SQUARE, _SQUARE_2],
            },
            crs=4326,
        )

        class _FakeDownloader:
            def to_gdf(self, fc, crs):
                return rows

        monkeypatch.setattr(download_module, "FeatureDownloader", _FakeDownloader)

    def test_count_columns(self, fake_ee, downloaded):
        """count gives raw pixel counts, 0 where a class is absent from a zone."""
        hist = ZonalHistogram(scale_m=30, class_band="lc", normalize="count")
        out = hist.sample_to_gdf(_FakeImage(), _zones(_SQUARE, _SQUARE_2))
        assert out["1"].tolist() == [10.0, 0.0]
        assert out["2"].tolist() == [5.0, 0.0]
        assert "groups" not in out.columns

    def test_fraction_columns_sum_to_one_per_zone(self, fake_ee, downloaded):
        """fraction normalises each zone's counts to sum to 1."""
        hist = ZonalHistogram(scale_m=30, class_band="lc", normalize="fraction")
        out = hist.sample_to_gdf(_FakeImage(), _zones(_SQUARE, _SQUARE_2))
        assert out["1"].iloc[0] == pytest.approx(10 / 15)
        assert out["2"].iloc[0] == pytest.approx(5 / 15)

    def test_area_columns_use_the_summed_pixel_area(self, fake_ee, downloaded):
        """area reports the summed pixelArea, not the count."""
        hist = ZonalHistogram(scale_m=30, class_band="lc", normalize="area")
        out = hist.sample_to_gdf(_FakeImage(), _zones(_SQUARE, _SQUARE_2))
        assert out["1"].iloc[0] == pytest.approx(100.0)
        assert out["2"].iloc[0] == pytest.approx(50.0)

    def test_class_labels_name_the_columns(self, fake_ee, downloaded):
        """A class_labels mapping renames the columns; unmapped codes stay raw."""
        hist = ZonalHistogram(scale_m=30, class_band="lc", class_labels={1: "forest"})
        out = hist.sample_to_gdf(_FakeImage(), _zones(_SQUARE, _SQUARE_2))
        assert "forest" in out.columns
        assert "2" in out.columns
