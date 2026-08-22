"""Gate A6 - root-cause the intermittent corruption A1 saw on overview reads.

A1 twice saw an overview level return impossible elevations, then could not
reproduce it; block alignment, pixel encoding, handle state and sustained load
were each tested and refuted. A5 showed the native path unaffected. What is left
is to catch the fault in the act with the transport visible, so the report can
say what the server actually returned.

This runs the exact read that failed - overview levels 0-2 of SRTM over Everest -
with `CPL_DEBUG` on, keeping a rolling buffer of the driver's own HTTP lines. Any
round whose pixels fail the oracle dumps the transport log for that round, which
is the artefact a GDAL issue needs.

The oracle is the one A5 settled on: bounds after masking the fill, degeneracy,
and equality with a reference. It runs until it catches a failure or exhausts its
round budget, and says plainly which happened - a clean run is not evidence the
fault is gone, only that it did not appear here.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque

import numpy as np
import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
from _common import BLOCK, activate, blockwise, judge, open_eedai
from osgeo import gdal

gdal.UseExceptions()

KEY = os.environ["GEE_SERVICE_KEY"]
ASSET, BAND, LON, LAT = "USGS/SRTMGL1_003", "elevation", 86.925, 27.988
BOUNDS, FILL = (-500.0, 9000.0), (-32768.0, -32767.0)
SIDE = 1024
LEVELS = (0, 1, 2)
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 25

_LOG: deque[str] = deque(maxlen=400)


# GDAL's handler signature.
def _collect(err_class, err_no, msg):  # noqa: ARG001
    """Keep a rolling window of GDAL debug and error lines."""
    _LOG.append(f"[{err_class}/{err_no}] {msg}")


def _ov_window(ds, level: int, x0: int, y0: int):
    """Map the native window onto one overview level."""
    ov = ds.GetRasterBand(1).GetOverview(level)
    fx, fy = ds.RasterXSize / ov.XSize, ds.RasterYSize / ov.YSize
    return (
        ov,
        int(round(x0 / fx)),
        int(round(y0 / fy)),
        int(round(SIDE / fx)),
        int(round(SIDE / fy)),
    )


def main() -> None:
    """Hammer the overview read with the transport logged, and dump on failure."""
    activate()
    gdal.SetConfigOption("CPL_DEBUG", "ON")
    gdal.PushErrorHandler(_collect)

    base = open_eedai(ASSET, bands=[BAND])
    gt = base.GetGeoTransform()
    px, py = int((LON - gt[0]) / gt[1]), int((LAT - gt[3]) / gt[5])
    x0 = (px - SIDE // 2) // BLOCK * BLOCK
    y0 = (py - SIDE // 2) // BLOCK * BLOCK
    print(
        f"{ASSET} @ Everest  window {x0},{y0} {SIDE}x{SIDE}  levels {LEVELS}  "
        f"rounds {ROUNDS}\n"
    )

    references: dict[int, np.ndarray] = {}
    for level in LEVELS:
        ds = open_eedai(ASSET, bands=[BAND])
        ov, ox0, oy0, ow, oh = _ov_window(ds, level, x0, y0)
        try:
            references[level] = blockwise(ov, ox0, oy0, ow, oh)
            ok, detail = judge(references[level], BOUNDS, FILL)
            print(f"  reference ov[{level}]: {'ok ' if ok else 'BAD'} {detail}")
        # The probe reports, it does not recover.
        except Exception as exc:  # noqa: BLE001
            print(
                f"  reference ov[{level}]: RAISED {type(exc).__name__}: {str(exc)[:70]}"
            )

    caught = 0
    started = time.time()
    for r in range(ROUNDS):
        marks = []
        for level in LEVELS:
            _LOG.clear()
            try:
                ds = open_eedai(ASSET, bands=[BAND])
                ov, ox0, oy0, ow, oh = _ov_window(ds, level, x0, y0)
                arr = blockwise(ov, ox0, oy0, ow, oh)
            except Exception as exc:  # noqa: BLE001
                caught += 1
                marks.append(f"L{level}:{type(exc).__name__[:9]}")
                print(
                    f"\n  !! round {r} level {level} RAISED {type(exc).__name__}: {exc}"
                )
                print("  --- transport log ---")
                for line in list(_LOG)[-25:]:
                    print(f"    {line[:150]}")
                continue
            ok, detail = judge(arr, BOUNDS, FILL)
            ref = references.get(level)
            stable = ref is None or bool(
                np.allclose(np.nan_to_num(arr), np.nan_to_num(ref), rtol=0, atol=1e-6)
            )
            if ok and stable:
                marks.append(f"L{level}:ok")
                continue
            caught += 1
            marks.append(f"L{level}:CAUGHT")
            print(
                f"\n  !! round {r} level {level}: healthy={ok} stable={stable} {detail}"
            )
            print("  --- transport log ---")
            for line in list(_LOG)[-25:]:
                print(f"    {line[:150]}")
        print(f"  round {r:02d}: " + "  ".join(marks))

    gdal.PopErrorHandler()
    print(f"\n  elapsed {time.time() - started:.0f}s   failures caught: {caught}")
    if not caught:
        print("  NOT REPRODUCED in this run - which is not evidence the fault is gone.")


if __name__ == "__main__":
    main()
