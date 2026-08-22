"""Gate A5 - can the intermittent EEDAI corruption reach the NATIVE path?

A1 saw impossible pixel values twice, but only ever on *overview* reads, which
pyramids-eo refuses to use. earthlens ships the native block-wise read, so the
question that matters for production is narrower: does the same silent corruption
ever reach that path?

Soaks several assets, regions and window sizes with the rounds interleaved across
combos, so a time-varying fault is sampled everywhere rather than concentrated
wherever the clock happened to land. Both the raw read and `from_earthengine` -
the call earthlens actually makes - are exercised.

    python probe_a5_native_soak.py [rounds]

Result on 2026-08-22: 96 reads, zero anomalies.
"""

from __future__ import annotations

import sys
import time

import numpy as np
from _common import (
    BLOCK,
    KEY,
    activate,
    blockwise,
    judge,
    matches,
    open_eedai,
    window_for,
)
from pyramids_eo.earthengine import from_earthengine

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
WINDOWS = [BLOCK * 2, BLOCK * 4]

# asset, band, plausible bounds, fill sentinels, sample locations
TARGETS = [
    (
        "USGS/SRTMGL1_003",
        "elevation",
        (-500.0, 9000.0),
        (-32768.0, -32767.0),
        [
            ("Everest", 86.925, 27.988),
            ("Matterhorn", 7.659, 45.976),
            ("Andes", -70.011, -32.653),
        ],
    ),
    (
        "NASA/NASADEM_HGT/001",
        "elevation",
        (-500.0, 9000.0),
        (-32768.0, -32767.0),
        [
            ("Everest", 86.925, 27.988),
            ("Alps", 7.659, 45.976),
        ],
    ),
    # GSW `occurrence` is a 0-100 percentage; Int8 -128 is its unobserved fill.
    (
        "JRC/GSW1_4/GlobalSurfaceWater",
        "occurrence",
        (0.0, 100.0),
        (-128.0,),
        [
            ("Amazon", -59.95, -3.13),
            ("Nile", 31.23, 30.05),
        ],
    ),
]

# The end-to-end leg must pass an explicit pixel `shape`: pyramids-eo sizes output
# in target-CRS units, so a metre `scale` against EPSG:4326 asks for degrees per
# pixel and returns a degenerate raster - the bug earthlens resolves to `shape=`.
END_TO_END = [
    (
        "USGS/SRTMGL1_003",
        "elevation",
        (7.60, 45.93, 7.72, 46.02),
        (128, 128),
        (-500.0, 9000.0),
    ),
    (
        "USGS/SRTMGL1_003",
        "elevation",
        (86.87, 27.95, 86.99, 28.03),
        (192, 192),
        (-500.0, 9000.0),
    ),
    (
        "NASA/NASADEM_HGT/001",
        "elevation",
        (7.60, 45.93, 7.72, 46.02),
        (128, 128),
        (-500.0, 9000.0),
    ),
]


def _build_references() -> list[tuple]:
    """Read each combo once, to compare every later round against."""
    combos = []
    for asset, band_name, bounds, fill, places in TARGETS:
        dataset = open_eedai(asset, bands=[band_name])
        for place, lon, lat in places:
            for side in WINDOWS:
                window = window_for(dataset, lon, lat, side)
                if window is None:
                    print(f"  skip {asset} @ {place} {side}px (outside asset)")
                    continue
                x0, y0 = window
                handle = open_eedai(asset, bands=[band_name])
                reference = blockwise(handle.GetRasterBand(1), x0, y0, side, side)
                ok, detail = judge(reference, bounds, fill)
                combos.append(
                    (asset, band_name, bounds, fill, place, x0, y0, side, reference)
                )
                print(
                    f"  ref {asset.split('/')[-1][:22]:22s} {place:11s} {side:>4}px  "
                    f"{'ok ' if ok else 'BAD'} {detail}"
                )
    return combos


def _soak(combos: list[tuple], anomalies: list[str]) -> None:
    """Re-read every combo, round by round, recording each disagreement."""
    for round_ in range(ROUNDS):
        marks = []
        for asset, band_name, bounds, fill, place, x0, y0, side, reference in combos:
            tag = f"{asset.split('/')[-1][:10]}@{place[:6]}/{side}"
            try:
                handle = open_eedai(asset, bands=[band_name])
                got = blockwise(handle.GetRasterBand(1), x0, y0, side, side)
            except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
                anomalies.append(f"round {round_} {tag}: RAISED {type(exc).__name__}")
                marks.append(f"{tag}:ERR")
                continue
            ok, detail = judge(got, bounds, fill)
            stable = matches(got, reference)
            if ok and stable:
                marks.append(f"{tag}:ok")
            else:
                anomalies.append(
                    f"round {round_} {tag}: healthy={ok} stable={stable} {detail}"
                )
                marks.append(f"{tag}:BAD")
        print(f"  round {round_}: " + " ".join(marks))


def _end_to_end(anomalies: list[str]) -> None:
    """Call the shipped reader repeatedly and compare every result."""
    for asset, band, bbox, shape, bounds in END_TO_END:
        reference = None
        for attempt in range(2):
            try:
                dataset = from_earthengine(
                    asset, bands=[band], bbox=bbox, shape=shape, credentials=KEY
                )
                arr = np.asarray(dataset.read_array(), dtype="float64")
            except Exception as exc:  # noqa: BLE001
                anomalies.append(f"from_earthengine {asset}: {type(exc).__name__}")
                print(f"    {asset:26s} try{attempt}: RAISED {type(exc).__name__}")
                continue
            ok, detail = judge(arr, bounds, (-32768.0, -32767.0))
            stable = reference is None or matches(arr, reference)
            reference = arr if reference is None else reference
            if not ok or not stable:
                anomalies.append(f"from_earthengine {asset} try{attempt}: {detail}")
            print(
                f"    {asset:26s} try{attempt}: {'ok ' if ok and stable else 'BAD'} "
                f"stable={stable} {detail}"
            )


def main() -> None:
    """Soak the native read path and report every anomaly."""
    activate()
    print("building references...")
    combos = _build_references()
    print(
        f"\nsoaking {len(combos)} combos x {ROUNDS} rounds "
        f"({len(combos) * ROUNDS} native reads)\n"
    )

    anomalies: list[str] = []
    started = time.time()
    _soak(combos, anomalies)
    print("\n  from_earthengine (the shipped call):")
    _end_to_end(anomalies)

    print(f"\n  elapsed {time.time() - started:.0f}s")
    print(f"  native reads: {len(combos) * ROUNDS}, anomalies: {len(anomalies)}")
    for line in anomalies:
        print(f"    ! {line}")
    if not anomalies:
        print("    (none - the native path was stable across every combo and round)")


if __name__ == "__main__":
    main()
