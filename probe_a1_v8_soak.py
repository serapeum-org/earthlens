"""Gate A1 (v8) - can sustained reading reproduce the corruption?

v2 and v3 saw overview levels 0-2 return impossible elevations; v4-v7 could not
reproduce it across ~40 reads while refuting block alignment, pixel encoding and
handle state as the cause. The runs that failed came late in a session that had
already issued many reads, so the remaining suspect is load: quota or throttling
answered with a body the driver decodes into plausible-shaped garbage.

This soaks the same read and reports the first failure and the failure rate,
using physical bounds as the oracle so a wrong result is caught even when it is
self-consistent.
"""

from __future__ import annotations

import json
import os
import time

import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

KEY = os.environ["GEE_SERVICE_KEY"]
BLOCK = 256
ASSET, BAND, LON, LAT = "USGS/SRTMGL1_003", "elevation", 86.925, 27.988
PLAUSIBLE = (-500.0, 9000.0)
ROUNDS = 30


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


def main() -> None:
    """Read overviews repeatedly and report any corrupt or failed round."""
    _activate()
    ds = _open()
    gt = ds.GetGeoTransform()
    side = BLOCK * 4
    px, py = int((LON - gt[0]) / gt[1]), int((LAT - gt[3]) / gt[5])
    x0 = (px - side // 2) // BLOCK * BLOCK
    y0 = (py - side // 2) // BLOCK * BLOCK
    print(f"soak: {ROUNDS} rounds x overview levels 0-2, window {x0},{y0} {side}x{side}\n")

    stats = {0: [0, 0], 1: [0, 0], 2: [0, 0]}  # level -> [attempts, failures]
    started = time.time()
    for r in range(ROUNDS):
        marks = []
        for level in (0, 1, 2):
            stats[level][0] += 1
            try:
                d = _open()
                ov = d.GetRasterBand(1).GetOverview(level)
                fx, fy = d.RasterXSize / ov.XSize, d.RasterYSize / ov.YSize
                arr = _blockwise(ov, int(round(x0 / fx)), int(round(y0 / fy)),
                                 int(round(side / fx)), int(round(side / fy)))
            except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
                stats[level][1] += 1
                marks.append(f"L{level}:{type(exc).__name__[:9]}")
                continue
            finite = arr[np.isfinite(arr)]
            impl = int(((finite < PLAUSIBLE[0]) | (finite > PLAUSIBLE[1])).sum())
            if impl:
                stats[level][1] += 1
                marks.append(f"L{level}:CORRUPT({impl}px,"
                             f"[{np.nanmin(arr):.0f},{np.nanmax(arr):.0f}])")
            else:
                marks.append(f"L{level}:ok")
        print(f"  round {r:02d}  " + "  ".join(marks))

    print(f"\n  elapsed {time.time() - started:.0f}s")
    for level, (n, bad) in stats.items():
        print(f"  level {level}: {bad}/{n} bad")


if __name__ == "__main__":
    main()
