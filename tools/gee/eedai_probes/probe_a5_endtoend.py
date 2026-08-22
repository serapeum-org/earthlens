"""Gate A5 - a corrected end-to-end leg through `from_earthengine`.

The soak's end-to-end leg was invalid twice over and proved nothing:

* it passed `scale=90.0` against the default EPSG:4326, and pyramids-eo sizes the
  output in the units of the target CRS - so that asked for 90 *degrees* per
  pixel and got a degenerate raster (earthlens avoids this by resolving metres to
  an explicit `shape=`, which is exactly the bug its live test was written for);
* the oracle accepted the result, because an all-zero elevation raster is inside
  the plausible band. A bounds check alone cannot catch a degenerate read.

This redoes that leg the way earthlens actually calls it - an explicit pixel
`shape` - and adds a degeneracy oracle (a real DEM window must vary) alongside
the bounds and repeat-consistency checks.

Fill sentinels are declared per case rather than read back from the reader,
because EEDAI exposes no nodata at all: `GetNoDataValue()` is `None`, the mask
band reports `GMF_ALL_VALID`, and no band or dataset metadata carries a hint.
"""

from __future__ import annotations

import os
import time

import numpy as np
from _common import judge
from pyramids_eo.earthengine import from_earthengine

KEY = os.environ["GEE_SERVICE_KEY"]
REPEATS = 4

# asset, band, bbox (west, south, east, north), shape, plausible bounds, fill
FILL = (-32768.0, -32767.0)
CASES = [
    (
        "USGS/SRTMGL1_003",
        "elevation",
        (7.60, 45.93, 7.72, 46.02),
        (128, 128),
        (-500.0, 9000.0),
        FILL,
    ),
    (
        "USGS/SRTMGL1_003",
        "elevation",
        (86.87, 27.95, 86.99, 28.03),
        (192, 192),
        (-500.0, 9000.0),
        FILL,
    ),
    (
        "NASA/NASADEM_HGT/001",
        "elevation",
        (7.60, 45.93, 7.72, 46.02),
        (128, 128),
        (-500.0, 9000.0),
        FILL,
    ),
]


def main() -> None:
    """Call the shipped reader repeatedly and compare every result."""
    print(f"from_earthengine end-to-end, {REPEATS} repeats per case\n")
    started = time.time()
    anomalies: list[str] = []

    for asset, band, bbox, shape, bounds, fill in CASES:
        print(f"  {asset}  bbox={bbox}  shape={shape}")
        reference = None
        for attempt in range(REPEATS):
            try:
                ds = from_earthengine(
                    asset, bands=[band], bbox=bbox, shape=shape, credentials=KEY
                )
                arr = np.asarray(ds.read_array(), dtype="float64")
            # The probe reports, it does not recover.
            except Exception as exc:  # noqa: BLE001
                anomalies.append(
                    f"{asset} try{attempt}: {type(exc).__name__}: {str(exc)[:90]}"
                )
                print(f"    try{attempt}: RAISED {type(exc).__name__}: {str(exc)[:70]}")
                continue
            good, detail = judge(arr, bounds, fill)
            if reference is None:
                reference = arr
                stable = True
            else:
                stable = bool(
                    np.allclose(
                        np.nan_to_num(arr), np.nan_to_num(reference), rtol=0, atol=1e-6
                    )
                )
            if not good or not stable:
                anomalies.append(
                    f"{asset} try{attempt}: good={good} stable={stable} {detail}"
                )
            print(
                f"    try{attempt}: {'ok ' if good and stable else 'BAD'} stable={stable}  {detail}"
            )

    print(f"\n  elapsed {time.time() - started:.0f}s   anomalies: {len(anomalies)}")
    for line in anomalies:
        print(f"    ! {line}")
    if not anomalies:
        print("    (none - the shipped call was stable and non-degenerate throughout)")


if __name__ == "__main__":
    main()
