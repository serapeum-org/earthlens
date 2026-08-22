"""Gate A1 - are the GDAL EEDAI driver's overviews actually corrupt?

pyramids-eo states in three places that they are, and materialises every read at
native resolution because of it. That belief is why earthlens caps the tiling
ratio and budgets native pixels, so it was worth settling by measurement.

Eight passes were needed, because each one refuted the hypothesis the previous
one raised. They are kept here as one function apiece, in order, because the
refutations *are* the result - the conclusion is not any single run but the list
of explanations that turned out to be wrong.

    python probe_a1_chain.py            # every pass, in order
    python probe_a1_chain.py v4         # one pass

The answer: **overviews are not structurally corrupt.** Every level from factor 2
to 256 reproduced a native downsample at `corr = 1.0000`. But an intermittent
silent corruption is real and remains unexplained - see `gate_a6_corruption_hunt`.
"""

from __future__ import annotations

import sys

import numpy as np
from _common import BLOCK, activate, blockwise, judge, matches, open_eedai
from osgeo import gdal

ASSET, BAND = "USGS/SRTMGL1_003", "elevation"
EVEREST, MATTERHORN = (86.925, 27.988), (7.659, 45.976)
BOUNDS, FILL = (-500.0, 9000.0), (-32768.0, -32767.0)
SIDE = BLOCK * 4


def _grid(dataset, lon: float, lat: float) -> tuple[int, int]:
    """Block-aligned window centred on a lon/lat, unclamped."""
    gt = dataset.GetGeoTransform()
    px, py = int((lon - gt[0]) / gt[1]), int((lat - gt[3]) / gt[5])
    return (px - SIDE // 2) // BLOCK * BLOCK, (py - SIDE // 2) // BLOCK * BLOCK


def _overview(dataset, level: int, x0: int, y0: int):
    """Map the native window onto one overview level."""
    ov = dataset.GetRasterBand(1).GetOverview(level)
    fx, fy = dataset.RasterXSize / ov.XSize, dataset.RasterYSize / ov.YSize
    return (
        ov,
        int(round(x0 / fx)),
        int(round(y0 / fy)),
        int(round(SIDE / fx)),
        int(round(SIDE / fy)),
        fx,
    )


def _downsample(native: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Block-mean the native window to an overview level's shape."""
    fh, fw = native.shape[0] // rows, native.shape[1] // cols
    trimmed = native[: rows * fh, : cols * fw]
    with np.errstate(invalid="ignore"):
        return np.nanmean(trimmed.reshape(rows, fh, cols, fw), axis=(1, 3))


def _agree(truth: np.ndarray, got: np.ndarray) -> tuple[float, float]:
    """Correlation and mean relative error against a ground truth."""
    a, b = truth.ravel(), got.ravel()
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 8 or np.std(a[ok]) < 1e-9:
        return float("nan"), float("nan")
    return (
        float(np.corrcoef(a[ok], b[ok])[0, 1]),
        float(np.mean(np.abs(b[ok] - a[ok])) / (np.abs(a[ok]).mean() + 1e-9)),
    )


def v1_centre_of_the_grid() -> None:
    """Pass 1 - inconclusive: the window landed in ocean.

    Centring on the asset's own pixel grid puts a global DEM's window in open
    ocean, so every sample was the nodata fill and every correlation degenerate.
    The lesson outlived the pass: a ground truth that does not vary makes a probe
    look clean while proving nothing.
    """
    activate()
    ds = open_eedai(ASSET, bands=[BAND])
    x0 = (ds.RasterXSize // 2 // BLOCK) * BLOCK
    y0 = (ds.RasterYSize // 2 // BLOCK) * BLOCK
    native = blockwise(ds.GetRasterBand(1), x0, y0, SIDE, SIDE)
    values = native[np.isfinite(native)]
    print(f"  window {x0},{y0} at the centre of the grid")
    print(
        f"  native: mean={values.mean():.1f} unique={np.unique(values).size} "
        f"-> {'degenerate, proves nothing' if np.unique(values).size < 2 else 'varies'}"
    )


def v2_over_land() -> None:
    """Pass 2 - the sighting: levels 0-2 returned impossible elevations."""
    activate()
    for place, (lon, lat) in (("Everest", EVEREST), ("Matterhorn", MATTERHORN)):
        ds = open_eedai(ASSET, bands=[BAND])
        x0, y0 = _grid(ds, lon, lat)
        native = blockwise(ds.GetRasterBand(1), x0, y0, SIDE, SIDE)
        ok, detail = judge(native, BOUNDS, FILL)
        print(f"\n  {place}: native {'ok ' if ok else 'BAD'} {detail}")
        for level in range(min(ds.GetRasterBand(1).GetOverviewCount(), 8)):
            ov, ox0, oy0, ow, oh, fx = _overview(ds, level, x0, y0)
            if ow < 4 or oh < 4:
                break
            try:
                arr = blockwise(ov, ox0, oy0, ow, oh)
            except RuntimeError as exc:
                print(f"    ov[{level}] factor {fx:.0f}: RAISED {str(exc)[:60]}")
                continue
            corr, rel = _agree(_downsample(native, oh, ow), arr)
            flag = "ok" if corr > 0.95 and rel < 0.10 else "CORRUPT"
            print(
                f"    ov[{level}] factor {fx:>4.0f}: corr={corr:+.4f} rel={rel:.3f}  {flag}"
            )


def v3_block_alignment() -> None:
    """Pass 3 - REFUTED: reading block-by-block does not help.

    Every exact read in pass 2 fitted inside one block and every bad one spanned
    several, which looked like the native multi-block fault pyramids-eo already
    works around. It is not: block-wise overview reads were equally corrupt.
    """
    activate()
    ds = open_eedai(ASSET, bands=[BAND])
    x0, y0 = _grid(ds, *EVEREST)
    native = blockwise(ds.GetRasterBand(1), x0, y0, SIDE, SIDE)
    print(f"  {'lvl':>3} {'factor':>6} {'one call':>22} {'block-wise':>22}")
    for level in range(3):
        ov, ox0, oy0, ow, oh, fx = _overview(ds, level, x0, y0)
        truth = _downsample(native, oh, ow)
        cells = []
        for label, reader in (
            ("one call", lambda: ov.ReadAsArray(ox0, oy0, ow, oh)),
            ("block-wise", lambda: blockwise(ov, ox0, oy0, ow, oh)),
        ):
            try:
                corr, rel = _agree(truth, np.asarray(reader(), dtype="float64"))
                cells.append(f"corr={corr:+.4f} rel={rel:.3f}")
            except RuntimeError as exc:
                cells.append(type(exc).__name__)
        print(f"  {level:>3} {fx:>6.0f} {cells[0]:>22} {cells[1]:>22}")


def v4_pixel_encoding() -> None:
    """Pass 4 - REFUTED: the wire encoding is not the fault.

    The bad values looked like a signed 16-bit raster through a byte-oriented
    codec, and `PIXEL_ENCODING` defaults to `AUTO` where PNG/JPEG are Byte-only.
    But all three encodings returned exact pixels, `AUTO` included.
    """
    activate()
    base = open_eedai(ASSET, bands=[BAND], encoding="NPY")
    x0, y0 = _grid(base, *EVEREST)
    native = blockwise(base.GetRasterBand(1), x0, y0, SIDE, SIDE)
    encodings = ["AUTO", "NPY", "GEO_TIFF"]
    print(f"  {'lvl':>3} " + " ".join(f"{e:>22}" for e in encodings))
    handles = {e: open_eedai(ASSET, bands=[BAND], encoding=e) for e in encodings}
    for level in range(3):
        cells = []
        for enc in encodings:
            ov, ox0, oy0, ow, oh, _ = _overview(handles[enc], level, x0, y0)
            corr, rel = _agree(
                _downsample(native, oh, ow), blockwise(ov, ox0, oy0, ow, oh)
            )
            cells.append(f"corr={corr:+.4f} rel={rel:.3f}")
        print(f"  {level:>3} " + " ".join(f"{c:>22}" for c in cells))


def v5_handle_state() -> None:
    """Pass 5 - REFUTED: a prior native read does not poison the handle."""
    activate()
    ref = open_eedai(ASSET, bands=[BAND], encoding="NPY")
    x0, y0 = _grid(ref, *EVEREST)
    for level in range(3):
        cold_ds = open_eedai(ASSET, bands=[BAND])
        ov, ox0, oy0, ow, oh, _ = _overview(cold_ds, level, x0, y0)
        cold = blockwise(ov, ox0, oy0, ow, oh)

        warm_ds = open_eedai(ASSET, bands=[BAND])
        blockwise(warm_ds.GetRasterBand(1), x0, y0, SIDE, SIDE)
        ov, ox0, oy0, ow, oh, _ = _overview(warm_ds, level, x0, y0)
        warm = blockwise(ov, ox0, oy0, ow, oh)
        print(f"  ov[{level}]: cold and warm agree = {matches(cold, warm)}")


def v6_lifetime_defect() -> None:
    """Pass 6 - a probe defect, kept as a warning, not a result.

    The native leg built its `Dataset` inside a lambda, so GDAL collected it
    before the band was read and all twelve trials raised. Reproduced here
    deliberately: a handle must outlive every read taken from it.
    """
    activate()
    ds = open_eedai(ASSET, bands=[BAND])
    x0, y0 = _grid(ds, *EVEREST)
    try:
        band = open_eedai(ASSET, bands=[BAND]).GetRasterBand(1)  # dataset dies here
        blockwise(band, x0, y0, BLOCK, BLOCK)
        print("  no error - the handle survived (GDAL build dependent)")
    except (TypeError, RuntimeError) as exc:
        print(
            f"  as expected, the collected handle broke the read: {type(exc).__name__}"
        )


def v7_repeatability() -> None:
    """Pass 7 - native reads are stable once the lifetime defect is fixed."""
    activate()
    hold = open_eedai(ASSET, bands=[BAND])
    x0, y0 = _grid(hold, *EVEREST)
    reference = blockwise(hold.GetRasterBand(1), x0, y0, SIDE, SIDE)
    bad = 0
    for trial in range(4):
        handle = open_eedai(ASSET, bands=[BAND])
        got = blockwise(handle.GetRasterBand(1), x0, y0, SIDE, SIDE)
        ok, _ = judge(got, BOUNDS, FILL)
        if not ok or not matches(got, reference):
            bad += 1
        print(f"  t{trial}: match={matches(got, reference)} healthy={ok}")
    print(f"  native mismatches: {bad}/4")


def v8_sustained_load() -> None:
    """Pass 8 - REFUTED: load does not provoke it either."""
    activate()
    ds = open_eedai(ASSET, bands=[BAND])
    x0, y0 = _grid(ds, *EVEREST)
    reference, bad = None, 0
    for round_ in range(8):
        handle = open_eedai(ASSET, bands=[BAND])
        ov, ox0, oy0, ow, oh, _ = _overview(handle, 0, x0, y0)
        arr = blockwise(ov, ox0, oy0, ow, oh)
        ok, detail = judge(arr, BOUNDS, FILL)
        if reference is None:
            reference = arr
        elif not ok or not matches(arr, reference):
            bad += 1
        print(f"  round {round_}: {'ok ' if ok else 'BAD'} {detail}")
    print(f"  bad rounds: {bad}/8")


PASSES = {
    "v1": v1_centre_of_the_grid,
    "v2": v2_over_land,
    "v3": v3_block_alignment,
    "v4": v4_pixel_encoding,
    "v5": v5_handle_state,
    "v6": v6_lifetime_defect,
    "v7": v7_repeatability,
    "v8": v8_sustained_load,
}


def main() -> None:
    """Run one named pass, or the whole chain in order."""
    gdal.UseExceptions()
    wanted = sys.argv[1:] or list(PASSES)
    for name in wanted:
        run = PASSES.get(name)
        if run is None:
            print(f"unknown pass {name!r}; choose from {', '.join(PASSES)}")
            continue
        print(
            f"\n{'=' * 74}\n{name}: {(run.__doc__ or '').splitlines()[0]}\n{'=' * 74}"
        )
        try:
            run()
        # The probe reports, it does not recover.
        except Exception as exc:  # noqa: BLE001
            print(f"  UNEXPECTED {type(exc).__name__}: {str(exc)[:130]}")


if __name__ == "__main__":
    main()
