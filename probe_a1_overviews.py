"""Gate A1 - are the GDAL EEDAI driver's overviews actually corrupt?

pyramids-eo states in three places that they are, and materialises every read at
native resolution because of it. That belief is why earthlens caps the tiling
ratio and budgets native pixels, so it is worth settling by measurement.

The test: for one window, compare pixels read from an overview level against a
ground truth built from the *native* pixels of the same ground area, downsampled
locally. A working overview correlates strongly with the local downsample; a
corrupt one does not (all-zero, all-nodata, shifted, or uncorrelated).

Run with the shared earthlens env's interpreter; needs GEE service-account creds.
"""

from __future__ import annotations

import json
import os
import sys

import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
import numpy as np
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


def _open(asset: str, band: str | None = None, block: int = BLOCK):
    """Open an EE asset through EEDAI with the block size pinned."""
    opts = [f"BLOCK_SIZE={block}"]
    if band:
        opts.append(f"BANDS={band}")
    ds = gdal.OpenEx(f"EEDAI:{asset}", gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR, open_options=opts)
    if ds is None:
        raise SystemExit(f"could not open {asset}: {gdal.GetLastErrorMsg()}")
    return ds


def _read_native_blockwise(band, x0: int, y0: int, w: int, h: int) -> np.ndarray:
    """Read a native-resolution window one 256-px block at a time.

    This is the read pyramids-eo considers the only reliably-correct one, so it
    is the ground truth the overview is judged against.
    """
    out = np.full((h, w), np.nan, dtype="float64")
    for by in range(y0 // BLOCK * BLOCK, y0 + h, BLOCK):
        for bx in range(x0 // BLOCK * BLOCK, x0 + w, BLOCK):
            rx0, ry0 = max(bx, x0), max(by, y0)
            rx1 = min(bx + BLOCK, x0 + w)
            ry1 = min(by + BLOCK, y0 + h)
            if rx1 <= rx0 or ry1 <= ry0:
                continue
            chunk = band.ReadAsArray(rx0, ry0, rx1 - rx0, ry1 - ry0)
            if chunk is None:
                raise SystemExit(f"native block read returned None at {rx0},{ry0}")
            out[ry0 - y0 : ry1 - y0, rx0 - x0 : rx1 - x0] = chunk
    return out


def _stats(label: str, arr: np.ndarray) -> dict:
    """Summarise an array, reporting how much of it is finite and distinct."""
    finite = arr[np.isfinite(arr)]
    info = {
        "label": label,
        "shape": arr.shape,
        "finite_frac": round(float(finite.size) / arr.size, 4) if arr.size else 0.0,
        "min": float(finite.min()) if finite.size else None,
        "max": float(finite.max()) if finite.size else None,
        "mean": round(float(finite.mean()), 3) if finite.size else None,
        "std": round(float(finite.std()), 3) if finite.size else None,
        "n_unique": int(np.unique(finite).size) if finite.size else 0,
        "all_zero": bool(finite.size and np.all(finite == 0)),
    }
    print(f"  {label:28s} {info['shape']}  mean={info['mean']}  std={info['std']}  "
          f"min={info['min']}  max={info['max']}  uniq={info['n_unique']}  all_zero={info['all_zero']}")
    return info


def probe(asset: str, band_name: str | None, window_blocks: int = 4) -> None:
    """Compare an overview read against a locally-downsampled native read."""
    print(f"\n{'=' * 78}\nASSET: {asset}\n{'=' * 78}")
    _activate()
    ds = _open(asset, band_name)
    band = ds.GetRasterBand(1)
    print(f"  size={ds.RasterXSize}x{ds.RasterYSize} dtype={gdal.GetDataTypeName(band.DataType)} "
          f"bands={ds.RasterCount} block={band.GetBlockSize()}")

    n_ov = band.GetOverviewCount()
    print(f"  overview count: {n_ov}")
    if n_ov == 0:
        print("  -> driver exposes NO overviews for this asset; nothing to test.")
        return
    for i in range(n_ov):
        ov = band.GetOverview(i)
        fx = ds.RasterXSize / ov.XSize
        print(f"    ov[{i}]: {ov.XSize}x{ov.YSize}  factor~{fx:.1f}  "
              f"block={ov.GetBlockSize()}")

    # A window at native res, block-aligned, near the middle of the asset.
    side = BLOCK * window_blocks
    x0 = (ds.RasterXSize // 2 // BLOCK) * BLOCK
    y0 = (ds.RasterYSize // 2 // BLOCK) * BLOCK
    x0 = min(x0, ds.RasterXSize - side)
    y0 = min(y0, ds.RasterYSize - side)
    print(f"\n  window: x0={x0} y0={y0} {side}x{side} px (native)")

    try:
        native = _read_native_blockwise(band, x0, y0, side, side)
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
        print(f"  !! native block-wise read FAILED: {type(exc).__name__}: {exc}")
        return
    _stats("native (block-wise)", native)

    # Does a single multi-block RasterIO work, or is that broken too?
    try:
        multi = band.ReadAsArray(x0, y0, side, side)
        multi = None if multi is None else multi.astype("float64")
        if multi is None:
            print("  !! multi-block ReadAsArray returned None")
        else:
            s = _stats("native (multi-block)", multi)
            same = np.allclose(np.nan_to_num(multi), np.nan_to_num(native), rtol=0, atol=1e-6)
            print(f"  multi-block matches block-wise: {same}")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! multi-block ReadAsArray raised: {type(exc).__name__}: {exc}")

    # Now the actual question: read the same ground area from each overview.
    for i in range(n_ov):
        ov = band.GetOverview(i)
        fx = ds.RasterXSize / ov.XSize
        fy = ds.RasterYSize / ov.YSize
        ox0, oy0 = int(round(x0 / fx)), int(round(y0 / fy))
        ow, oh = int(round(side / fx)), int(round(side / fy))
        if ow < 1 or oh < 1:
            print(f"    ov[{i}]: window degenerates below 1 px, skipped")
            continue
        print(f"\n  -- overview {i} (factor {fx:.1f}) window {ox0},{oy0} {ow}x{oh}")
        try:
            ov_arr = ov.ReadAsArray(ox0, oy0, ow, oh)
        except Exception as exc:  # noqa: BLE001
            print(f"    !! overview read raised: {type(exc).__name__}: {exc}")
            continue
        if ov_arr is None:
            print("    !! overview read returned None")
            continue
        ov_arr = ov_arr.astype("float64")
        _stats(f"overview[{i}]", ov_arr)

        # Ground truth: block-mean the native window down to the overview shape.
        fh_, fw_ = side // oh, side // ow
        if fh_ < 1 or fw_ < 1:
            print("    (native window too small to downsample for comparison)")
            continue
        trimmed = native[: oh * fh_, : ow * fw_]
        local = trimmed.reshape(oh, fh_, ow, fw_).mean(axis=(1, 3))
        _stats("native->downsampled", local)

        a, b = local.ravel(), ov_arr.ravel()
        ok = np.isfinite(a) & np.isfinite(b)
        if ok.sum() < 8:
            print("    (too few comparable pixels)")
            continue
        corr = float(np.corrcoef(a[ok], b[ok])[0, 1])
        bias = float(np.mean(b[ok] - a[ok]))
        rel = float(np.mean(np.abs(b[ok] - a[ok])) / (np.abs(a[ok]).mean() + 1e-9))
        verdict = "PLAUSIBLE" if corr > 0.95 and rel < 0.10 else "SUSPECT"
        print(f"    corr={corr:.4f}  mean_bias={bias:.3f}  mean_rel_err={rel:.4f}  -> {verdict}")


if __name__ == "__main__":
    targets = [
        ("USGS/SRTMGL1_003", "elevation"),
        ("NASA/NASADEM_HGT/001", "elevation"),
        ("JRC/GSW1_4/GlobalSurfaceWater", "occurrence"),
    ]
    if len(sys.argv) > 1:
        targets = [(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)]
    for asset, band_name in targets:
        try:
            probe(asset, band_name)
        except SystemExit as exc:
            print(f"  ABORTED: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  UNEXPECTED {type(exc).__name__}: {exc}")
