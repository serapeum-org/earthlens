"""Lock-in for the CHC crop: the bbox overlap guard (M2) and the outer window (H1)."""

from __future__ import annotations

import numpy as np
import pytest
from pyramids.dataset import Dataset, GeoReference

from earthlens.chc.backend import CHIRPS, _snap_bbox_outward

pytestmark = [pytest.mark.chc]


def _chirps_with_bbox(lat_lim: list[float], lon_lim: list[float]) -> CHIRPS:
    """Build a minimal CHIRPS backend pinned to a known bbox + date.

    Uses CHIRPS-2.0 global-daily so the legacy list-shape `variables`
    path is exercised; the date range is one day to keep the constructor
    fast.
    """
    return CHIRPS(
        variables=["precipitation"],
        temporal_resolution="daily",
        start="2020-01-01",
        end="2020-01-01",
        lat_lim=lat_lim,
        lon_lim=lon_lim,
    )


# A small fake raster covering lon [-180, 180] x lat [-50, 50] at 1-degree
# pixels: 100 rows x 360 cols. The geo-affine origin sits at (-180, 50)
# (top-left), pixel size 1 in both directions, no rotation.
_FAKE_GEO: list[float] = [-180.0, 1.0, 0.0, 50.0, 0.0, -1.0]


class TestSnapBboxOutward:
    """`_snap_bbox_outward` grows a bbox to the enclosing cell edges (H1)."""

    def test_aligned_bbox_is_unchanged(self):
        """A bbox already on cell edges snaps to itself."""
        assert _snap_bbox_outward((0.0, 0.0, 10.0, 10.0), _FAKE_GEO) == [
            0.0,
            0.0,
            10.0,
            10.0,
        ]

    def test_misaligned_bbox_grows_to_enclosing_cells(self):
        """Every edge cutting through a cell moves out to that cell's boundary."""
        assert _snap_bbox_outward((0.5, 0.5, 9.5, 9.5), _FAKE_GEO) == [
            0.0,
            0.0,
            10.0,
            10.0,
        ]

    def test_snapped_box_always_contains_the_request(self):
        """The snap only ever grows the box — never trims it."""
        west, south, east, north = 3.7, -4.2, 12.3, 8.9
        snapped = _snap_bbox_outward((west, south, east, north), _FAKE_GEO)
        assert snapped[0] <= west and snapped[1] <= south
        assert snapped[2] >= east and snapped[3] >= north


class TestClipAndNormaliseWindow:
    """`_clip_and_normalise` keeps covering the requested bbox (H1 regression).

    The crop must select the *outer* window: the hand-rolled slice this
    replaced floored west/north and ceiled east/south, so the output was
    always a superset of the request. A raw `crop_to_aoi(touch=False)` keeps
    only fully-inside cells and would silently shrink every misaligned
    request — which is the normal case on CHC's 0.05-degree grids.
    """

    @staticmethod
    def _granule() -> Dataset:
        """A 100x360 one-degree granule spanning the whole _FAKE_GEO extent."""
        return Dataset.from_array(
            arr=np.ones((100, 360), dtype=np.float32),
            no_data_value=None,
            geo_ref=GeoReference(geo=tuple(_FAKE_GEO), epsg=4326),
        )

    def test_aligned_bbox_window(self):
        """A cell-aligned request yields the exact 10x10 window at (0, 10)."""
        chirps = _chirps_with_bbox(lat_lim=[0.0, 10.0], lon_lim=[0.0, 10.0])
        out = chirps._clip_and_normalise(self._granule())
        assert (out.rows, out.columns) == (10, 10)
        assert out.geotransform[0] == pytest.approx(0.0)
        assert out.geotransform[3] == pytest.approx(10.0)

    def test_misaligned_bbox_still_covers_the_request(self):
        """A misaligned request keeps its partially-covered edge cells.

        Regression: without the outward snap this returned an 8x8 window
        spanning 1..9, so the output no longer covered the 0.5..9.5 asked for.
        """
        chirps = _chirps_with_bbox(lat_lim=[0.5, 9.5], lon_lim=[0.5, 9.5])
        out = chirps._clip_and_normalise(self._granule())
        assert (out.rows, out.columns) == (10, 10)
        west, south, east, north = out.bbox
        assert west <= 0.5 and south <= 0.5
        assert east >= 9.5 and north >= 9.5

    def test_realistic_005_degree_grid_covers_the_request(self):
        """A 0.05° CHIRPS-pitch grid (float, non-binary-exact edges) still covers.

        The 1° fixture lands every snapped edge on an exactly-representable
        coordinate, hiding whether the snap holds when a cell edge like
        `-175.05` is not binary-exact — which is the real CHIRPS case. Build a
        0.05° granule and request an off-grid box; the crop must still be a
        superset of it.
        """
        pitch = 0.05
        geo = (-180.0, pitch, 0.0, 40.0, 0.0, -pitch)  # 1600 rows x 7200 cols
        granule = Dataset.from_array(
            arr=np.ones((1600, 7200), dtype=np.float32),
            no_data_value=None,
            geo_ref=GeoReference(geo=geo, epsg=4326),
        )
        chirps = _chirps_with_bbox(lat_lim=[3.72, 12.28], lon_lim=[-9.03, -0.07])
        out = chirps._clip_and_normalise(granule)
        west, south, east, north = out.bbox
        assert west <= -9.03 and south <= 3.72
        assert east >= -0.07 and north >= 12.28
        # Snapped to whole 0.05° cells: no partially-covered edge cell dropped,
        # and the -9999 sentinel is declared (no_data_value is per-band).
        assert out.no_data_value[0] == pytest.approx(-9999.0)


class TestClipToBboxOverlap:
    """`_check_bbox_overlaps` refuses non-overlapping bboxes (M2)."""

    def test_overlapping_bbox_does_not_raise(self):
        """A normal bbox inside the raster extent passes the overlap guard."""
        chirps = _chirps_with_bbox(lat_lim=[0.0, 10.0], lon_lim=[0.0, 10.0])
        data = np.zeros((100, 360), dtype=np.float32)
        chirps._check_bbox_overlaps(data, _FAKE_GEO)  # no exception

    def test_bbox_entirely_north_of_raster_raises(self):
        """A bbox north of the raster's extent raises ValueError naming both extents."""
        chirps = _chirps_with_bbox(lat_lim=[60.0, 70.0], lon_lim=[0.0, 10.0])
        data = np.zeros((100, 360), dtype=np.float32)
        with pytest.raises(ValueError, match=r"does not overlap") as exc:
            chirps._check_bbox_overlaps(data, _FAKE_GEO)
        message = str(exc.value)
        assert "60.0" in message and "70.0" in message
        assert "raster" in message.lower()

    def test_bbox_entirely_south_of_raster_raises(self):
        """A bbox south of the raster's extent raises ValueError."""
        chirps = _chirps_with_bbox(lat_lim=[-80.0, -70.0], lon_lim=[0.0, 10.0])
        data = np.zeros((100, 360), dtype=np.float32)
        with pytest.raises(ValueError, match=r"does not overlap"):
            chirps._check_bbox_overlaps(data, _FAKE_GEO)

    def test_bbox_east_of_narrow_raster_raises(self):
        """A bbox east of a *narrow* raster (lon [-180, -100]) raises ValueError."""
        # SpatialExtent enforces user lon <= 180, so we can't put the bbox
        # east of a global raster — instead, make the raster narrow.
        chirps = _chirps_with_bbox(lat_lim=[0.0, 10.0], lon_lim=[0.0, 10.0])
        narrow_geo = [-180.0, 1.0, 0.0, 50.0, 0.0, -1.0]
        data = np.zeros((100, 80), dtype=np.float32)  # cols=80 -> east edge at lon=-100
        with pytest.raises(ValueError, match=r"does not overlap"):
            chirps._check_bbox_overlaps(data, narrow_geo)

    def test_message_names_both_bbox_and_raster_extent(self):
        """The error message must surface the user bbox AND the raster extent."""
        chirps = _chirps_with_bbox(lat_lim=[60.0, 70.0], lon_lim=[0.0, 10.0])
        data = np.zeros((100, 360), dtype=np.float32)
        with pytest.raises(ValueError) as exc:
            chirps._check_bbox_overlaps(data, _FAKE_GEO)
        message = str(exc.value)
        # User bbox
        assert "60.0" in message and "70.0" in message
        # Raster extent (top-edge at 50, bottom at -50, west -180, east 180)
        assert "50" in message and "-50" in message
        assert "-180" in message
