"""Gate A1 (v3) - is the fault the overviews, or multi-block RasterIO on them?

v2 showed overview levels 3-7 reproducing the native data exactly (corr 1.0000)
while level 0 returned garbage and levels 1-2 raised "Access window out of
range". Sorting those by window geometry, every exact read fitted inside a
single 256-px block and every bad one spanned several - the same failure mode
pyramids-eo already documents for *native* reads and works around by reading
block-wise.

This isolates that: for one land window, read each overview level twice - once
as a single multi-block RasterIO, once block-by-block - and compare both against
a native-resolution ground truth. If the block-wise overview read matches, the
overviews are sound and only the driver's multi-block path is broken.
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


def _activate() -> None:
    """Point GDAL's EEDA auth at the service-account key."""
    with open(KEY, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def _blockwise(band, x0: int, y0: int, w: int, h: int) -> np.ndarray:
    """Read a window one block at a time, never crossing a block in one call."""
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


def _agree(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Return correlation and mean relative error between two arrays."""
    x, y = a.ravel(), b.ravel()
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 8 or np.std(x[ok]) < 1e-9:
        return float("nan"), float("nan")
    corr = float(np.corrcoef(x[ok], y[ok])[0, 1])
    rel = float(np.mean(np.abs(y[ok] - x[ok])) / (np.abs(x[ok]).mean() + 1e-9))
    return corr, rel


def _blocks_spanned(off: int, size: int) -> int:
    """How many 256-px blocks a window touches along one axis."""
    return (off + size - 1) // BLOCK - off // BLOCK + 1


def main() -> None:
    """Read one window natively and from every overview, two ways each."""
    _activate()
    asset, band_name, lon, lat = "USGS/SRTMGL1_003", "elevation", 86.925, 27.988
    ds = gdal.OpenEx(
        f"EEDAI:{asset}",
        gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR,
        open_options=[f"BLOCK_SIZE={BLOCK}", f"BANDS={band_name}"],
    )
    band = ds.GetRasterBand(1)
    gt = ds.GetGeoTransform()
    side = BLOCK * 4
    px, py = int((lon - gt[0]) / gt[1]), int((lat - gt[3]) / gt[5])
    x0 = (px - side // 2) // BLOCK * BLOCK
    y0 = (py - side // 2) // BLOCK * BLOCK

    print(f"{asset} @ Everest   native window {x0},{y0} {side}x{side}\n")
    native_bw = _blockwise(band, x0, y0, side, side)
    print(
        f"  native block-wise : mean={np.nanmean(native_bw):.1f} "
        f"range=[{np.nanmin(native_bw):.0f},{np.nanmax(native_bw):.0f}]"
    )

    # Is the native multi-block path broken too, on real (non-constant) data?
    try:
        native_mb = band.ReadAsArray(x0, y0, side, side).astype("float64")
        corr, rel = _agree(native_bw, native_mb)
        print(
            f"  native multi-block: mean={np.nanmean(native_mb):.1f} "
            f"range=[{np.nanmin(native_mb):.0f},{np.nanmax(native_mb):.0f}]  "
            f"corr={corr:.4f} rel={rel:.4f}  "
            f"{'MATCHES' if corr > 0.999 and rel < 1e-3 else 'DIFFERS'}"
        )
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
        print(f"  native multi-block: RAISED {type(exc).__name__}: {str(exc)[:90]}")

    print(
        f"\n  {'lvl':>3} {'factor':>6} {'blocks':>7}  {'multi-block':>22}  {'block-wise':>22}"
    )
    print(f"  {'-' * 68}")
    for i in range(band.GetOverviewCount()):
        ov = band.GetOverview(i)
        fx, fy = ds.RasterXSize / ov.XSize, ds.RasterYSize / ov.YSize
        ox0, oy0 = int(round(x0 / fx)), int(round(y0 / fy))
        ow, oh = int(round(side / fx)), int(round(side / fy))
        if ow < 4 or oh < 4:
            break

        # Ground truth: block-mean the native window down to this level's shape.
        fh_, fw_ = side // oh, side // ow
        truth = (
            native_bw[: oh * fh_, : ow * fw_]
            .reshape(oh, fh_, ow, fw_)
            .mean(axis=(1, 3))
        )

        spanned = _blocks_spanned(ox0, ow) * _blocks_spanned(oy0, oh)

        try:
            mb = ov.ReadAsArray(ox0, oy0, ow, oh).astype("float64")
            c1, r1 = _agree(truth, mb)
            s1 = (
                "RAISED"
                if False
                else (f"corr={c1:.4f} rel={r1:.3f}" if np.isfinite(c1) else "flat")
            )
        except Exception as exc:  # noqa: BLE001
            s1 = f"{type(exc).__name__[:14]}"

        try:
            bw = _blockwise(ov, ox0, oy0, ow, oh)
            c2, r2 = _agree(truth, bw)
            s2 = f"corr={c2:.4f} rel={r2:.3f}" if np.isfinite(c2) else "flat"
        except Exception as exc:  # noqa: BLE001
            s2 = f"{type(exc).__name__[:14]}"

        print(f"  {i:>3} {fx:>6.0f} {spanned:>7}  {s1:>22}  {s2:>22}")


if __name__ == "__main__":
    main()
