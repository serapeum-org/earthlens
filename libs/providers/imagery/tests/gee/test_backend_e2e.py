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


def _download_srtm(tmp_path, engine: str, crs: str | None = None):
    """Fetch the shared tiny SRTM tile through one engine.

    Args:
        tmp_path: Directory to write under; the engine (and CRS) name is
            appended so two fetches' outputs never collide.
        engine: The `engine=` value to force for this fetch.
        crs: The output CRS, or `None` for the backend default (EPSG:4326).

    Returns:
        The written GeoTIFF's path.
    """
    label = engine if crs is None else f"{engine}-{crs.replace(':', '_')}"
    kwargs = {} if crs is None else {"crs": crs}
    el = EarthLens(
        data_source="gee",
        start="2000-02-11",
        end="2000-02-12",
        variables={"USGS/SRTMGL1_003": ["elevation"]},
        lat_lim=[29.95, 30.0],
        lon_lim=[31.25, 31.3],
        path=str(tmp_path / label),
        scale=90,
        engine=engine,
        **kwargs,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    paths = el.download(progress_bar=False)
    assert len(paths) == 1, f"{engine}: expected one GeoTIFF, got {paths}"
    return paths[0]


def _record_plan(plan, sink):
    """Record how a plan tiled, then hand it back unchanged."""
    sink.append(plan.tiles if plan.tile_size is not None else 0)
    return plan


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
    Engine, once forced onto the pyramids-eo reader — and compares their
    CRS, bounds, per-engine resolution and elevation distribution. The two
    paths grid the AOI differently
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
    assert ee_finite.size, "the Earth Engine raster has no valid pixels"
    assert eedai_finite.size, "the EEDAI raster has no valid pixels"
    # Compare the elevation distributions rather than the means: a mean alone
    # would survive a shifted AOI or a transposed grid over flat terrain.
    for quantile in (0.05, 0.5, 0.95):
        got = float(np.quantile(eedai_finite, quantile))
        want = float(np.quantile(ee_finite, quantile))
        assert abs(got - want) < 5.0, (
            f"EEDAI q{quantile} {got:.2f} m differs from Earth Engine {want:.2f} m"
        )


@_skip_without_creds
@pytest.mark.skipif(
    not eedai_available(), reason="the [eedai] extra (pyramids-eo) is not installed"
)
def test_live_srtm_tiled_read_matches_single_pass(tmp_path, monkeypatch):
    """A tiled EEDAI read returns the same raster as a single-pass one.

    Tiling only engages for windows too large to materialise at once, and a
    genuinely oversized AOI would pull an enormous native-resolution read.
    The single-read budget is lowered instead, so the same tiny AOI takes the
    streaming path against the live service for a few KB.
    """
    import numpy as np

    from earthlens.gee import backend as backend_module

    single = _download_srtm(tmp_path / "single", "eedai")
    # Shrink the single-pass budget until this tiny AOI cannot be read in one
    # pass. Nothing else is patched: the reader's real 3-px window padding
    # still applies, so the tile maths under test is the shipped one.
    monkeypatch.setattr(backend_module, "_EEDAI_MAX_PIXELS", 20_000)
    tiled_calls: list[int] = []
    original_plan = backend_module.GEE._eedai_plan
    monkeypatch.setattr(
        backend_module.GEE,
        "_eedai_plan",
        lambda self, var_info, band_count: _record_plan(
            original_plan(self, var_info, band_count), tiled_calls
        ),
    )
    tiled = _download_srtm(tmp_path / "tiled", "eedai")
    # Without this the test would silently compare two single-pass reads and
    # pass, proving nothing about tiling.
    assert tiled_calls, "no read was planned at all"
    assert all(count > 1 for count in tiled_calls), (
        f"the read was not cut into multiple tiles: tile counts {tiled_calls}"
    )

    assert tiled.is_file(), f"tiled output missing: {tiled}"
    assert tiled.read_bytes() != b"", "the tiled output is empty"
    assert not list(tiled.parent.glob("*.partial*")), "staged tiles left behind"

    single_values, single_epsg, single_bbox = _open_raster(single)
    tiled_values, tiled_epsg, tiled_bbox = _open_raster(tiled)
    assert tiled_epsg == single_epsg
    assert tiled_values.shape == single_values.shape
    for got, want in zip(tiled_bbox, single_bbox, strict=True):
        assert abs(got - want) < 1e-6, f"AOI moved: {tiled_bbox} vs {single_bbox}"
    both_finite = np.isfinite(single_values) & np.isfinite(tiled_values)
    assert both_finite.any(), "no comparable pixels"
    assert np.allclose(
        tiled_values[both_finite], single_values[both_finite], equal_nan=True
    ), "the tiled mosaic differs from the single-pass read"


@_skip_without_creds
@pytest.mark.skipif(
    not eedai_available(), reason="the [eedai] extra (pyramids-eo) is not installed"
)
def test_live_srtm_projected_crs_reads_the_same_ground(tmp_path):
    """A projected-CRS EEDAI read returns the same elevations as an EPSG:4326 one.

    The Cairo AOI is read through the reader twice — once in EPSG:4326, once in
    UTM zone 36N (EPSG:32636) — and their elevation distributions are compared.
    If the projected read passed lon/lat where the reader wanted metres it would
    land in the ocean (a flat nodata/near-zero raster); matching elevations prove
    the AOI was reprojected into the CRS before reading.
    """
    import numpy as np

    latlon_path = _download_srtm(tmp_path, "eedai")
    utm_path = _download_srtm(tmp_path, "eedai", crs="EPSG:32636")
    assert utm_path.is_file() and utm_path.stat().st_size > 0, "UTM output missing"

    latlon_values, _latlon_epsg, _latlon_bbox = _open_raster(latlon_path)
    utm_values, utm_epsg, utm_bbox = _open_raster(utm_path)

    assert utm_epsg == 32636, f"projected read wrote EPSG:{utm_epsg}, not 32636"
    assert utm_bbox[0] > 100_000, f"UTM bounds are not metric: {utm_bbox}"

    latlon_finite = latlon_values[np.isfinite(latlon_values)]
    utm_finite = utm_values[np.isfinite(utm_values)]
    assert latlon_finite.size and utm_finite.size, "a raster has no valid pixels"
    # Cairo sits ~20-70 m above sea level; a wrong-ground read would be flat or
    # negative. Compare the distributions, not the means.
    for quantile in (0.05, 0.5, 0.95):
        got = float(np.quantile(utm_finite, quantile))
        want = float(np.quantile(latlon_finite, quantile))
        assert abs(got - want) < 8.0, (
            f"UTM q{quantile} {got:.2f} m differs from EPSG:4326 {want:.2f} m"
        )
