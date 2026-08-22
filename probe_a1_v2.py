"""Gate A1 (v2) - EEDAI overviews, tested over land with nodata masked.

v1 centred its window on the asset's pixel grid, which for a global DEM is open
ocean: every sample was the -32767 fill, so the correlation was degenerate and
proved nothing. This version targets a lon/lat with real relief, masks nodata,
and only reports a verdict when the ground truth actually varies.
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


def _activate() -> None:
    """Point GDAL's EEDA auth at the service-account key."""
    with open(KEY, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def _open(asset: str, band: str | None):
    """Open an EE asset through EEDAI with the block size pinned."""
    opts = [f"BLOCK_SIZE={BLOCK}"]
    if band:
        opts.append(f"BANDS={band}")
    ds = gdal.OpenEx(f"EEDAI:{asset}", gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR, open_options=opts)
    if ds is None:
        raise SystemExit(f"could not open {asset}: {gdal.GetLastErrorMsg()}")
    return ds


def _pixel_of(ds, lon: float, lat: float) -> tuple[int, int]:
    """Convert a lon/lat to this dataset's pixel coordinates."""
    gt = ds.GetGeoTransform()
    x = int((lon - gt[0]) / gt[1])
    y = int((lat - gt[3]) / gt[5])
    return x, y


def _read_blockwise(band, x0: int, y0: int, w: int, h: int) -> np.ndarray:
    """Read a window one 256-px block at a time (the read pyramids-eo trusts)."""
    out = np.full((h, w), np.nan, dtype="float64")
    for by in range(y0 // BLOCK * BLOCK, y0 + h, BLOCK):
        for bx in range(x0 // BLOCK * BLOCK, x0 + w, BLOCK):
            rx0, ry0 = max(bx, x0), max(by, y0)
            rx1, ry1 = min(bx + BLOCK, x0 + w), min(by + BLOCK, y0 + h)
            if rx1 <= rx0 or ry1 <= ry0:
                continue
            chunk = band.ReadAsArray(rx0, ry0, rx1 - rx0, ry1 - ry0)
            out[ry0 - y0 : ry1 - y0, rx0 - x0 : rx1 - x0] = chunk
    return out


def _describe(label: str, arr: np.ndarray, nodata: float | None) -> np.ndarray:
    """Print a summary and return the array with nodata replaced by NaN."""
    clean = arr.astype("float64").copy()
    if nodata is not None:
        clean[clean == nodata] = np.nan
    ok = clean[np.isfinite(clean)]
    print(
        f"    {label:26s} {arr.shape}  valid={ok.size / clean.size:.1%}  "
        f"mean={ok.mean():.1f}  std={ok.std():.1f}  "
        f"range=[{ok.min():.0f},{ok.max():.0f}]  uniq={np.unique(ok).size}"
        if ok.size
        else f"    {label:26s} {arr.shape}  NO VALID PIXELS"
    )
    return clean


def probe(asset: str, band_name: str | None, lon: float, lat: float, place: str) -> None:
    """Compare each overview level against a locally-downsampled native read."""
    print(f"\n{'=' * 78}\n{asset}   @ {place} ({lon}, {lat})\n{'=' * 78}")
    _activate()
    ds = _open(asset, band_name)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    print(f"  {ds.RasterXSize}x{ds.RasterYSize}  {gdal.GetDataTypeName(band.DataType)}  "
          f"nodata={nodata}  overviews={band.GetOverviewCount()}")

    side = BLOCK * 4
    px, py = _pixel_of(ds, lon, lat)
    x0 = max(0, min((px - side // 2) // BLOCK * BLOCK, ds.RasterXSize - side))
    y0 = max(0, min((py - side // 2) // BLOCK * BLOCK, ds.RasterYSize - side))
    print(f"  native window: {x0},{y0} {side}x{side}")

    native = _read_blockwise(band, x0, y0, side, side)
    native = _describe("native (block-wise)", native, nodata)
    valid = native[np.isfinite(native)]
    if valid.size == 0 or valid.std() < 1e-6:
        print("  -> ground truth is constant/empty here; pick another location.")
        return

    for i in range(band.GetOverviewCount()):
        ov = band.GetOverview(i)
        fx, fy = ds.RasterXSize / ov.XSize, ds.RasterYSize / ov.YSize
        ox0, oy0 = int(round(x0 / fx)), int(round(y0 / fy))
        ow, oh = int(round(side / fx)), int(round(side / fy))
        if ow < 4 or oh < 4:
            break
        print(f"\n  -- ov[{i}] factor {fx:.0f}  window {ox0},{oy0} {ow}x{oh}")
        try:
            raw = ov.ReadAsArray(ox0, oy0, ow, oh)
        except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
            print(f"    !! READ RAISED: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if raw is None:
            print("    !! READ RETURNED None")
            continue
        ov_arr = _describe(f"overview[{i}]", raw, nodata)

        fh_, fw_ = side // oh, side // ow
        trimmed = native[: oh * fh_, : ow * fw_]
        blocks = trimmed.reshape(oh, fh_, ow, fw_)
        with np.errstate(invalid="ignore"):
            local = np.nanmean(blocks, axis=(1, 3))
        _describe("native->downsampled", local, None)

        a, b = local.ravel(), ov_arr.ravel()
        ok = np.isfinite(a) & np.isfinite(b)
        if ok.sum() < 8 or np.std(a[ok]) < 1e-6:
            print(f"    (not comparable: {ok.sum()} overlapping valid px)")
            continue
        corr = float(np.corrcoef(a[ok], b[ok])[0, 1])
        rel = float(np.mean(np.abs(b[ok] - a[ok])) / (np.abs(a[ok]).mean() + 1e-9))
        flag = "OK" if corr > 0.95 and rel < 0.10 else "CORRUPT/MISMATCHED"
        print(f"    corr={corr:.4f}  mean_rel_err={rel:.4f}   -> {flag}")


if __name__ == "__main__":
    for asset, bnd, lon, lat, place in [
        ("USGS/SRTMGL1_003", "elevation", 86.925, 27.988, "Everest"),
        ("USGS/SRTMGL1_003", "elevation", 7.659, 45.976, "Matterhorn"),
        ("NASA/NASADEM_HGT/001", "elevation", 86.925, 27.988, "Everest"),
    ]:
        try:
            probe(asset, bnd, lon, lat, place)
        except SystemExit as exc:
            print(f"  ABORTED: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  UNEXPECTED {type(exc).__name__}: {str(exc)[:160]}")
