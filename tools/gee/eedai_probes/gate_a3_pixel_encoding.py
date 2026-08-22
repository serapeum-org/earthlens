"""Gate A3 - which PIXEL_ENCODING is fastest, on the dtypes that have a choice?

The A1 work already showed `AUTO`, `NPY` and `GEO_TIFF` return identical correct
pixels on an Int16 asset, so correctness equivalence is settled there. What is
still unmeasured is *throughput*, and whether the picture changes for the two
cases where the encoding actually has room to differ: a float band (which the
byte-oriented codecs cannot carry at all) and a multi-band read (where a codec
can pack bands together).

Candidate assets are probed for dtype and band count first, because choosing them
from memory is how a benchmark ends up measuring the wrong thing. `PNG`/`JPEG`
are offered only to Byte bands, where the driver documents them as valid.
"""

from __future__ import annotations

import os
import time

import numpy as np
import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
from _common import BLOCK, activate, open_eedai
from osgeo import gdal

gdal.UseExceptions()

KEY = os.environ["GEE_SERVICE_KEY"]
SIDE = 1024
REPEATS = 2

CANDIDATES = [
    ("USGS/SRTMGL1_003", None, 86.925, 27.988),
    ("CSP/ERGo/1_0/Global/SRTM_topoDiversity", None, 86.925, 27.988),
    ("NASA/NASADEM_HGT/001", None, 86.925, 27.988),
    ("JRC/GSW1_4/GlobalSurfaceWater", None, 31.23, 30.05),
]
BYTE_TYPES = {"Byte", "Int8"}


def _read(ds, x0: int, y0: int, side: int) -> np.ndarray:
    """Read every band of a window, one block per call."""
    planes = []
    for i in range(1, ds.RasterCount + 1):
        band = ds.GetRasterBand(i)
        out = np.full((side, side), np.nan, dtype="float64")
        for by in range(y0, y0 + side, BLOCK):
            for bx in range(x0, x0 + side, BLOCK):
                w, h = min(BLOCK, x0 + side - bx), min(BLOCK, y0 + side - by)
                out[by - y0 : by - y0 + h, bx - x0 : bx - x0 + w] = band.ReadAsArray(
                    bx, by, w, h
                )
        planes.append(out)
    return np.stack(planes)


def _window(ds, lon: float, lat: float) -> tuple[int, int]:
    """Block-aligned window centred on a lon/lat."""
    gt = ds.GetGeoTransform()
    px, py = int((lon - gt[0]) / gt[1]), int((lat - gt[3]) / gt[5])
    return (px - SIDE // 2) // BLOCK * BLOCK, (py - SIDE // 2) // BLOCK * BLOCK


def main() -> None:
    """Profile each asset under every encoding its dtype allows."""
    activate()
    print("probing candidates for dtype and band count...\n")
    profiles = []
    for asset, bands, lon, lat in CANDIDATES:
        try:
            ds = open_eedai(asset, bands=bands, block=BLOCK)
        except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
            print(f"  {asset:42s} UNAVAILABLE: {type(exc).__name__}")
            continue
        types = {
            gdal.GetDataTypeName(ds.GetRasterBand(i).DataType)
            for i in range(1, ds.RasterCount + 1)
        }
        print(f"  {asset:42s} bands={ds.RasterCount:2d}  dtypes={sorted(types)}")
        profiles.append((asset, bands, lon, lat, ds.RasterCount, types))

    for asset, bands, lon, lat, count, types in profiles:
        encodings = ["AUTO", "NPY", "GEO_TIFF"]
        if types <= BYTE_TYPES:
            encodings += ["PNG", "AUTO_JPEG_PNG"]
        print(
            f"\n{'=' * 76}\n{asset}   bands={count}  dtypes={sorted(types)}\n{'=' * 76}"
        )
        ds0 = open_eedai(asset, bands=bands, block=BLOCK, encoding="NPY")
        x0, y0 = _window(ds0, lon, lat)
        reference = None
        print(f"  {'encoding':>14} {'best secs':>10} {'pixels':>34}")
        print("  " + "-" * 62)
        for enc in encodings:
            times, arr = [], None
            failure = None
            for _ in range(REPEATS):
                try:
                    ds = open_eedai(asset, bands=bands, block=BLOCK, encoding=enc)
                    started = time.time()
                    arr = _read(ds, x0, y0, SIDE)
                    times.append(time.time() - started)
                except Exception as exc:  # noqa: BLE001
                    failure = f"{type(exc).__name__}: {str(exc)[:40]}"
                    break
            if failure or arr is None:
                print(f"  {enc:>14} {'-':>10}   {failure or 'no data'}")
                continue
            if reference is None:
                reference = arr
                verdict = "reference"
            else:
                same = bool(
                    np.allclose(
                        np.nan_to_num(arr), np.nan_to_num(reference), rtol=0, atol=1e-6
                    )
                )
                verdict = "identical" if same else "DIFFERS from NPY"
            print(
                f"  {enc:>14} {min(times):>10.2f}   {verdict:>18}  "
                f"[{np.nanmin(arr):.1f},{np.nanmax(arr):.1f}]"
            )


if __name__ == "__main__":
    main()
