"""Gate A5 (scoped) - can the intermittent EEDAI corruption hit the NATIVE path?

A1 saw impossible pixel values twice, but only ever on *overview* reads, which
pyramids-eo refuses to use. earthlens ships the native block-wise read, so the
question that matters for production is narrower: does the same silent corruption
ever reach that path?

Three oracles, because the failure is silent - no exception, right shape,
internally consistent, wrong numbers:

1. **bounds**, after masking the band's fill value: an elevation outside
   -500..9000 m, or a percentage outside 0..100, cannot be real;
2. **degeneracy**: a window chosen for relief must vary. A constant raster passes
   every bounds test ever written, which is how the first version of the
   end-to-end leg scored an all-zero read as healthy;
3. **repeat-consistency**: every repeat must equal a reference read of the same
   window, compared on raw values so a shifted fill pattern still trips it.

The fill values are declared per band below rather than read from the driver,
because EEDAI exposes none: `GetNoDataValue()` is `None`, the mask band reports
`GMF_ALL_VALID`, and neither band nor dataset metadata carries a fill hint. That
is itself the gap `PE-12` tracks - and it means any consumer masking EEDAI output
has to source the sentinel from the Earth Engine catalog, not from GDAL.
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
from osgeo import gdal

gdal.UseExceptions()

KEY = os.environ["GEE_SERVICE_KEY"]
BLOCK = 256
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 6

# asset, band, plausible (lo, hi), fill sentinels, sample locations
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
WINDOWS = [BLOCK * 2, BLOCK * 4]


def _activate() -> None:
    """Point GDAL's EEDA auth at the service-account key."""
    with open(KEY, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def _open(asset: str, band: str):
    """Open an asset the way pyramids-eo does."""
    return gdal.OpenEx(
        f"EEDAI:{asset}",
        gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR,
        open_options=[f"BLOCK_SIZE={BLOCK}", f"BANDS={band}"],
    )


def _blockwise(band, x0: int, y0: int, w: int, h: int) -> np.ndarray:
    """Read a window one block at a time - pyramids-eo's `_materialize` read."""
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


def _window_for(ds, lon: float, lat: float, side: int) -> tuple[int, int] | None:
    """Block-aligned pixel window centred on a lon/lat, or None if off-asset."""
    gt = ds.GetGeoTransform()
    px, py = int((lon - gt[0]) / gt[1]), int((lat - gt[3]) / gt[5])
    x0 = (px - side // 2) // BLOCK * BLOCK
    y0 = (py - side // 2) // BLOCK * BLOCK
    if x0 < 0 or y0 < 0 or x0 + side > ds.RasterXSize or y0 + side > ds.RasterYSize:
        return None
    return x0, y0


def _valid(arr: np.ndarray, fill: tuple[float, ...]) -> np.ndarray:
    """Return the observed pixels, with the band's fill sentinels removed."""
    out = arr.astype("float64").copy()
    for sentinel in fill:
        out[out == sentinel] = np.nan
    return out[np.isfinite(out)]


def _judge(
    arr: np.ndarray, bounds: tuple[float, float], fill: tuple[float, ...]
) -> tuple[bool, str]:
    """Judge a read on fill-masked bounds and on degeneracy."""
    observed = _valid(arr, fill)
    if observed.size == 0:
        return False, "no observed pixels once the fill was masked"
    outside = int(((observed < bounds[0]) | (observed > bounds[1])).sum())
    if outside:
        return False, (
            f"{outside} px outside {bounds} "
            f"(observed [{observed.min():.1f},{observed.max():.1f}])"
        )
    if float(observed.std()) < 1e-6:
        return False, f"degenerate - every observed pixel is {observed.flat[0]:.1f}"
    return True, (
        f"[{observed.min():7.1f},{observed.max():7.1f}] std={observed.std():6.1f} "
        f"valid={observed.size / arr.size:.0%}"
    )


def main() -> None:
    """Soak the native read path and report every anomaly."""
    _activate()
    combos = []
    print("building references...")
    for asset, band_name, bounds, fill, places in TARGETS:
        ds = _open(asset, band_name)
        for place, lon, lat in places:
            for side in WINDOWS:
                win = _window_for(ds, lon, lat, side)
                if win is None:
                    print(f"  skip {asset} @ {place} {side}px (outside asset)")
                    continue
                x0, y0 = win
                handle = _open(asset, band_name)
                ref = _blockwise(handle.GetRasterBand(1), x0, y0, side, side)
                ok, detail = _judge(ref, bounds, fill)
                combos.append(
                    (asset, band_name, bounds, fill, place, x0, y0, side, ref)
                )
                print(
                    f"  ref {asset.split('/')[-1][:22]:22s} {place:11s} {side:>4}px  "
                    f"{'ok ' if ok else 'BAD'} {detail}"
                )

    print(
        f"\nsoaking {len(combos)} combos x {ROUNDS} rounds "
        f"({len(combos) * ROUNDS} native reads)\n"
    )
    anomalies: list[str] = []
    started = time.time()

    for r in range(ROUNDS):
        marks = []
        for asset, band_name, bounds, fill, place, x0, y0, side, ref in combos:
            tag = f"{asset.split('/')[-1][:10]}@{place[:6]}/{side}"
            try:
                h = _open(asset, band_name)
                got = _blockwise(h.GetRasterBand(1), x0, y0, side, side)
            except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
                anomalies.append(
                    f"round {r} {tag}: RAISED {type(exc).__name__}: {str(exc)[:80]}"
                )
                marks.append(f"{tag}:ERR")
                continue
            ok, detail = _judge(got, bounds, fill)
            stable = bool(
                np.allclose(np.nan_to_num(got), np.nan_to_num(ref), rtol=0, atol=1e-6)
            )
            if not ok or not stable:
                anomalies.append(
                    f"round {r} {tag}: healthy={ok} stable={stable} {detail}"
                )
                marks.append(f"{tag}:BAD")
            else:
                marks.append(f"{tag}:ok")
        print(f"  round {r}: " + " ".join(marks))

    print(f"\n  elapsed {time.time() - started:.0f}s")
    print(f"  native reads: {len(combos) * ROUNDS}, anomalies: {len(anomalies)}")
    for line in anomalies:
        print(f"    ! {line}")
    if not anomalies:
        print("    (none - the native path was stable across every combo and round)")


if __name__ == "__main__":
    main()
