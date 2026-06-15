"""Unit tests for :meth:`ECMWF._mask_netcdf_to_geometry`.

A polygon `aoi=` trims the bbox-cropped NetCDF to the exact shape via
`pyramids.NetCDF`; a bbox / point `aoi=` is a no-op; a genuine crop error
propagates; and a temp-write failure removes the partial temp file. All
paths use a fake `pyramids.netcdf.NetCDF`.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


class _FakeCube:
    """A stand-in for `pyramids.NetCDF` that records crop / close calls."""

    def __init__(self, path, *, crop_raises=None, to_file_raises=None):
        self.path = path
        self.closed = False
        self._crop_raises = crop_raises
        self._to_file_raises = to_file_raises

    def crop(self, mask=None, touch=True):
        if self._crop_raises is not None:
            raise self._crop_raises
        return self

    def to_file(self, path):
        from pathlib import Path

        Path(path).write_text("masked")  # partial write lands before any failure
        if self._to_file_raises is not None:
            raise self._to_file_raises

    def close(self):
        self.closed = True


def _patch_netcdf(monkeypatch, *, crop_raises=None, to_file_raises=None):
    """Install a fake `pyramids.netcdf.NetCDF.read_file` and return the box."""
    import pyramids.netcdf as netcdf_module

    box = {}

    class _FakeNetCDF:
        @staticmethod
        def read_file(path):
            cube = _FakeCube(
                path, crop_raises=crop_raises, to_file_raises=to_file_raises
            )
            box["cube"] = cube
            return cube

    monkeypatch.setattr(netcdf_module, "NetCDF", _FakeNetCDF)
    return box


def _polygon_space(space):
    """Return a copy of `space` carrying a tiny polygon mask."""
    gpd = pytest.importorskip("geopandas")
    shapely = pytest.importorskip("shapely")
    poly = shapely.geometry.box(-75.65, 4.19, -74.73, 4.64)
    gdf = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
    return space.model_copy(update={"geometry": gdf})


class TestMaskNetcdfToGeometry:
    """The polygon mask over a written CDS NetCDF."""

    def test_no_geometry_is_a_noop(self, ecmwf_stub, monkeypatch, tmp_path):
        """With no polygon mask the file is left exactly as written."""
        box = _patch_netcdf(monkeypatch)
        target = tmp_path / "t2m.nc"
        target.write_text("original")
        ecmwf_stub._mask_netcdf_to_geometry(target)
        assert target.read_text() == "original", "file must be untouched"
        assert "cube" not in box, "NetCDF.read_file must not be called"

    def test_polygon_mask_replaces_file(self, ecmwf_stub, monkeypatch, tmp_path):
        """A polygon mask crops the cube and atomically replaces the NetCDF."""
        box = _patch_netcdf(monkeypatch)
        ecmwf_stub.space = _polygon_space(ecmwf_stub.space)
        target = tmp_path / "t2m.nc"
        target.write_text("original")
        ecmwf_stub._mask_netcdf_to_geometry(target)
        assert target.read_text() == "masked", "masked cube should replace the file"
        assert box["cube"].closed, "the source cube must be closed"
        assert not (tmp_path / "t2m.masked.nc").exists(), "temp file must be replaced"

    def test_crop_error_propagates(self, ecmwf_stub, monkeypatch, tmp_path):
        """A genuine pyramids crop error propagates (no longer swallowed)."""
        box = _patch_netcdf(monkeypatch, crop_raises=ValueError("polygon misses data"))
        ecmwf_stub.space = _polygon_space(ecmwf_stub.space)
        target = tmp_path / "t2m.nc"
        target.write_text("original")
        with pytest.raises(ValueError, match="polygon misses data"):
            ecmwf_stub._mask_netcdf_to_geometry(target)
        assert target.read_text() == "original", "the source NetCDF is left intact"
        assert box["cube"].closed, "the source cube must be closed even on error"

    def test_temp_file_cleaned_up_on_write_failure(
        self, ecmwf_stub, monkeypatch, tmp_path
    ):
        """A failure during the temp write removes the partial temp file."""
        _patch_netcdf(monkeypatch, to_file_raises=OSError("disk full"))
        ecmwf_stub.space = _polygon_space(ecmwf_stub.space)
        target = tmp_path / "t2m.nc"
        target.write_text("original")
        with pytest.raises(OSError, match="disk full"):
            ecmwf_stub._mask_netcdf_to_geometry(target)
        assert target.read_text() == "original", "the original NetCDF must be retained"
        assert not (tmp_path / "t2m.masked.nc").exists(), "partial temp must be removed"
