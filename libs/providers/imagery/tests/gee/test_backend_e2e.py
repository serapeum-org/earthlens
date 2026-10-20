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

import datetime as dt
import functools
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


def _counted_estimate(real, calls, asset_id, **kwargs):
    """Record one scene-discovery call, then make it for real.

    Bound with `functools.partial` over the reader's own function, so the query
    still goes to the live catalog and only its count and arguments are
    observed.

    Args:
        real: The reader's `estimate_earthengine_cost`.
        calls: The list the call's keyword arguments are appended to.
        asset_id: The asset being discovered.
        **kwargs: The discovery window, AOI and filter.

    Returns:
        Whatever the reader returned.
    """
    calls.append(kwargs)
    return real(asset_id, **kwargs)


def _record_discovery(monkeypatch):
    """Instrument the reader's scene discovery for the duration of a test.

    Args:
        monkeypatch: The pytest fixture that undoes the patch afterwards.

    Returns:
        list[dict]: Appended to, in order, as discovery queries are made.
    """
    from earthlens.gee._eedai import import_earthengine_reader

    reader = import_earthengine_reader()
    calls: list[dict] = []
    monkeypatch.setattr(
        reader,
        "estimate_earthengine_cost",
        functools.partial(_counted_estimate, reader.estimate_earthengine_cost, calls),
    )
    return calls


def _s2_backend(tmp_path, property_filter):
    """A GEE backend over one small Sentinel-2 window, with the given filter.

    Args:
        tmp_path: The test's output directory.
        property_filter: The `property_filter` string, or `None`.

    Returns:
        The `GEE` backend the facade bound, authenticated against the live
        service account.
    """
    el = EarthLens(
        data_source="gee",
        start="2024-01-01",
        end="2024-12-30",
        variables={"COPERNICUS/S2_SR_HARMONIZED": ["B4"]},
        lat_lim=[29.98, 30.0],
        lon_lim=[31.28, 31.3],
        path=str(tmp_path),
        scale=10,
        engine="eedai",
        property_filter=property_filter,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    return el.datasource


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
    original_plan = backend_module.GEE._eedai_single_image_plan
    monkeypatch.setattr(
        backend_module.GEE,
        "_eedai_single_image_plan",
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
    assert utm_path.is_file(), "UTM output missing"
    assert utm_path.stat().st_size > 0, "UTM output is empty"

    latlon_values, _latlon_epsg, _latlon_bbox = _open_raster(latlon_path)
    utm_values, utm_epsg, utm_bbox = _open_raster(utm_path)

    assert utm_epsg == 32636, f"projected read wrote EPSG:{utm_epsg}, not 32636"
    assert utm_bbox[0] > 100_000, f"UTM bounds are not metric: {utm_bbox}"

    latlon_finite = latlon_values[np.isfinite(latlon_values)]
    utm_finite = utm_values[np.isfinite(utm_values)]
    assert latlon_finite.size, "the lat/lon raster has no valid pixels"
    assert utm_finite.size, "the UTM raster has no valid pixels"
    # Cairo sits ~20-70 m above sea level; a wrong-ground read would be flat or
    # negative. Compare the distributions, not the means.
    for quantile in (0.05, 0.5, 0.95):
        got = float(np.quantile(utm_finite, quantile))
        want = float(np.quantile(latlon_finite, quantile))
        assert abs(got - want) < 8.0, (
            f"UTM q{quantile} {got:.2f} m differs from EPSG:4326 {want:.2f} m"
        )


def _download_pinned(tmp_path, label, lon_min, crs_transform, engine="pixels"):
    """Fetch the shared SRTM window on an explicitly pinned grid.

    Args:
        tmp_path: Directory to write under; `label` keeps two fetches apart.
        label: A name for this fetch's output directory.
        lon_min: The AOI's western edge, varied to move the window off the
            pinned grid's origin.
        crs_transform: The 6-element transform to pin the grid with.
        engine: The engine to force.

    Returns:
        The written GeoTIFF's path.
    """
    el = EarthLens(
        data_source="gee",
        start="2000-02-11",
        end="2000-02-12",
        variables={"USGS/SRTMGL1_003": ["elevation"]},
        lat_lim=[29.95, 30.0],
        lon_lim=[lon_min, 31.3],
        path=str(tmp_path / label),
        scale=90,
        engine=engine,
        crs_transform=crs_transform,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    paths = el.download(progress_bar=False)
    assert len(paths) == 1, f"{label}: expected one GeoTIFF, got {paths}"
    return paths[0]


@_skip_without_creds
def test_live_srtm_pixels_matches_ee(tmp_path):
    """`computePixels` returns the same elevations as `getDownloadURL`.

    The comparison Task 1.13 asks for before anything considers preferring the
    newer API: same AOI, same CRS, same distribution. Both engines evaluate the
    graph server-side on the same grid convention, so unlike the EEDAI
    comparison this one can be tight.
    """
    import numpy as np

    ee_path = _download_srtm(tmp_path, "ee")
    pixels_path = _download_srtm(tmp_path, "pixels")
    assert pixels_path.is_file(), f"pixels output missing: {pixels_path}"
    assert pixels_path.stat().st_size > 0, "pixels GeoTIFF is empty"

    ee_values, ee_epsg, ee_bbox = _open_raster(ee_path)
    pixels_values, pixels_epsg, pixels_bbox = _open_raster(pixels_path)

    assert pixels_epsg == ee_epsg, f"CRS differs: {pixels_epsg} vs {ee_epsg}"
    assert pixels_values.shape[0] == ee_values.shape[0], "band count differs"
    for got, want in zip(pixels_bbox, ee_bbox, strict=True):
        assert abs(got - want) < 1e-3, f"AOI differs: {pixels_bbox} vs {ee_bbox}"

    ee_finite = ee_values[np.isfinite(ee_values)]
    pixels_finite = pixels_values[np.isfinite(pixels_values)]
    assert ee_finite.size, "the getDownloadURL raster has no valid pixels"
    assert pixels_finite.size, "the computePixels raster has no valid pixels"
    for quantile in (0.05, 0.5, 0.95):
        got = float(np.quantile(pixels_finite, quantile))
        want = float(np.quantile(ee_finite, quantile))
        assert abs(got - want) < 5.0, (
            f"computePixels q{quantile} {got:.2f} m differs from getDownloadURL "
            f"{want:.2f} m"
        )


@_skip_without_creds
def test_live_pixels_composites_server_side(tmp_path):
    """A reduced collection through `computePixels` matches Earth Engine's reduce.

    The regression this pins is the one wiring the transport exposed: a plan
    accepted by this engine was once served from the EEDAI reader's stored
    pixels, which silently dropped the reduction. A single scene would not match
    the ten-day mean.
    """
    import numpy as np

    ee_path = _download_chirps(tmp_path, "ee")
    pixels_path = _download_chirps(tmp_path, "pixels")
    ee_values, ee_epsg, _ee_bbox = _open_raster(ee_path)
    pixels_values, pixels_epsg, _pixels_bbox = _open_raster(pixels_path)
    assert pixels_epsg == ee_epsg, f"CRS differs: {pixels_epsg} vs {ee_epsg}"

    ee_finite = ee_values[np.isfinite(ee_values)]
    pixels_finite = pixels_values[np.isfinite(pixels_values)]
    assert ee_finite.size, "the Earth Engine composite has no valid pixels"
    assert pixels_finite.size, "the computePixels composite has no valid pixels"
    assert float(np.nanstd(ee_finite)) > 0.5, (
        "the reference composite is nearly flat, so it cannot tell a correct "
        "composite from a dropped reduction"
    )
    for quantile in (0.05, 0.5, 0.95):
        got = float(np.quantile(pixels_finite, quantile))
        want = float(np.quantile(ee_finite, quantile))
        assert abs(got - want) < 2.0, (
            f"computePixels composite q{quantile} {got:.3f} mm differs from "
            f"Earth Engine {want:.3f} mm"
        )


@_skip_without_creds
def test_live_high_volume_endpoint_serves_the_same_request(tmp_path):
    """The high-volume host is a host, not a different answer.

    Opting in changes which endpoint the session is opened against, so the
    thing worth asserting live is that the request still succeeds there and
    returns the same pixels.
    """
    import numpy as np

    el = EarthLens(
        data_source="gee",
        start="2000-02-11",
        end="2000-02-12",
        variables={"USGS/SRTMGL1_003": ["elevation"]},
        lat_lim=[29.95, 30.0],
        lon_lim=[31.25, 31.3],
        path=str(tmp_path / "high-volume"),
        scale=90,
        engine="pixels",
        high_volume=True,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    (high_volume_path,) = el.download(progress_bar=False)
    assert high_volume_path.is_file(), "the high-volume fetch wrote nothing"

    regular_path = _download_srtm(tmp_path, "pixels")
    high_volume_values, _epsg, _bbox = _open_raster(high_volume_path)
    regular_values, _regular_epsg, _regular_bbox = _open_raster(regular_path)
    assert high_volume_values.shape == regular_values.shape, "grids differ"
    assert np.allclose(high_volume_values, regular_values, equal_nan=True), (
        "the high-volume endpoint returned different pixels"
    )


@_skip_without_creds
@pytest.mark.parametrize("engine", ["pixels", "ee"])
def test_live_pinned_grids_share_their_pixel_boundaries(tmp_path, engine):
    """Two different AOIs, one `crs_transform`, one grid.

    The point of pinning: without it each output's origin follows its own AOI,
    so differencing two windows needs a resample first. With it both origins sit
    an integer number of pixels from the pinned one.

    Both engines that accept a transform are exercised, because they send it
    differently - `computePixels` as the request grid's `affineTransform`,
    `getDownloadURL` as `crs_transform` in place of `scale`, which Earth Engine
    rejects if both arrive.
    """
    pixel = 0.001
    origin = (pixel, 0.0, 31.2, 0.0, -pixel, 30.0)
    wide = _download_pinned(tmp_path, f"wide-{engine}", 31.25, origin, engine)
    narrow = _download_pinned(tmp_path, f"narrow-{engine}", 31.2523, origin, engine)

    _wide_values, _wide_epsg, wide_bbox = _open_raster(wide)
    _narrow_values, _narrow_epsg, narrow_bbox = _open_raster(narrow)
    assert wide_bbox[0] != narrow_bbox[0], (
        "the two AOIs reduced to the same window, so the test proves nothing"
    )
    for name, bbox in (("wide", wide_bbox), ("narrow", narrow_bbox)):
        offset = (bbox[0] - origin[2]) / pixel
        assert abs(offset - round(offset)) < 1e-3, (
            f"{name} west edge {bbox[0]} is {offset:.3f} pixels from the pinned "
            "origin, so it did not land on the pinned grid"
        )


def _download_wide_strip(tmp_path, engine: str, **overrides):
    """Fetch a composited strip wider than Earth Engine's synchronous cap.

    A 360-degree strip 0.1 degrees tall at 1 km is about 39,000 x 11 px: over the
    32768-px per-axis limit on the long axis, and barely over a megabyte of
    pixels, so it tests the cap rather than the bandwidth.

    Args:
        tmp_path: Directory to write under.
        engine: The engine to force.
        **overrides: Extra `EarthLens` keyword arguments.

    Returns:
        The written GeoTIFF's path.
    """
    el = EarthLens(
        data_source="gee",
        start="2020-06-01",
        end="2020-06-03",
        variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
        temporal_resolution="raw",
        lat_lim=[9.9, 10.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path / f"strip-{engine}"),
        scale=1000,
        engine=engine,
        **overrides,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    paths = el.download(progress_bar=False)
    assert len(paths) == 1, f"{engine}: expected one GeoTIFF, got {paths}"
    return paths[0]


@_skip_without_creds
def test_live_pixels_is_capped_per_axis_like_the_thumbnail_path(tmp_path):
    """Earth Engine caps a `computePixels` grid too, and this is the evidence.

    Task 1.13 expected this engine to serve an oversize composite unsplit. It
    does not: the live API refuses the request. What the backend must do is own
    that refusal rather than letting a raw Earth Engine error out, which is what
    this asserts.
    """
    with pytest.raises(ValueError, match="same per-axis cap"):
        _download_wide_strip(tmp_path, "pixels")


@_skip_without_creds
def test_live_pixels_serves_that_request_when_it_may_split_it(tmp_path):
    """And with `auto_split=True` the oversize composite does succeed.

    Which is the acceptance criterion in substance: a composited AOI wider than
    32768 px comes back as one raster, tiled and mosaicked rather than refused.
    """
    import numpy as np

    written = _download_wide_strip(tmp_path, "pixels", auto_split=True)
    assert written.is_file(), f"the mosaic was not written: {written}"
    assert written.stat().st_size > 0, "the mosaic is empty"

    values, _epsg, bbox = _open_raster(written)
    assert values.shape[2] > 32768, (
        f"the mosaic came back {values.shape[2]} px wide, so it did not "
        "exercise the cap"
    )
    assert abs((bbox[2] - bbox[0]) - 360.0) < 1.0, f"not a global strip: {bbox}"
    assert np.isfinite(values).any(), "the mosaic has no valid pixels"


@_skip_without_creds
def test_live_get_download_url_still_refuses_that_request(tmp_path):
    """The other half: the cap is real, and `auto_split=False` still reports it.

    Without this the test above could pass for the wrong reason - a cap that had
    quietly stopped applying to either engine.
    """
    with pytest.raises(ValueError, match="32,768|auto_split"):
        _download_wide_strip(tmp_path, "ee")


@_skip_without_creds
def test_live_a_materialised_download_stacks_into_a_datacube(tmp_path):
    """Task 1.8's `.collection()`, against real rasters.

    Two monthly buckets, so the collection has a time axis to build; the unit
    test can only check which constructor is called and with what.
    """
    el = EarthLens(
        data_source="gee",
        start="2020-06-01",
        end="2020-07-31",
        variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
        temporal_resolution="monthly",
        lat_lim=[9.9, 10.1],
        lon_lim=[76.3, 76.5],
        path=str(tmp_path / "cube"),
        scale=5566,
        engine="ee",
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    result = el.download(progress_bar=False, materialize=True)
    assert len(result.paths) == 2, f"expected two buckets, got {result.paths}"
    cube = result.collection()
    try:
        assert len(cube) == 2, f"the cube did not stack both buckets: {len(cube)}"
    finally:
        result.close()


@_skip_without_creds
def test_live_nodata_reaches_the_pixels_and_the_declaration(tmp_path):
    """`nodata=` is honoured end to end, which it was not when it shipped.

    The first implementation put `nodata` on the `computePixels` grid, where no
    such field exists, so every request carrying it was refused with
    `Unknown name "nodata" at 'grid'`. Nothing caught it: the offline fake
    accepts any body, and no live test set the argument. This is that test.

    The AOI straddles the Kerala coast, and CHIRPS is land-only, so the Arabian
    Sea half is masked and the fill value has to appear.
    """
    import numpy as np

    el = EarthLens(
        data_source="gee",
        start="2020-06-01",
        end="2020-06-03",
        variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
        temporal_resolution="raw",
        lat_lim=[9.9, 10.2],
        lon_lim=[75.5, 76.3],
        path=str(tmp_path / "nodata"),
        scale=5566,
        engine="pixels",
        nodata=-9999,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    (written,) = el.download(progress_bar=False)

    from pyramids.dataset import Dataset

    raster = Dataset.read_file(str(written))
    try:
        declared = raster.no_data_value
        values = np.asarray(raster.read_array(), dtype="float64")
    finally:
        raster.close()
    assert declared[0] == -9999, f"the raster declares {declared}, not -9999"
    assert int(np.sum(values == -9999)) > 0, (
        "no pixel carries the fill value, so the unmask did not reach the expression"
    )
    assert float(np.max(values)) > 0.0, "the AOI has no valid rainfall left"


def _download_chirps(tmp_path, engine: str):
    """Fetch a tiny CHIRPS precipitation composite through one engine."""
    el = EarthLens(
        data_source="gee",
        start="2020-06-01",
        end="2020-06-10",
        variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
        temporal_resolution="raw",
        # Monsoon Kerala, not arid Cairo: a comparison over ~0 mm everywhere
        # passes whatever the reducer does, so the AOI has to carry real rain.
        lat_lim=[9.9, 10.1],
        lon_lim=[76.3, 76.5],
        path=str(tmp_path / engine),
        scale=5566,
        engine=engine,
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    paths = el.download(progress_bar=False)
    assert len(paths) == 1, f"{engine}: expected one GeoTIFF, got {paths}"
    return paths[0]


@_skip_without_creds
@pytest.mark.skipif(
    not eedai_available(), reason="the [eedai] extra (pyramids-eo) is not installed"
)
@pytest.mark.xfail(
    reason="live EEDAI client-side reduce is blocked on pyramids#1349 (in-memory "
    "collection vs file-backed reduce_time); tracked by earthlens#1336",
    strict=False,
)
def test_live_chirps_collection_eedai_matches_ee(tmp_path):
    """A collection composited through the reader matches Earth Engine's reduce.

    CHIRPS DAILY is an ImageCollection; the default `mean` reducer collapses the
    ten-day window to one image. Earth Engine reduces server-side, the reader
    downloads the scenes and reduces client-side — so the check is a physical
    agreement of the precipitation distribution, proving C1 routes the composite
    correctly rather than reading a single scene.
    """
    import numpy as np

    ee_path = _download_chirps(tmp_path, "ee")
    eedai_path = _download_chirps(tmp_path, "eedai")
    assert eedai_path.is_file(), "EEDAI output missing"
    assert eedai_path.stat().st_size > 0, "EEDAI output is empty"

    ee_values, ee_epsg, _ee_bbox = _open_raster(ee_path)
    eedai_values, eedai_epsg, _eedai_bbox = _open_raster(eedai_path)
    assert eedai_epsg == ee_epsg, f"CRS differs: {eedai_epsg} vs {ee_epsg}"

    ee_finite = ee_values[np.isfinite(ee_values)]
    eedai_finite = eedai_values[np.isfinite(eedai_values)]
    assert ee_finite.size, "the Earth Engine composite has no valid pixels"
    assert eedai_finite.size, "the EEDAI composite has no valid pixels"
    # Without real variance the percentile comparison below is satisfied by any
    # reducer, by a single scene, and by a wrong date window alike.
    assert float(np.nanstd(ee_finite)) > 0.5, (
        f"the reference composite is nearly flat (std={np.nanstd(ee_finite):.3f} mm); "
        "this AOI/season cannot distinguish a correct composite from a broken one"
    )
    for quantile in (0.05, 0.5, 0.95):
        got = float(np.quantile(eedai_finite, quantile))
        want = float(np.quantile(ee_finite, quantile))
        assert abs(got - want) < 2.0, (
            f"EEDAI composite q{quantile} {got:.3f} mm differs from Earth Engine "
            f"{want:.3f} mm"
        )


@_skip_without_creds
@pytest.mark.skipif(
    not eedai_available(), reason="the [eedai] extra (pyramids-eo) is not installed"
)
def test_live_property_filter_narrows_the_selection_through_the_backend(
    tmp_path, monkeypatch
):
    """A stricter cloud threshold must select fewer scenes, driven by earthlens.

    Calling the reader's estimator directly would only prove that *upstream's*
    filter narrows a selection, which is upstream's test to own — a regression
    that dropped `property_filter` on the way out of this backend would leave it
    green. So the string is set on the constructor and the counts are read off
    the backend's own routing decision, with the queries it makes recorded.
    """
    start, end = dt.datetime(2024, 1, 1), dt.datetime(2024, 12, 31)
    counts = {}
    calls = _record_discovery(monkeypatch)
    for label, property_filter in (
        ("unfiltered", None),
        ("permissive", "CLOUDY_PIXEL_PERCENTAGE < 95"),
        ("strict", "CLOUDY_PIXEL_PERCENTAGE < 5"),
    ):
        gee = _s2_backend(tmp_path, property_filter)
        var_info = gee.catalog.get_dataset("COPERNICUS/S2_SR_HARMONIZED")
        plan = gee._eedai_collection_fits(var_info, 1, start, end)
        assert plan.can_serve, f"{label}: {plan.reason}"
        counts[label] = plan.tiles
    assert [c.get("property_filter") for c in calls] == [
        None,
        "CLOUDY_PIXEL_PERCENTAGE < 95",
        "CLOUDY_PIXEL_PERCENTAGE < 5",
    ], f"the backend did not forward each filter to discovery: {calls}"
    assert counts["unfiltered"] > 0, "the AOI/window found no Sentinel-2 scenes at all"
    assert counts["strict"] < counts["permissive"] <= counts["unfiltered"], (
        f"the filter did not narrow the selection: {counts}"
    )


@_skip_without_creds
@pytest.mark.skipif(
    not eedai_available(), reason="the [eedai] extra (pyramids-eo) is not installed"
)
@pytest.mark.xfail(
    reason="live EEDAI client-side reduce is blocked on pyramids#1349 (in-memory "
    "collection vs file-backed reduce_time); tracked by earthlens#1336",
    strict=False,
)
def test_live_monthly_buckets_are_disjoint_and_cost_one_query_each(
    tmp_path, monkeypatch
):
    """Monthly buckets each write their own month, at one catalog query apiece.

    The single-bucket comparison never exercises the multi-bucket path, which is
    exactly where an inclusive/exclusive mix-up would make consecutive buckets
    overlap. Discovery is per bucket by construction — each has its own window —
    so the count is pinned here rather than assumed: a routing change that
    re-discovered per band, or per read attempt, would show up as more.
    """
    calls = _record_discovery(monkeypatch)
    el = EarthLens(
        data_source="gee",
        start="2020-06-01",
        end="2020-07-31",
        variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
        temporal_resolution="monthly",
        reducer="mean",
        lat_lim=[9.9, 10.1],
        lon_lim=[76.3, 76.5],
        path=str(tmp_path),
        scale=5566,
        engine="eedai",
    ).authenticate(service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY)
    paths = el.download(progress_bar=False)
    names = sorted(p.name for p in paths)
    assert names == [
        "UCSB-CHG_CHIRPS_DAILY_precipitation_20200601.tif",
        "UCSB-CHG_CHIRPS_DAILY_precipitation_20200701.tif",
    ], names
    # Two months of monsoon rain differ; identical rasters would mean both
    # buckets read the same window.
    import numpy as np

    june, _e, _b = _open_raster(paths[0])
    july, _e2, _b2 = _open_raster(paths[1])
    assert not np.allclose(np.nan_to_num(june), np.nan_to_num(july)), (
        "the two monthly buckets produced identical rasters"
    )
    assert len(calls) == 2, (
        f"two buckets must cost exactly two discovery queries: {len(calls)}"
    )
    assert [(c["start"], c["end"]) for c in calls] == [
        ("2020-06-01", "2020-06-30"),
        ("2020-07-01", "2020-07-31"),
    ], calls


def _rising_scene(value, date):
    """A global constant image stamped with its acquisition time."""
    import ee

    return (
        ee.Image(ee.Number(value))
        .rename("v")
        .set("system:time_start", ee.Date(date).millis())
    )


@_skip_without_creds
@pytest.mark.parametrize("reducer_name", ["trend", "sens_slope"])
def test_live_trend_is_positive_over_a_rising_stack(reducer_name):
    """The EE trend path fits a real slope over time: a rising stack is positive."""
    import ee

    from earthlens.gee.auth import EarthEngineAuth
    from earthlens.gee.reducers import temporal_reducer_for

    EarthEngineAuth(_SERVICE_ACCOUNT, _SERVICE_KEY)
    collection = ee.ImageCollection(
        [
            _rising_scene(1, "2020-01-01"),
            _rising_scene(5, "2020-02-01"),
            _rising_scene(9, "2020-03-01"),
        ]
    )
    slope = temporal_reducer_for(reducer_name).apply_ee(collection)
    value = (
        slope.reduceRegion(
            reducer=ee.Reducer.first(),
            geometry=ee.Geometry.Point([31.27, 29.97]),
            scale=1000,
        )
        .get("v")
        .getInfo()
    )
    assert value is not None, f"expected a slope value, got {value}"
    assert value > 0, f"expected a positive slope, got {value}"
