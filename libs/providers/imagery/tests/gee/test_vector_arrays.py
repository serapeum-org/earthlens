"""Tests for the ee -> numpy / ee -> NetCDF pyramids bridges."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.vector import arrays as arrays_module
from earthlens.gee.vector.arrays import to_netcdf, to_numpy

_GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


class _RecordingCollection:
    """Stands in for a `DatasetCollection`, recording the NetCDF path."""

    def __init__(self):
        self.path = None

    def to_netcdf(self, path):
        self.path = path


class TestToNumpy:
    """Reading a downloaded Dataset into a numpy array."""

    def test_reads_the_band_array(self):
        """`to_numpy` returns the band's values unchanged."""
        dataset = Dataset.from_array(
            arr=np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32"),
            geo_ref=_GEO,
            no_data_value=None,
        )
        assert to_numpy(dataset).tolist() == [[1.0, 2.0], [3.0, 4.0]]


class TestToNetcdf:
    """Writing a DatasetCollection to NetCDF through pyramids."""

    def test_delegates_and_returns_the_path(self, tmp_path):
        """`to_netcdf` calls the collection and returns the path as a `Path`."""
        collection = _RecordingCollection()
        target = tmp_path / "stack.nc"
        result = to_netcdf(collection, target)
        assert collection.path == target
        assert result == Path(target)


def test_no_banned_io_libraries_are_imported():
    """The bridge stays on pyramids: no xee / direct xarray."""
    text = open(arrays_module.__file__, encoding="utf-8").read()
    for banned in ("import xee", "import xarray"):
        assert banned not in text
