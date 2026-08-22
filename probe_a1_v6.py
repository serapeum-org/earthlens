"""Gate A1 (v6) - is EEDAI corruption intermittent, and does it hit native reads?

v3 saw overview levels 0-2 return impossible elevations; v4 and v5 read the same
levels, same window, same open options, and got exact data every time. Encoding
and read order were both refuted as the variable, which leaves non-determinism.

That reframes the question. If EEDAI can silently return wrong pixels under some
condition (load, throttling, a transient response), the important question is not
"are overviews corrupt" but "how often does any read lie, and does it happen on
the native path earthlens already ships".

This repeats one overview read and one native read many times against a reference
captured up front, and counts disagreements. A corrupt result is kept and printed
rather than averaged away.
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
TRIALS = 12
PLAUSIBLE = (-500.0, 9000.0)  # physical bounds for SRTM elevation in metres


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


def _implausible(arr: np.ndarray) -> int:
    """Count pixels outside the physically possible range for this band."""
    finite = arr[np.isfinite(arr)]
    return int(((finite < PLAUSIBLE[0]) | (finite > PLAUSIBLE[1])).sum())


def _trial(label: str, reader, reference: np.ndarray | None) -> np.ndarray | None:
    """Run one read, reporting whether it matches the reference and is plausible."""
    try:
        arr = reader()
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
        print(f"    {label}: RAISED {type(exc).__name__}: {str(exc)[:60]}")
        return None
    bad = _implausible(arr)
    if reference is None:
        print(f"    {label}: reference  range=[{np.nanmin(arr):.0f},{np.nanmax(arr):.0f}]"
              f"  implausible={bad}")
        return arr
    same = np.allclose(np.nan_to_num(arr), np.nan_to_num(reference), rtol=0, atol=1e-6)
    mark = "ok " if same and bad == 0 else "BAD"
    extra = "" if same else f"  range=[{np.nanmin(arr):.0f},{np.nanmax(arr):.0f}]"
    print(f"    {label}: {mark} match={same} implausible={bad}{extra}")
    return arr


def main() -> None:
    """Repeat a native read and an overview read, counting disagreements."""
    _activate()
    ds = _open()
    gt = ds.GetGeoTransform()
    side = BLOCK * 2
    px, py = int((LON - gt[0]) / gt[1]), int((LAT - gt[3]) / gt[5])
    x0 = (px - side // 2) // BLOCK * BLOCK
    y0 = (py - side // 2) // BLOCK * BLOCK
    print(f"{ASSET} @ Everest   window {x0},{y0} {side}x{side}   trials={TRIALS}\n")

    print("  NATIVE (the path earthlens ships):")
    ref_native = _trial("  init", lambda: _blockwise(_open().GetRasterBand(1), x0, y0, side, side), None)
    native_bad = 0
    for t in range(TRIALS):
        got = _trial(f"  t{t:02d}", lambda: _blockwise(_open().GetRasterBand(1), x0, y0, side, side), ref_native)
        if got is None or not np.allclose(np.nan_to_num(got), np.nan_to_num(ref_native), rtol=0, atol=1e-6):
            native_bad += 1

    def read_ov():
        """Read overview level 0 over the same ground area, on a fresh handle."""
        d = _open()
        ov = d.GetRasterBand(1).GetOverview(0)
        fx, fy = d.RasterXSize / ov.XSize, d.RasterYSize / ov.YSize
        return _blockwise(ov, int(round(x0 / fx)), int(round(y0 / fy)),
                          int(round(side / fx)), int(round(side / fy)))

    print("\n  OVERVIEW level 0:")
    ref_ov = _trial("  init", read_ov, None)
    ov_bad = 0
    for t in range(TRIALS):
        got = _trial(f"  t{t:02d}", read_ov, ref_ov)
        if got is None or not np.allclose(np.nan_to_num(got), np.nan_to_num(ref_ov), rtol=0, atol=1e-6):
            ov_bad += 1

    print(f"\n  RESULT: native mismatches {native_bad}/{TRIALS}, "
          f"overview mismatches {ov_bad}/{TRIALS}")


if __name__ == "__main__":
    main()
