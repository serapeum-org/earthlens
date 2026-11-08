"""A polygon aoi= masks CHIRPS output to the exact shape, not just the bbox."""

from __future__ import annotations

import numpy as np
import pytest

from earthlens.chc.backend import CHIRPS

pytestmark = [pytest.mark.chc]

# A 10x10 grid of ones over lon [0, 10] x lat [0, 10] at 1-degree pixels.
# Top-left origin (0, 10), pixel size 1, no rotation.
_GEO: list[float] = [0.0, 1.0, 0.0, 10.0, 0.0, -1.0]


def _write_ones_tif(path, dataset):
    """Write a 10x10 all-ones GeoTIFF over [0,10]x[0,10] to `path`."""
    from pyramids.dataset import GeoReference

    arr = np.ones((10, 10), dtype="float32")
    dataset.from_array(
        arr, no_data_value=-9999.0, geo_ref=GeoReference(geo=_GEO, epsg=4326)
    ).to_file(str(path))


def _polygon_chirps(tmp_path, geometry):
    """Build a CHIRPS backend with a polygon aoi= over [0,10]x[0,10]."""
    return CHIRPS(
        variables=["precipitation"],
        temporal_resolution="daily",
        start="2020-01-01",
        end="2020-01-01",
        path=str(tmp_path),
        aoi=geometry,
    )


class TestChcPolygonMask:
    """_clip_raster_in_place masks to the polygon when one is attached."""

    def test_polygon_aoi_masks_outside_cells(self, tmp_path):
        """A triangular aoi leaves its interior intact and -9999s the rest."""
        from pyramids.dataset import Dataset

        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        triangle = shapely.geometry.Polygon([(0, 0), (10, 0), (0, 10)])
        gdf = gpd.GeoDataFrame(geometry=[triangle], crs="EPSG:4326")
        backend = _polygon_chirps(tmp_path, gdf)
        assert backend.space.geometry is not None, "polygon aoi should attach a mask"

        tif = tmp_path / "ones.tif"
        _write_ones_tif(tif, Dataset)
        backend._clip_raster_in_place(tif)

        out = Dataset.read_file(str(tif)).read_array().astype(float)
        masked = int(np.sum(out == -9999.0))
        kept = int(np.sum(out == 1.0))
        assert masked > 0, "the polygon's outside should be masked to -9999"
        assert kept > 0, "the polygon's interior should be preserved"
        assert kept + masked == out.size, "every cell is either kept or masked"

    def test_bbox_aoi_keeps_all_cells(self, tmp_path):
        """A bbox aoi attaches no mask, so every in-bbox cell is preserved."""
        from pyramids.dataset import Dataset

        backend = CHIRPS(
            variables=["precipitation"],
            temporal_resolution="daily",
            start="2020-01-01",
            end="2020-01-01",
            path=str(tmp_path),
            aoi=[0.0, 0.0, 10.0, 10.0],
        )
        assert backend.space.geometry is None, "a bbox aoi attaches no mask"

        tif = tmp_path / "ones.tif"
        _write_ones_tif(tif, Dataset)
        backend._clip_raster_in_place(tif)

        out = Dataset.read_file(str(tif)).read_array().astype(float)
        assert int(np.sum(out == -9999.0)) == 0, "a bbox aoi masks nothing"
