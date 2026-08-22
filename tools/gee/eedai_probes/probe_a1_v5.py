"""Gate A1 (v5) - does a prior native read poison later overview reads?

v4 refuted the encoding theory: under an explicit PIXEL_ENCODING every overview
level reproduced the native data exactly, AUTO included - yet v3, which differed
only in *not* passing PIXEL_ENCODING and in reading the full native window first
on the same handle, saw levels 0-2 come back corrupt.

Encoding is therefore not the variable. The remaining difference is read order and
per-handle state, so this tests that directly: read one overview level from a
handle that has done nothing else, and from a handle that has just read the native
window, and see whether the same level disagrees with itself.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
from osgeo import gdal

gdal.UseExceptions()

KEY = os.environ["GEE_SERVICE_KEY"]
BLOCK = 256
ASSET, BAND, LON, LAT = "USGS/SRTMGL1_003", "elevation", 86.925, 27.988


def _activate() -> None:
    """Point GDAL's EEDA auth at the service-account key."""
    with open(KEY, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def _open(encoding: str | None = None):
    """Open the probe asset, optionally pinning the pixel encoding."""
    opts = [f"BLOCK_SIZE={BLOCK}", f"BANDS={BAND}"]
    if encoding:
        opts.append(f"PIXEL_ENCODING={encoding}")
    return gdal.OpenEx(
        f"EEDAI:{ASSET}", gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR, open_options=opts
    )


def _blockwise(band, x0: int, y0: int, w: int, h: int) -> np.ndarray:
    """Read a window one block at a time."""
    out = np.full((h, w), np.nan, dtype="float64")
    for by in range(y0 // BLOCK * BLOCK, y0 + h, BLOCK):
        for bx in range(x0 // BLOCK * BLOCK, x0 + w, BLOCK):
            rx0, ry0 = max(bx, x0), max(by, y0)
            rx1, ry1 = min(bx + BLOCK, x0 + w), min(by + BLOCK, y0 + h)
            if rx1 <= rx0 or ry1 <= ry0:
                continue
            out[ry0 - y0 : ry1 - y0, rx0 - x0 : rx1 - x0] = band.ReadAsArray(
                rx0, ry0, rx1 - rx0, ry1 - ry0
            )
    return out


def _window(ds) -> tuple[int, int, int]:
    """Return the block-aligned native window centred on the probe location."""
    gt = ds.GetGeoTransform()
    side = BLOCK * 4
    px, py = int((LON - gt[0]) / gt[1]), int((LAT - gt[3]) / gt[5])
    return (px - side // 2) // BLOCK * BLOCK, (py - side // 2) // BLOCK * BLOCK, side


def _ov_window(ds, level: int, x0: int, y0: int, side: int):
    """Map the native window onto one overview level."""
    ov = ds.GetRasterBand(1).GetOverview(level)
    fx, fy = ds.RasterXSize / ov.XSize, ds.RasterYSize / ov.YSize
    return (
        ov,
        int(round(x0 / fx)),
        int(round(y0 / fy)),
        int(round(side / fx)),
        int(round(side / fy)),
    )


def _summary(arr: np.ndarray) -> str:
    """One-line range summary of a read."""
    return f"mean={np.nanmean(arr):8.1f} range=[{np.nanmin(arr):7.0f},{np.nanmax(arr):7.0f}]"


def main() -> None:
    """Read overview levels 0-2 with and without a preceding native read."""
    _activate()
    ref = _open("NPY")
    x0, y0, side = _window(ref)
    print(f"{ASSET} @ Everest  native window {x0},{y0} {side}x{side}\n")

    for level in (0, 1, 2):
        print(f"--- overview level {level} " + "-" * 52)

        # Case A: a handle that has read nothing else.
        dsa = _open()
        ov, ox0, oy0, ow, oh = _ov_window(dsa, level, x0, y0, side)
        try:
            cold = _blockwise(ov, ox0, oy0, ow, oh)
            print(f"  A cold handle, overview only : {_summary(cold)}")
        except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
            cold = None
            print(
                f"  A cold handle, overview only : RAISED {type(exc).__name__}: {str(exc)[:70]}"
            )

        # Case B: the same read, but after pulling the native window first.
        dsb = _open()
        _blockwise(dsb.GetRasterBand(1), x0, y0, side, side)
        ov, ox0, oy0, ow, oh = _ov_window(dsb, level, x0, y0, side)
        try:
            warm = _blockwise(ov, ox0, oy0, ow, oh)
            print(f"  B after a native read        : {_summary(warm)}")
        except Exception as exc:  # noqa: BLE001
            warm = None
            print(
                f"  B after a native read        : RAISED {type(exc).__name__}: {str(exc)[:70]}"
            )

        if cold is not None and warm is not None:
            same = np.allclose(
                np.nan_to_num(cold), np.nan_to_num(warm), rtol=0, atol=1e-6
            )
            print(f"  -> cold and warm agree: {same}")
        print()


if __name__ == "__main__":
    main()
