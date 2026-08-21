"""Live end-to-end test against Google Earth Engine.

Gated behind `-m e2e` (the file lives under `tests/gee/`, so the
package conftest also tags it `@pytest.mark.gee`). It needs real
service-account credentials in the environment — `GEE_SERVICE_ACCOUNT`
(the service-account email) and `GEE_SERVICE_KEY` (a path to the JSON
key file, or the key's JSON content) — and skips cleanly when they are
absent, so contributors without credentials (and fork PRs) are not
affected. The request is deliberately tiny: a single `USGS/SRTMGL1_003`
(SRTM elevation) tile over a ~0.05°×0.05° box at 90 m — a few KB, no
queue.
"""

from __future__ import annotations

import os

import pytest

from earthlens.earthlens import EarthLens
from earthlens.gee._eedai import eedai_available

pytestmark = [pytest.mark.e2e]

_SERVICE_ACCOUNT = os.environ.get("GEE_SERVICE_ACCOUNT")
_SERVICE_KEY = os.environ.get("GEE_SERVICE_KEY")

_skip_without_creds = pytest.mark.skipif(
    not (_SERVICE_ACCOUNT and _SERVICE_KEY),
    reason="GEE_SERVICE_ACCOUNT / GEE_SERVICE_KEY not set",
)


@_skip_without_creds
def test_live_srtm_download(tmp_path):
    """Download one tiny SRTM tile from Earth Engine via the facade."""
    el = EarthLens(
        data_source="gee",
        start="2000-02-11",
        end="2000-02-12",
        variables={"USGS/SRTMGL1_003": ["elevation"]},
        lat_lim=[29.95, 30.0],
        lon_lim=[31.25, 31.3],
        path=str(tmp_path),
        scale=90,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    paths = el.download(progress_bar=False)
    assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
    target = paths[0]
    assert target.is_file() and target.suffix == ".tif", f"unexpected output: {target}"
    assert target.stat().st_size > 0, "downloaded GeoTIFF is empty"

    from pyramids.dataset import Dataset

    raster = Dataset.read_file(str(target))
    assert raster.shape[0] == 1, f"expected 1 band, got shape {raster.shape}"
    assert raster.rows > 0 and raster.columns > 0, f"empty raster grid {raster.shape}"


def _download_srtm(tmp_path, engine: str):
    """Fetch the shared tiny SRTM tile through one engine, returning its path."""
    el = EarthLens(
        data_source="gee",
        start="2000-02-11",
        end="2000-02-12",
        variables={"USGS/SRTMGL1_003": ["elevation"]},
        lat_lim=[29.95, 30.0],
        lon_lim=[31.25, 31.3],
        path=str(tmp_path / engine),
        scale=90,
        engine=engine,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    paths = el.download(progress_bar=False)
    assert len(paths) == 1, f"{engine}: expected one GeoTIFF, got {paths}"
    return paths[0]


def _open_raster(path):
    """Return `(array, crs, bounds)` for a written raster."""
    import numpy as np
    from pyramids.dataset import Dataset

    raster = Dataset.read_file(str(path))
    values = np.asarray(raster.read_array(), dtype="float64")
    if values.ndim == 2:
        # A single-band read comes back as (rows, cols); normalise so callers
        # can index bands/rows/cols uniformly.
        values = values[np.newaxis, :, :]
    nodata = raster.no_data_value[0] if raster.no_data_value else None
    if nodata is not None:
        values = np.where(values == nodata, np.nan, values)
    return values, raster.epsg, raster.bbox


@_skip_without_creds
@pytest.mark.skipif(
    not eedai_available(), reason="the [eedai] extra (pyramids-eo) is not installed"
)
def test_live_srtm_eedai_matches_ee(tmp_path):
    """The EEDAI fast-path returns the same elevations as `getDownloadURL`.

    Fetches the identical tiny SRTM AOI twice — once forced onto Earth
    Engine, once forced onto the pyramids-eo reader — and compares the
    valid-pixel means. The two paths grid the AOI slightly differently
    (EE renders server-side; EEDAI reads block-aligned native pixels then
    warps), so the check is a physical-agreement tolerance, not pixel
    equality.
    """
    import numpy as np

    ee_path = _download_srtm(tmp_path, "ee")
    eedai_path = _download_srtm(tmp_path, "eedai")
    assert eedai_path.is_file(), f"EEDAI output missing: {eedai_path}"
    assert eedai_path.stat().st_size > 0, "EEDAI GeoTIFF is empty"

    ee_values, ee_epsg, ee_bbox = _open_raster(ee_path)
    eedai_values, eedai_epsg, eedai_bbox = _open_raster(eedai_path)

    assert eedai_epsg == ee_epsg, f"CRS differs: {eedai_epsg} vs {ee_epsg}"
    assert eedai_values.shape[0] == ee_values.shape[0], "band count differs"
    for got, want in zip(eedai_bbox, ee_bbox, strict=True):
        assert abs(got - want) < 1e-3, f"AOI differs: {eedai_bbox} vs {ee_bbox}"
    # The two engines grid independently — Earth Engine treats `scale` in a
    # geographic CRS as a uniform degree-equivalent, while the EEDAI grid is
    # sized for square metres on the ground — so the column counts legitimately
    # differ away from the equator. Latitude is the axis where both use the
    # same metres-per-degree, so that is where the resolutions must agree.
    span_m = (ee_bbox[3] - ee_bbox[1]) * 111_320.0
    for name, values in (("ee", ee_values), ("eedai", eedai_values)):
        rows = values.shape[1]
        assert abs(span_m / rows - 90.0) < 9.0, (
            f"{name} latitude resolution {span_m / rows:.1f} m is not ~90 m"
        )

    ee_finite = ee_values[np.isfinite(ee_values)]
    eedai_finite = eedai_values[np.isfinite(eedai_values)]
    assert ee_finite.size and eedai_finite.size, "a raster has no valid pixels"
    # Compare the elevation distributions rather than the means: a mean alone
    # would survive a shifted AOI or a transposed grid over flat terrain.
    for quantile in (0.05, 0.5, 0.95):
        got = float(np.quantile(eedai_finite, quantile))
        want = float(np.quantile(ee_finite, quantile))
        assert abs(got - want) < 5.0, (
            f"EEDAI q{quantile} {got:.2f} m differs from Earth Engine {want:.2f} m"
        )
