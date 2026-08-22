"""Gate A2 - is a BLOCK_SIZE above the pinned 256 still read-correct, and cheaper?

pyramids-eo pins `BLOCK_SIZE=256` and reads a window one block per `RasterIO`
call, so round-trips for a window scale with its area in 256-px blocks. The
driver documents 256 only as the *default*, so a larger block should cut
round-trips proportionally - if the pixels still come back right.

Correctness is judged against a 256-px reference read of the same ground window;
cost is measured as wall time plus a count of the driver's own debug lines, whose
shape is sampled first so the count means something.

Result on 2026-08-22: identical pixels at every size from 128 to 2048, and 1024
read a 1024-px window ~9x faster than the pin. 2048 is slower again - past the
window size you pay for pixels you discard.
"""

from __future__ import annotations

import time
from collections import Counter

from _common import activate, blockwise, judge, matches, open_eedai
from osgeo import gdal

ASSET, BAND, LON, LAT = "USGS/SRTMGL1_003", "elevation", 86.925, 27.988
BOUNDS, FILL = (-500.0, 9000.0), (-32768.0, -32767.0)
SIDE = 1024
CANDIDATES = [128, 256, 512, 1024, 2048]

_MESSAGES: list[str] = []


def _collect(err_class, err_no, msg):  # noqa: ARG001
    """Record every GDAL debug/error message for later counting."""
    _MESSAGES.append(msg)


def main() -> None:
    """Read one window at each candidate block size and compare cost and pixels."""
    activate()
    gdal.SetConfigOption("CPL_DEBUG", "ON")
    gdal.PushErrorHandler(_collect)

    reference_ds = open_eedai(ASSET, bands=[BAND], block=256)
    gt = reference_ds.GetGeoTransform()
    px, py = int((LON - gt[0]) / gt[1]), int((LAT - gt[3]) / gt[5])
    # Aligned for every candidate, so no size reads a differently-placed window.
    x0 = (px - SIDE // 2) // max(CANDIDATES) * max(CANDIDATES)
    y0 = (py - SIDE // 2) // max(CANDIDATES) * max(CANDIDATES)
    print(f"{ASSET}  window {x0},{y0} {SIDE}x{SIDE}\n")

    _MESSAGES.clear()
    reference = blockwise(reference_ds.GetRasterBand(1), x0, y0, SIDE, SIDE, block=256)
    print("  debug-message prefixes seen during one 256-px read:")
    for prefix, count in Counter(m.split(":")[0][:34] for m in _MESSAGES).most_common(
        6
    ):
        print(f"    {count:5d}  {prefix}")

    print(
        f"\n  {'asked':>6} {'actual':>7} {'reads':>6} {'debug':>7} {'secs':>7}  pixels"
    )
    print("  " + "-" * 62)
    for block in CANDIDATES:
        dataset = open_eedai(ASSET, bands=[BAND], block=block)
        band = dataset.GetRasterBand(1)
        actual = band.GetBlockSize()[0]
        calls = ((SIDE + actual - 1) // actual) ** 2
        _MESSAGES.clear()
        started = time.time()
        try:
            arr = blockwise(band, x0, y0, SIDE, SIDE, block=actual)
        except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
            print(
                f"  {block:>6} {actual:>7} {calls:>6} {'-':>7} {'-':>7}  "
                f"RAISED {type(exc).__name__}"
            )
            continue
        secs = time.time() - started
        ok, detail = judge(arr, BOUNDS, FILL)
        same = matches(arr, reference)
        verdict = "ok" if ok and same else ("MISMATCH" if ok else "BAD")
        print(
            f"  {block:>6} {actual:>7} {calls:>6} {len(_MESSAGES):>7} {secs:>7.1f}  "
            f"{verdict:>8} {detail}"
        )

    gdal.PopErrorHandler()


if __name__ == "__main__":
    main()
