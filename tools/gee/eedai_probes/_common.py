"""Shared mechanics for the EEDAI probes.

Every probe needs the same four things: service-account auth wired into GDAL's
EEDA config, an asset opened the way pyramids-eo opens it, a window read one
block per `RasterIO` call, and an oracle that can tell a healthy read from a
silently wrong one. Those live here so each probe is only the question it asks.

What deliberately does *not* live here is anything a probe is testing. The A1
chain, for example, keeps its own window arithmetic and its own read calls where
those were the variable under test - including the two defects the README records,
which are properties of how a probe used these helpers rather than of the helpers
themselves.

The oracle is the one the A5 soak settled on, and it judges three things because
the failure mode it exists to catch is silent - correct shape, no exception,
internally consistent, wrong numbers:

* **bounds**, after masking the band's fill value;
* **degeneracy**, because a constant raster passes every bounds test ever written;
* and, at the call site, **equality with a reference read**.

Fill values are passed in by the caller rather than read from the driver: EEDAI
reports `GetNoDataValue() == None` and a `GMF_ALL_VALID` mask even for bands that
plainly have a sentinel, so the value has to come from the Earth Engine catalog.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
from osgeo import gdal

gdal.UseExceptions()

#: EEDAI serves 256-px blocks by default. Gate A2 found every size from 128 to
#: 2048 read-correct, with cost tracking round-trips, but 256 stays the default
#: here because it is what pyramids-eo pins and therefore what the probes model.
BLOCK = 256

KEY = os.environ.get("GEE_SERVICE_KEY", "")


def activate(key_path: str | None = None) -> None:
    """Point GDAL's EEDA auth at a service-account key.

    Args:
        key_path: Path to the service-account JSON. Defaults to `GEE_SERVICE_KEY`.

    Raises:
        RuntimeError: No key path was given and the environment does not set one.
    """
    path = key_path or KEY
    if not path:
        raise RuntimeError("set GEE_SERVICE_KEY to a service-account JSON path")
    with open(path, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def open_eedai(
    asset: str,
    *,
    bands: list[str] | None = None,
    block: int = BLOCK,
    encoding: str | None = None,
):
    """Open an Earth Engine asset through EEDAI, as pyramids-eo does.

    Args:
        asset: An asset id, or a full `EEDAI:` connection string.
        bands: Band names to request, or `None` for whatever the driver returns.
        block: `BLOCK_SIZE` open option.
        encoding: `PIXEL_ENCODING` open option, or `None` to leave it defaulted.

    Returns:
        The opened `gdal.Dataset`.

    Raises:
        RuntimeError: The driver could not open the asset.
    """
    connection = asset if asset.startswith(("EEDAI:", "EEDA:")) else f"EEDAI:{asset}"
    options = [f"BLOCK_SIZE={block}"]
    if bands:
        options.append("BANDS=" + ",".join(bands))
    if encoding:
        options.append(f"PIXEL_ENCODING={encoding}")
    dataset = gdal.OpenEx(
        connection, gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR, open_options=options
    )
    if dataset is None:
        raise RuntimeError(f"{connection} would not open: {gdal.GetLastErrorMsg()}")
    return dataset


def blockwise(band, x0: int, y0: int, w: int, h: int, block: int = BLOCK) -> np.ndarray:
    """Read a window one block at a time, never crossing a block in one call.

    This is the read pyramids-eo's `_materialize` performs, and the only one it
    considers reliably correct on this driver.

    Args:
        band: The `gdal.Band` (or overview band) to read.
        x0: Left edge of the window, in that band's pixel space.
        y0: Top edge of the window, in that band's pixel space.
        w: Window width in pixels.
        h: Window height in pixels.
        block: Block size to step by.

    Returns:
        The window as a float array, unread pixels left as `NaN`.
    """
    out = np.full((h, w), np.nan, dtype="float64")
    for by in range(y0 // block * block, y0 + h, block):
        for bx in range(x0 // block * block, x0 + w, block):
            rx0, ry0 = max(bx, x0), max(by, y0)
            rx1, ry1 = min(bx + block, x0 + w), min(by + block, y0 + h)
            if rx1 <= rx0 or ry1 <= ry0:
                continue
            out[ry0 - y0 : ry1 - y0, rx0 - x0 : rx1 - x0] = band.ReadAsArray(
                rx0, ry0, rx1 - rx0, ry1 - ry0
            )
    return out


def window_for(
    dataset, lon: float, lat: float, side: int, block: int = BLOCK
) -> tuple[int, int] | None:
    """Return a block-aligned pixel window centred on a lon/lat.

    Args:
        dataset: The open dataset whose geotransform defines the grid.
        lon: Longitude of the window centre.
        lat: Latitude of the window centre.
        side: Window size in pixels, both axes.
        block: Alignment to snap the origin to.

    Returns:
        `(x0, y0)`, or `None` when the window would fall outside the asset.
    """
    gt = dataset.GetGeoTransform()
    px, py = int((lon - gt[0]) / gt[1]), int((lat - gt[3]) / gt[5])
    x0 = (px - side // 2) // block * block
    y0 = (py - side // 2) // block * block
    if (
        x0 < 0
        or y0 < 0
        or x0 + side > dataset.RasterXSize
        or y0 + side > dataset.RasterYSize
    ):
        return None
    return x0, y0


def observed(arr: np.ndarray, fill: tuple[float, ...] = ()) -> np.ndarray:
    """Return the finite pixels of a read, with fill sentinels removed.

    Args:
        arr: The array as read.
        fill: Sentinel values this band uses for "no observation".

    Returns:
        A flat array of the pixels that carry a real measurement.
    """
    out = arr.astype("float64").copy()
    for sentinel in fill:
        out[out == sentinel] = np.nan
    return out[np.isfinite(out)]


def judge(
    arr: np.ndarray,
    bounds: tuple[float, float],
    fill: tuple[float, ...] = (),
    *,
    require_variation: bool = True,
) -> tuple[bool, str]:
    """Judge a read on fill-masked bounds and, optionally, on degeneracy.

    Args:
        arr: The array as read.
        bounds: `(low, high)` the band physically cannot leave.
        fill: Sentinel values to mask before judging.
        require_variation: Whether a constant result counts as a failure. True
            for any window picked for relief; False where flatness is plausible.

    Returns:
        `(healthy, detail)` - `detail` describes the range when healthy, and
        names what failed when not.
    """
    values = observed(arr, fill)
    if values.size == 0:
        return False, "no observed pixels once the fill was masked"
    outside = int(((values < bounds[0]) | (values > bounds[1])).sum())
    if outside:
        return False, (
            f"{outside} px outside {bounds} "
            f"(observed [{values.min():.1f},{values.max():.1f}])"
        )
    if require_variation and float(values.std()) < 1e-6:
        return False, f"degenerate - every observed pixel is {values.flat[0]:.1f}"
    return True, (
        f"[{values.min():7.1f},{values.max():7.1f}] std={values.std():6.1f} "
        f"valid={values.size / arr.size:.0%}"
    )


def matches(arr: np.ndarray, reference: np.ndarray) -> bool:
    """Return whether a read equals a reference exactly, `NaN`s aligned.

    Args:
        arr: The read under test.
        reference: The read it must reproduce.

    Returns:
        `True` when every pixel agrees.
    """
    return bool(
        np.allclose(np.nan_to_num(arr), np.nan_to_num(reference), rtol=0, atol=1e-6)
    )
