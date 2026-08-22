"""Gate A2 - is a BLOCK_SIZE above the pinned 256 still read-correct, and cheaper?

pyramids-eo pins `BLOCK_SIZE=256` and reads a window one block per `RasterIO`
call, so the number of network round-trips for a window scales with its area in
256-px blocks. The driver documents 256 only as the *default*, so a larger block
should cut round-trips proportionally - if the pixels still come back right.

Correctness is judged against a 256-px reference read of the same ground window,
plus physical bounds and a degeneracy check (a constant raster passes any bounds
test). Cost is measured two ways: wall time, and a count of the driver's own
debug lines, whose shape is sampled first because it is what makes the count
meaningful rather than guessed.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter

import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

KEY = os.environ["GEE_SERVICE_KEY"]
ASSET, BAND, LON, LAT = "USGS/SRTMGL1_003", "elevation", 86.925, 27.988
BOUNDS, FILL = (-500.0, 9000.0), (-32768.0, -32767.0)
SIDE = 1024
CANDIDATES = [128, 256, 512, 1024, 2048]

_MESSAGES: list[str] = []


def _collect(err_class, err_no, msg):  # noqa: ARG001 - GDAL's handler signature
    """Record every GDAL debug/error message for later counting."""
    _MESSAGES.append(msg)


def _activate() -> None:
    """Point GDAL's EEDA auth at the service-account key."""
    with open(KEY, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def _open(block: int):
    """Open the probe asset with one candidate block size."""
    return gdal.OpenEx(
        f"EEDAI:{ASSET}",
        gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR,
        open_options=[f"BLOCK_SIZE={block}", f"BANDS={BAND}"],
    )


def _blockwise(band, x0: int, y0: int, side: int, block: int) -> np.ndarray:
    """Read a window one block at a time, at this dataset's block size."""
    out = np.full((side, side), np.nan, dtype="float64")
    for by in range(y0 // block * block, y0 + side, block):
        for bx in range(x0 // block * block, x0 + side, block):
            rx0, ry0 = max(bx, x0), max(by, y0)
            rx1, ry1 = min(bx + block, x0 + side), min(by + block, y0 + side)
            if rx1 <= rx0 or ry1 <= ry0:
                continue
            out[ry0 - y0 : ry1 - y0, rx0 - x0 : rx1 - x0] = band.ReadAsArray(
                rx0, ry0, rx1 - rx0, ry1 - ry0
            )
    return out


def _judge(arr: np.ndarray) -> tuple[bool, str]:
    """Judge a read on fill-masked bounds and on degeneracy."""
    observed = arr.astype("float64").copy()
    for sentinel in FILL:
        observed[observed == sentinel] = np.nan
    observed = observed[np.isfinite(observed)]
    if observed.size == 0:
        return False, "no observed pixels"
    outside = int(((observed < BOUNDS[0]) | (observed > BOUNDS[1])).sum())
    if outside:
        return False, f"{outside} px outside {BOUNDS}"
    if float(observed.std()) < 1e-6:
        return False, "degenerate"
    return True, f"[{observed.min():.0f},{observed.max():.0f}]"


def main() -> None:
    """Read one window at each candidate block size and compare cost and pixels."""
    _activate()
    gdal.SetConfigOption("CPL_DEBUG", "ON")
    gdal.PushErrorHandler(_collect)

    ref_ds = _open(256)
    gt = ref_ds.GetGeoTransform()
    px, py = int((LON - gt[0]) / gt[1]), int((LAT - gt[3]) / gt[5])
    x0 = (px - SIDE // 2) // 2048 * 2048  # aligned for every candidate
    y0 = (py - SIDE // 2) // 2048 * 2048
    print(f"{ASSET}  window {x0},{y0} {SIDE}x{SIDE}\n")

    _MESSAGES.clear()
    reference = _blockwise(ref_ds.GetRasterBand(1), x0, y0, SIDE, 256)
    sample = Counter(m.split(":")[0][:34] for m in _MESSAGES)
    print("  debug-message prefixes seen during one 256-px read:")
    for prefix, n in sample.most_common(6):
        print(f"    {n:5d}  {prefix}")
    print()

    print(f"  {'asked':>6} {'actual':>7} {'reads':>6} {'debug':>7} {'secs':>7}  {'pixels':>26}")
    print("  " + "-" * 70)
    for block in CANDIDATES:
        ds = _open(block)
        band = ds.GetRasterBand(1)
        actual = band.GetBlockSize()[0]
        n_reads = ((SIDE + actual - 1) // actual) ** 2
        _MESSAGES.clear()
        started = time.time()
        try:
            arr = _blockwise(band, x0, y0, SIDE, actual)
        except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
            print(f"  {block:>6} {actual:>7} {n_reads:>6} {'-':>7} {'-':>7}  "
                  f"RAISED {type(exc).__name__}: {str(exc)[:28]}")
            continue
        secs = time.time() - started
        debug_lines = len(_MESSAGES)
        ok, detail = _judge(arr)
        same = bool(np.allclose(np.nan_to_num(arr), np.nan_to_num(reference), rtol=0, atol=1e-6))
        verdict = "ok" if ok and same else ("MISMATCH" if ok else "BAD")
        print(f"  {block:>6} {actual:>7} {n_reads:>6} {debug_lines:>7} {secs:>7.1f}  "
              f"{verdict:>8} {detail}")

    gdal.PopErrorHandler()


if __name__ == "__main__":
    main()
