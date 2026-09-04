from __future__ import annotations

import numpy as np
import pytest
from pyramids.dataset import Dataset, GeoReference

from earthlens.base.spatial import (
    ensure_no_data,
    vsicurl_config,
    widen_degenerate_bbox,
    windowed_bbox_crop,
)


def _dataset(no_data_value):
    """Return a small in-memory 4326 Dataset with the given no-data value."""
    arr = np.arange(9, dtype="float32").reshape(3, 3)
    return Dataset.from_array(
        arr,
        no_data_value=no_data_value,
        geo_ref=GeoReference(top_left_corner=(0, 0), cell_size=1.0, epsg=4326),
    )


class TestWidenDegenerateBbox:
    """`widen_degenerate_bbox` pushes a collapsed edge out by one source pixel."""

    def test_point_widened_on_both_axes(self):
        """A point AOI is widened by one pixel on both axes."""
        assert widen_degenerate_bbox([5.0, -5.0, 5.0, -5.0], 1.0, -1.0) == [
            5.0,
            -5.0,
            6.0,
            -4.0,
        ]

    def test_positive_box_unchanged(self):
        """A box already positive on both axes is returned unchanged."""
        assert widen_degenerate_bbox([4.8, 51.8, 5.0, 52.0], 0.1, -0.1) == [
            4.8,
            51.8,
            5.0,
            52.0,
        ]

    def test_only_collapsed_axis_widened(self):
        """Only the zero-width axis is widened; the positive axis is left alone."""
        assert widen_degenerate_bbox([5.0, 51.0, 5.0, 52.0], 2.0, -2.0) == [
            5.0,
            51.0,
            7.0,
            52.0,
        ]

    def test_pixel_size_sign_ignored(self):
        """The absolute pixel size is used, so a positive pixel_height still widens."""
        assert widen_degenerate_bbox([0.0, 0.0, 0.0, 0.0], -3.0, 3.0) == [
            0.0,
            0.0,
            3.0,
            3.0,
        ]


class TestEnsureNoData:
    """`ensure_no_data` stamps a fallback only when the dataset declares none."""

    def test_stamps_default_when_absent(self):
        """A dataset with no no-data gets the default stamped (pixels unchanged)."""
        ds = _dataset(no_data_value=None)
        before = ds.read_array().tolist()
        result = ensure_no_data(ds, -9999.0)
        assert result.no_data_value[0] == -9999.0, result.no_data_value
        assert result.read_array().tolist() == before, "pixels must be untouched"

    def test_keeps_existing_no_data(self):
        """A dataset that already declares a no-data keeps it (no override)."""
        ds = _dataset(no_data_value=-32768.0)
        assert ensure_no_data(ds, -9999.0).no_data_value[0] == -32768.0

    def test_returns_same_object(self):
        """The dataset is returned for chaining."""
        ds = _dataset(no_data_value=-1.0)
        assert ensure_no_data(ds, -9999.0) is ds


class TestVsicurlConfig:
    """`vsicurl_config` builds a CloudConfig carrying the /vsicurl tuning."""

    def test_carries_tuning_and_retry_budget(self):
        """The config enables vsicurl_tuning plus the retry / timeout budget."""
        cfg = vsicurl_config()
        assert cfg.vsicurl_tuning is True, "readdir/HTTP2 fast-read tuning enabled"
        assert cfg.http_max_retry == 3, cfg.http_max_retry
        assert cfg.http_retry_delay == 2.0, cfg.http_retry_delay
        assert cfg.http_timeout == 30, cfg.http_timeout

    def test_is_a_context_manager(self):
        """It is usable as a `with` block around a read."""
        assert hasattr(vsicurl_config(), "__enter__")
        assert hasattr(vsicurl_config(), "__exit__")


class TestWindowedBboxCrop:
    """`windowed_bbox_crop` reads the window, keeping an all-no-data AOI."""

    def _raster(self, all_nodata: bool):
        """A 10x10 4326 raster; optionally entirely no-data."""
        arr = (
            np.full((10, 10), -9999.0, "float32")
            if all_nodata
            else np.arange(100, dtype="float32").reshape(10, 10)
        )
        return Dataset.from_array(
            arr,
            no_data_value=-9999.0,
            geo_ref=GeoReference(top_left_corner=(0, 0), cell_size=1.0, epsg=4326),
        )

    def test_data_present_window_is_cropped(self):
        """A 3-deg box on a 1-deg grid reads exactly the 3x3 AOI window."""
        crop = windowed_bbox_crop(
            self._raster(all_nodata=False), [2.0, -6.0, 5.0, -3.0]
        )
        assert crop.rows == 3, crop.rows
        assert crop.columns == 3, crop.columns

    def test_all_nodata_window_does_not_raise(self):
        """An entirely-no-data window returns an all-no-data crop, not a raise."""
        crop = windowed_bbox_crop(self._raster(all_nodata=True), [2.0, -6.0, 5.0, -3.0])
        assert bool((crop.read_array() == -9999.0).all()), "all-no-data crop returned"


pytestmark = pytest.mark.unit
