"""Gate A1 (v7) - what actually differed between the corrupt and clean runs.

v6's native column was a defect in the probe, not in GDAL: the Dataset was
created inside a lambda and collected before the band was read, so every trial
raised. Fixed here by holding the handle for the life of the read.

That leaves one uncontrolled difference between v3 (overview levels 0-2 corrupt)
and v5 (the same levels exact): v3 issued a large multi-block native RasterIO on
the handle before touching the overviews, and v5 never did. This tests that
sequence explicitly, and re-checks native repeatability with the lifetime bug
fixed.
"""

from __future__ import annotations

import json
import os

import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

KEY = os.environ["GEE_SERVICE_KEY"]
BLOCK = 256
ASSET, BAND, LON, LAT = "USGS/SRTMGL1_003", "elevation", 86.925, 27.988
PLAUSIBLE = (-500.0, 9000.0)


def _activate() -> None:
    """Point GDAL's EEDA auth at the service-account key."""
    with open(KEY, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def _open():
    """Open the probe asset exactly as pyramids-eo does."""
    return gdal.OpenEx(
        f"EEDAI:{ASSET}",
        gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR,
        open_options=[f"BLOCK_SIZE={BLOCK}", f"BANDS={BAND}"],
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


def _ov_read(ds, x0: int, y0: int, side: int, level: int = 0) -> np.ndarray:
    """Read overview `level` over the ground area of the native window."""
    ov = ds.GetRasterBand(1).GetOverview(level)
    fx, fy = ds.RasterXSize / ov.XSize, ds.RasterYSize / ov.YSize
    return _blockwise(ov, int(round(x0 / fx)), int(round(y0 / fy)),
                      int(round(side / fx)), int(round(side / fy)))


def _verdict(arr: np.ndarray, ref: np.ndarray) -> str:
    """Describe a read against the reference and physical plausibility."""
    finite = arr[np.isfinite(arr)]
    bad = int(((finite < PLAUSIBLE[0]) | (finite > PLAUSIBLE[1])).sum())
    same = np.allclose(np.nan_to_num(arr), np.nan_to_num(ref), rtol=0, atol=1e-6)
    return (f"{'CLEAN' if same and not bad else 'CORRUPT'}  match={same} "
            f"implausible={bad}  range=[{np.nanmin(arr):.0f},{np.nanmax(arr):.0f}]")


def main() -> None:
    """Run the poisoning cases and a corrected native repeatability check."""
    _activate()
    ds0 = _open()
    gt = ds0.GetGeoTransform()
    side = BLOCK * 4
    px, py = int((LON - gt[0]) / gt[1]), int((LAT - gt[3]) / gt[5])
    x0 = (px - side // 2) // BLOCK * BLOCK
    y0 = (py - side // 2) // BLOCK * BLOCK
    print(f"{ASSET} @ Everest   native window {x0},{y0} {side}x{side}\n")

    ref = _ov_read(_open(), x0, y0, side)
    print(f"  reference overview[0]: range=[{np.nanmin(ref):.0f},{np.nanmax(ref):.0f}]\n")

    print("  Does a preceding native read poison the overview?")
    d1 = _open()
    _ = _blockwise(d1.GetRasterBand(1), x0, y0, side, side)
    print(f"    after block-wise native : {_verdict(_ov_read(d1, x0, y0, side), ref)}")

    d2 = _open()
    _ = d2.GetRasterBand(1).ReadAsArray(x0, y0, side, side)
    print(f"    after multi-block native: {_verdict(_ov_read(d2, x0, y0, side), ref)}")

    d3 = _open()
    print(f"    no preceding read       : {_verdict(_ov_read(d3, x0, y0, side), ref)}")

    print("\n  Native repeatability (handle held for the read):")
    hold = _open()
    ref_native = _blockwise(hold.GetRasterBand(1), x0, y0, side, side)
    print(f"    reference: range=[{np.nanmin(ref_native):.0f},{np.nanmax(ref_native):.0f}]")
    bad = 0
    for t in range(6):
        h = _open()
        got = _blockwise(h.GetRasterBand(1), x0, y0, side, side)
        same = np.allclose(np.nan_to_num(got), np.nan_to_num(ref_native), rtol=0, atol=1e-6)
        finite = got[np.isfinite(got)]
        impl = int(((finite < PLAUSIBLE[0]) | (finite > PLAUSIBLE[1])).sum())
        if not same or impl:
            bad += 1
        print(f"    t{t}: match={same} implausible={impl} "
              f"range=[{np.nanmin(got):.0f},{np.nanmax(got):.0f}]")
    print(f"\n  native mismatches: {bad}/6")


if __name__ == "__main__":
    main()
