"""`jaxa-earth` protocol branch — STAC + COG via `jaxa.earth.je`.

Imports `jaxa.earth.je` lazily so the wider `earthlens.jaxa` surface stays
importable without the `[jaxa]` extra. The branch builds an
`ImageCollection`, walks the strict required filter chain
(`filter_date` → `filter_resolution` → `filter_bounds` → `select` →
`get_images`), and writes each returned numpy array to a north-up GeoTIFF
via `pyramids.dataset.Dataset.from_array(...).to_file(...)`.

The strict filter order was confirmed empirically against the installed
`jaxa.earth` 0.1.6 — calling the methods out of order raises explicit
"Please use method filterX before filterY" errors. Likewise the `Raster`
object's `latlim` / `lonlim` are 2-D arrays (`[[min, max]]`, not flat
lists) and `img` is a 4-D `(time, lat, lon, band)` tensor — the helpers
below normalise those into shapes pyramids accepts.

Orientation: every collection probed during A1 (AW3D30 PRISM v3.2, MODIS
LST re-hosts, GSMaP gauge products) is already north-up out of the API,
so the branch writes the array straight through — no flip helper. If a
future collection is observed south-up, add a per-collection rule at the
write site rather than reintroducing a guard that didn't guard.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
from earthlens.jaxa.catalog import Dataset


def _flatten_pair(arr_like) -> list[float]:
    """Flatten a `jaxa.earth` 2-D limit `[[min, max]]` into a flat list.

    The API returns `latlim` and `lonlim` as `numpy.ndarray` of shape
    `(1, 2)` (verified against `jaxa.earth` 0.1.6); flatten them so the
    geotransform builder can do plain min/max arithmetic.

    Args:
        arr_like: Anything `numpy.asarray` accepts.

    Returns:
        list[float]: The flattened, ordered values.
    """
    return [float(x) for x in np.asarray(arr_like).ravel().tolist()]


def _geo_tuple(
    latlim: list[float], lonlim: list[float], shape: tuple[int, int]
) -> tuple[float, float, float, float, float, float]:
    """Build a north-up GDAL geotransform 6-tuple.

    For a `(rows, cols)` array spanning `[lat_min, lat_max]` ×
    `[lon_min, lon_max]`, the standard north-up geotransform is
    `(lon_min, x_res, 0, lat_max, 0, -y_res)`. Verified round-trip
    against the AW3D30 sample tile.

    Args:
        latlim: Flat `[lat_min, lat_max]`.
        lonlim: Flat `[lon_min, lon_max]`.
        shape: `(rows, cols)` of the 2-D array.

    Returns:
        Six floats matching pyramids' `Dataset.from_array(geo_ref=GeoReference(geo=...))`.
    """
    rows, cols = shape
    lon_min, lon_max = min(lonlim), max(lonlim)
    lat_min, lat_max = min(latlim), max(latlim)
    x_res = (lon_max - lon_min) / cols
    y_res = (lat_max - lat_min) / rows
    return (lon_min, x_res, 0.0, lat_max, 0.0, -y_res)


def _slice_to_2d(img: np.ndarray) -> np.ndarray:
    """Reduce a 4-D `(time, lat, lon, band)` JAXA array to its 2-D `(lat, lon)`.

    The verified API returns a 4-D tensor even for single-date /
    single-band requests (shape `(1, H, W, 1)`); the GeoTIFF writer
    consumes a 2-D plane. Multi-date / multi-band stacks will need a
    different routing in a follow-on PR.

    Args:
        img: The 4-D API tensor.

    Returns:
        numpy.ndarray: 2-D `(lat, lon)` slice — `img[0, :, :, 0]`.

    Raises:
        ValueError: If `img` is not 4-D, has more than one entry on the
            time or band axis (multi-step requests are not handled here
            yet), or has a zero-pixel spatial extent (the requested
            bbox + ppu produced an empty array).
    """
    if img.ndim != 4:
        raise ValueError(
            f"expected a 4-D `(time, lat, lon, band)` array from jaxa.earth; "
            f"got shape {img.shape!r}."
        )
    n_time, n_lat, n_lon, n_band = img.shape
    if n_time != 1 or n_band != 1:
        raise ValueError(
            f"multi-time / multi-band JAXA tensors are not supported yet; "
            f"got shape {img.shape!r}."
        )
    if n_lat == 0 or n_lon == 0:
        raise ValueError(
            f"jaxa.earth returned a zero-pixel array (shape {img.shape!r}); "
            "the requested bbox + ppu combination yielded no pixels — widen "
            "the bbox or raise the resolution."
        )
    return img[0, :, :, 0]


def fetch_jaxa_earth(
    *,
    dataset: Dataset,
    space: SpatialExtent,
    time: TemporalExtent,
    resolution: float | None,
    bands: list[str] | None,
    out_dir: Path,
) -> list[Path]:
    """Fetch one `jaxa-earth` dataset and write AOI-cropped COGs.

    Walks the strict `ImageCollection` chain
    (`filter_date` → `filter_resolution` → `filter_bounds` → `select` →
    `get_images`), squeezes the returned 4-D tensor to a 2-D plane, and
    writes one GeoTIFF per band via
    `pyramids.dataset.Dataset.from_array`. Every collection probed
    during A1 returned a north-up array, so no flip is applied — add a
    per-collection rule at the write site if a south-up collection
    surfaces later.

    Args:
        dataset: The resolved catalog row (its `collection` and
            `default_band` drive the API call).
        space: The validated WGS84 bbox.
        time: The validated date window.
        resolution: `ppu` (pixels per degree) for `filter_resolution`;
            `None` uses the API's native resolution.
        bands: Override the dataset's `default_band`. `None` falls back
            to `dataset.default_band`.
        out_dir: Output directory (created if missing).

    Returns:
        list[Path]: One written GeoTIFF per band, named
            `<dataset.key>_<band>.tif`.

    Raises:
        ImportError: If the `jaxa.earth` SDK is not installed.
        ValueError: If `dataset.collection` is missing, neither `bands`
            nor `dataset.default_band` is set, or the API returns an
            array shape the writer cannot consume (multi-time /
            multi-band stack, zero pixels — see :func:`_slice_to_2d`).
    """
    try:
        from jaxa.earth import je  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "the 'jaxa.earth' SDK is required for the jaxa-earth protocol. "
            "Install it via the [jaxa] extra: pip install 'earthlens[jaxa]'."
        ) from exc
    from pyramids.dataset import Dataset as PyrDataset
    from pyramids.dataset import GeoReference

    if not dataset.collection:
        raise ValueError(
            f"dataset {dataset.key!r} has no collection — bad catalog row."
        )

    target_bands = (
        bands
        if bands is not None
        else ([dataset.default_band] if dataset.default_band else [])
    )
    if not target_bands:
        raise ValueError(
            f"no band selected for {dataset.key!r}: pass bands=[...] or set "
            "default_band in the catalog row."
        )

    out_dir.mkdir(parents=True, exist_ok=True)

    start_iso = time.start_date.isoformat()
    end_iso = time.end_date.isoformat()
    bbox = [space.west, space.south, space.east, space.north]

    written: list[Path] = []
    for band in target_bands:
        col = je.ImageCollection(collection=dataset.collection)
        col = col.filter_date(dlim=[start_iso, end_iso])
        if resolution is not None:
            col = col.filter_resolution(ppu=resolution)
        col = col.filter_bounds(bbox=bbox)
        col = col.select(band=band)
        result = col.get_images()
        raster = result.raster

        arr_2d = _slice_to_2d(np.asarray(raster.img))
        latlim = _flatten_pair(raster.latlim)
        lonlim = _flatten_pair(raster.lonlim)
        geo = _geo_tuple(latlim, lonlim, arr_2d.shape)

        ds_out = PyrDataset.from_array(arr_2d, geo_ref=GeoReference(geo=geo, epsg=4326))
        target = out_dir / f"{dataset.key}_{band}.tif"
        ds_out.to_file(str(target))
        written.append(target)
    return written
