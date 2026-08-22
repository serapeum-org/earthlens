"""Gate A1 (v4) - is the overview fault really the wire encoding?

v3 refuted block-alignment: reading an overview block-by-block returned the same
wrong pixels as one multi-block call, so the read strategy is not the fault.

What the bad levels do share is impossible values - SRTM elevation came back as
-30933 and 16384 against a true range of 3661..8748. That is what a signed 16-bit
raster looks like after a round-trip through a byte-oriented codec, and EEDAI's
PIXEL_ENCODING defaults to AUTO, where PNG/JPEG are documented as Byte-only.

So: read the same overview windows under each PIXEL_ENCODING and see which ones
reproduce the native data.
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
ENCODINGS = ["AUTO", "NPY", "GEO_TIFF"]


def _activate() -> None:
    """Point GDAL's EEDA auth at the service-account key."""
    with open(KEY, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def _open(asset: str, band_name: str, encoding: str):
    """Open the asset with one explicit pixel encoding."""
    return gdal.OpenEx(
        f"EEDAI:{asset}",
        gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR,
        open_options=[
            f"BLOCK_SIZE={BLOCK}",
            f"BANDS={band_name}",
            f"PIXEL_ENCODING={encoding}",
        ],
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


def _agree(truth: np.ndarray, got: np.ndarray) -> tuple[float, float]:
    """Return correlation and mean relative error against the ground truth."""
    x, y = truth.ravel(), got.ravel()
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 8 or np.std(x[ok]) < 1e-9:
        return float("nan"), float("nan")
    return (
        float(np.corrcoef(x[ok], y[ok])[0, 1]),
        float(np.mean(np.abs(y[ok] - x[ok])) / (np.abs(x[ok]).mean() + 1e-9)),
    )


def main() -> None:
    """Compare every overview level under every pixel encoding."""
    _activate()
    asset, band_name, lon, lat = "USGS/SRTMGL1_003", "elevation", 86.925, 27.988

    base = _open(asset, band_name, "NPY")
    gt = base.GetGeoTransform()
    side = BLOCK * 4
    px, py = int((lon - gt[0]) / gt[1]), int((lat - gt[3]) / gt[5])
    x0 = (px - side // 2) // BLOCK * BLOCK
    y0 = (py - side // 2) // BLOCK * BLOCK
    truth_full = _blockwise(base.GetRasterBand(1), x0, y0, side, side)
    print(f"{asset} @ Everest  window {x0},{y0} {side}x{side}")
    print(f"native ground truth (NPY): mean={np.nanmean(truth_full):.1f} "
          f"range=[{np.nanmin(truth_full):.0f},{np.nanmax(truth_full):.0f}]\n")

    header = f"  {'lvl':>3} {'factor':>6} " + " ".join(f"{e:>24}" for e in ENCODINGS)
    print(header)
    print("  " + "-" * (len(header) - 2))

    handles = {e: _open(asset, band_name, e) for e in ENCODINGS}
    for i in range(handles["NPY"].GetRasterBand(1).GetOverviewCount()):
        cells = []
        factor = None
        for enc in ENCODINGS:
            b = handles[enc].GetRasterBand(1)
            ov = b.GetOverview(i)
            fx = handles[enc].RasterXSize / ov.XSize
            fy = handles[enc].RasterYSize / ov.YSize
            factor = fx
            ox0, oy0 = int(round(x0 / fx)), int(round(y0 / fy))
            ow, oh = int(round(side / fx)), int(round(side / fy))
            if ow < 4 or oh < 4:
                cells.append("-")
                continue
            fh_, fw_ = side // oh, side // ow
            truth = truth_full[: oh * fh_, : ow * fw_].reshape(oh, fh_, ow, fw_).mean(axis=(1, 3))
            try:
                arr = _blockwise(ov, ox0, oy0, ow, oh)
                c, r = _agree(truth, arr)
                lo, hi = np.nanmin(arr), np.nanmax(arr)
                cells.append(f"c={c:+.3f} [{lo:.0f},{hi:.0f}]" if np.isfinite(c) else "flat")
            except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
                cells.append(type(exc).__name__[:20])
        if all(c == "-" for c in cells):
            break
        print(f"  {i:>3} {factor:>6.0f} " + " ".join(f"{c:>24}" for c in cells))


if __name__ == "__main__":
    main()
