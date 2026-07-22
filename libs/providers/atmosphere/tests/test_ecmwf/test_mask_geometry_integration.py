"""Integration test for ECMWF polygon masking through real pyramids.

Exercises :meth:`ECMWF._mask_netcdf_to_geometry` end-to-end against an
ERA5-like NetCDF that carries a non-spatial `expver` string aux variable —
the cube shape that used to trip pyramids' crop (serapeum-org/pyramids#513,
fixed in pyramids 0.34.0 / #514). Unlike the fake-backed unit tests, this
runs the real `pyramids.NetCDF` read / crop / write path, so it guards both
the aux-var crop and the handle release before the atomic replace (on
Windows the GDAL handle must be dropped or `os.replace` raises). The cube is
generated on demand (no committed binaries, no network, no CDS credentials).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


def _write_era5_like_cube(path):
    """Write a small ERA5-like NetCDF with a non-spatial `expver` aux var."""
    xr = pytest.importorskip("xarray")
    import numpy as np
    import pandas as pd

    times = pd.date_range("2022-01-01", periods=3, freq="D")
    lat = np.linspace(5.0, 4.0, 8)  # decreasing, like ERA5
    lon = np.linspace(-75.5, -74.5, 8)
    t2m = (np.arange(3 * 8 * 8, dtype="float32").reshape(3, 8, 8) / 100.0) + 280.0
    ds = xr.Dataset(
        {
            "t2m": (("valid_time", "latitude", "longitude"), t2m),
            "expver": (
                ("valid_time",),
                np.array(["0001", "0001", "0005"], dtype="<U4"),
            ),
        },
        coords={"valid_time": times, "latitude": lat, "longitude": lon},
    )
    ds["latitude"].attrs["units"] = "degrees_north"
    ds["longitude"].attrs["units"] = "degrees_east"
    ds["t2m"].attrs["units"] = "K"
    ds.to_netcdf(path)
    ds.close()


def _polygon_space(space):
    """Return a copy of `space` carrying a polygon mask over the cube extent."""
    gpd = pytest.importorskip("geopandas")
    shapely = pytest.importorskip("shapely")
    poly = shapely.geometry.box(-75.65, 4.19, -74.73, 4.64)
    gdf = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
    return space.model_copy(update={"geometry": gdf})


def test_polygon_mask_real_pyramids_carries_aux_var(ecmwf_stub, tmp_path):
    """Real pyramids crops the expver-carrying cube and replaces the file."""
    target = tmp_path / "t2m.nc"
    _write_era5_like_cube(target)
    ecmwf_stub.space = _polygon_space(ecmwf_stub.space)
    ecmwf_stub._mask_netcdf_to_geometry(target)  # must NOT raise (pyramids#514)
    assert target.exists() and target.stat().st_size > 0, (
        "masked NetCDF must be written"
    )
    assert not (tmp_path / "t2m.masked.nc").exists(), "temp file must be replaced"
