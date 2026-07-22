"""Unit tests for the `earthlens.s3.S3` backend (offline, faked boto3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.aggregate import AggregationConfig

from earthlens.s3 import S3

pytestmark = [pytest.mark.s3]

# A 1-degree DEM tile name whose synthetic COG fixture covers [6, 0, 7, 1].
_DEM_KEY = (
    "Copernicus_DSM_COG_10_N00_00_E006_00_DEM/"
    "Copernicus_DSM_COG_10_N00_00_E006_00_DEM.tif"
)


def _dem_source(path, **kwargs):
    """Build a Copernicus-DEM S3 source over a tiny in-tile AOI."""
    return S3(
        start="2021-01-01",
        end="2021-01-01",
        lat_lim=[0.4, 0.6],
        lon_lim=[6.4, 6.6],
        dataset="copernicus-dem",
        path=str(path),
        **kwargs,
    )


def test_search_plans_the_dem_tile(tmp_path, fake_client_factory, patch_auth):
    """_search returns the deterministic tile key with no transfer."""
    patch_auth(fake_client_factory())
    products = _dem_source(tmp_path)._search()
    assert [p.href for p in products] == [_DEM_KEY]


def test_download_crops_to_the_aoi(tmp_path, fake_client_factory, patch_auth):
    """download fetches the tile and crops it to the requested bbox."""
    from pyramids.dataset import Dataset

    patch_auth(fake_client_factory())
    paths = _dem_source(tmp_path).download(progress_bar=False)
    assert len(paths) == 1 and Path(paths[0]).exists()
    cropped = Dataset.read_file(str(paths[0]))
    assert cropped.epsg == 4326
    assert (
        cropped.shape[1] < 10 and cropped.shape[2] < 10
    )  # smaller than the 10x10 tile


def test_download_is_idempotent_on_the_raw_file(
    tmp_path, fake_client_factory, patch_auth
):
    """A second download does not re-fetch an already-downloaded granule."""
    client = fake_client_factory()
    patch_auth(client)
    source = _dem_source(tmp_path)
    source.download(progress_bar=False)
    source.download(progress_bar=False)
    assert len(client.downloaded) == 1


def test_missing_object_is_skipped(tmp_path, fake_client_factory, patch_auth):
    """An absent object is skipped while a present one is still fetched."""
    from earthlens.base import RemoteProduct

    client = fake_client_factory(missing=["ghost.tif"])
    patch_auth(client)
    source = _dem_source(tmp_path)
    present = source._search()[0]
    ghost = RemoteProduct(
        id="ghost", href="ghost.tif", metadata={"bucket": "copernicus-dem-30m"}
    )
    written = source._fetch([present, ghost])
    assert len(written) == 1


def test_all_missing_raises(tmp_path, fake_client_factory, patch_auth):
    """When every planned object is absent, download raises rather than return []."""
    source = _dem_source(tmp_path)
    client = fake_client_factory(missing=[p.href for p in source._search()])
    patch_auth(client)
    with pytest.raises(RuntimeError, match="none of"):
        source.download(progress_bar=False)


def test_egress_warning_sums_known_sizes(tmp_path, monkeypatch):
    """The egress check sums size_bytes across products that carry one."""
    from earthlens.base import RemoteProduct

    calls: list[tuple[str | None, int, bool]] = []
    monkeypatch.setattr(
        "earthlens.s3.backend.warn_if_egress",
        lambda region, *, size_bytes, probe: calls.append((region, size_bytes, probe)),
    )
    source = _dem_source(tmp_path)
    products = [
        RemoteProduct(id="a", href="a.tif", metadata={"bucket": "b", "size_bytes": 10}),
        RemoteProduct(id="b", href="b.tif", metadata={"bucket": "b", "size_bytes": 20}),
        RemoteProduct(id="c", href="c.tif", metadata={"bucket": "b"}),
    ]
    source._warn_cross_region_egress(products)
    assert calls == [(source._dataset.region, 30, False)]


def test_no_egress_warning_when_no_sizes(tmp_path, monkeypatch):
    """No product carries a size, so the egress helper is not consulted."""
    from earthlens.base import RemoteProduct

    calls: list[int] = []
    monkeypatch.setattr(
        "earthlens.s3.backend.warn_if_egress",
        lambda *args, **kwargs: calls.append(1),
    )
    source = _dem_source(tmp_path)
    source._warn_cross_region_egress(
        [RemoteProduct(id="a", href="a.tif", metadata={"bucket": "b"})]
    )
    assert calls == []


def test_cog_dataset_rejects_aggregate(tmp_path, fake_client_factory, patch_auth):
    """aggregate= is rejected for a COG dataset with a clear message."""
    patch_auth(fake_client_factory())
    with pytest.raises(NotImplementedError, match="COG"):
        _dem_source(tmp_path).download(aggregate=AggregationConfig(freq="D", op="mean"))


def test_bucket_override(tmp_path, fake_client_factory, patch_auth):
    """A bucket= kwarg overrides the dataset's default bucket."""
    patch_auth(fake_client_factory())
    source = _dem_source(tmp_path, bucket="copernicus-dem-90m")
    assert source._dataset.bucket == "copernicus-dem-90m"


def test_output_kind_is_mixed():
    """The backend declares the mixed output kind for the facade gating."""
    assert S3.OUTPUT_KIND == "mixed"


def test_static_dataset_yields_single_date(tmp_path, fake_client_factory, patch_auth):
    """A static dataset collapses the window to one date entry."""
    patch_auth(fake_client_factory())
    source = _dem_source(tmp_path)
    assert len(source.time.dates) == 1


def test_datasets_lists_the_registry():
    """The discovery classmethod returns the registry names."""
    assert "era5" in S3.datasets() and "goes" in S3.datasets()


def test_requester_pays_uses_a_signed_client(tmp_path):
    """A requester-pays dataset builds a signed client in the region (not UNSIGNED)."""
    from botocore import UNSIGNED

    source = S3(
        start="2021-09-01",
        end="2021-09-01",
        lat_lim=[0, 1],
        lon_lim=[0, 1],
        dataset="usgs-landsat",
        variables=["red"],
        scene="LC08_L2SP_039037_20210901_20210910_02_T1",
        path=str(tmp_path),
    )
    assert source._dataset.requester_pays is True
    client = source._auth.client()
    assert client.meta.config.signature_version is not UNSIGNED
    assert client.meta.region_name == "us-west-2"


def test_requester_pays_passes_request_payer_on_download(
    tmp_path, fake_client_factory, patch_auth
):
    """download_file is called with ExtraArgs RequestPayer=requester for requester-pays."""
    client = fake_client_factory()
    patch_auth(client)
    source = S3(
        start="2021-09-01",
        end="2021-09-01",
        lat_lim=[0.4, 0.6],
        lon_lim=[6.4, 6.6],
        dataset="usgs-landsat",
        variables=["red"],
        scene="LC08_L2SP_039037_20210901_20210910_02_T1",
        path=str(tmp_path),
    )
    source.download(progress_bar=False)
    assert client.extra_args == [{"RequestPayer": "requester"}]


def test_public_download_has_no_request_payer(
    tmp_path, fake_client_factory, patch_auth
):
    """A public dataset downloads with no RequestPayer ExtraArgs."""
    client = fake_client_factory()
    patch_auth(client)
    _dem_source(tmp_path).download(progress_bar=False)
    assert client.extra_args == [None]


def test_landsat_scene_resolves_band_keys(tmp_path, fake_client_factory, patch_auth):
    """The scene= argument flows into the per-band Landsat keys."""
    patch_auth(fake_client_factory())
    source = S3(
        start="2021-09-01",
        end="2021-09-01",
        lat_lim=[0, 1],
        lon_lim=[0, 1],
        dataset="usgs-landsat",
        variables=["red", "nir"],
        scene="LC08_L2SP_039037_20210901_20210910_02_T1",
        path=str(tmp_path),
    )
    hrefs = [p.href for p in source._search()]
    assert all("LC08_L2SP_039037" in h and h.endswith(".TIF") for h in hrefs)
    assert hrefs[0].endswith("_SR_B4.TIF") and hrefs[1].endswith("_SR_B5.TIF")


def test_api_composes_search_and_fetch(tmp_path, fake_client_factory, patch_auth):
    """_api returns the same paths as the search/fetch composition."""
    patch_auth(fake_client_factory())
    assert len(_dem_source(tmp_path)._api()) == 1


def test_non_missing_download_error_raises(tmp_path, fake_client_factory, patch_auth):
    """A non-404 download error is surfaced, not swallowed."""
    source = _dem_source(tmp_path)
    client = fake_client_factory(broken=[source._search()[0].href])
    patch_auth(client)
    with pytest.raises(RuntimeError, match="failed to download"):
        source.download(progress_bar=False)


def test_access_denied_raises_permission_error_not_skip(
    tmp_path, fake_client_factory, patch_auth
):
    """A 403/AccessDenied is a permission error, not a missing object (M1)."""
    source = _dem_source(tmp_path)
    client = fake_client_factory(denied=[source._search()[0].href])
    patch_auth(client)
    with pytest.raises(PermissionError, match="access denied"):
        source.download(progress_bar=False)


def test_access_denied_names_requester_pays(tmp_path, fake_client_factory, patch_auth):
    """For requester-pays datasets the AccessDenied message points at credentials."""
    source = S3(
        start="2021-09-01",
        end="2021-09-01",
        lat_lim=[0.4, 0.6],
        lon_lim=[6.4, 6.6],
        dataset="usgs-landsat",
        variables=["red"],
        scene="LC08_L2SP_039037_20210901_20210910_02_T1",
        path=str(tmp_path),
    )
    client = fake_client_factory(denied=[source._search()[0].href])
    patch_auth(client)
    with pytest.raises(PermissionError, match="requester-pays.*credentials"):
        source.download(progress_bar=False)


def test_no_such_bucket_raises_clear_error(tmp_path, fake_client_factory, patch_auth):
    """A NoSuchBucket is reported as a bucket error, not 'no data' (M1)."""
    source = _dem_source(tmp_path)
    client = fake_client_factory(no_bucket=[source._search()[0].href])
    patch_auth(client)
    with pytest.raises(RuntimeError, match="bucket not found"):
        source.download(progress_bar=False)


def test_variable_for_native_handles_none(tmp_path, fake_client_factory, patch_auth):
    """The variable lookup returns None for an unknown / missing token."""
    patch_auth(fake_client_factory())
    source = _dem_source(tmp_path)
    assert source._variable_for_native(None) is None
    assert source._variable_for_native("nope") is None


def test_reproject_branch_runs_for_non_4326(tmp_path, fake_client_factory, patch_auth):
    """A dataset with crs other than 4326 routes through the reproject path."""
    client = fake_client_factory()
    patch_auth(client)
    # Inline passthrough COG with crs=None (per-file) over the fixture's tile;
    # exercises the `crs != 4326 -> to_crs(4326)` branch in _localise.
    source = S3(
        start="2021-01-01",
        end="2021-01-01",
        lat_lim=[0.4, 0.6],
        lon_lim=[6.4, 6.6],
        dataset={
            "bucket": "copernicus-dem-30m",
            "format": "cog",
            "layout": "deterministic_tiles",
            "crs": None,
            "params": {"key_template": "any.tif"},
        },
        variables=["band"],
        path=str(tmp_path),
    )
    # the passthrough variable token is opaque; resolve_variables passes it raw
    paths = source.download(progress_bar=False)
    assert len(paths) == 1 and Path(paths[0]).exists()


def test_goes_selects_cmi_over_dqf(tmp_path, fake_client_factory, patch_auth):
    """GOES resolves the data variable CMI, never the same-rank DQF quality flag (review M1)."""
    import numpy as np
    import xarray as xr
    from pyramids.netcdf import NetCDF

    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    nc_path = tmp_path / "goes_cmi_dqf.nc"
    n = 8
    # DQF deliberately listed before CMI to defeat a naive first-at-max-rank pick
    xr.Dataset(
        {
            "DQF": (("y", "x"), np.zeros((n, n), "float32")),
            "CMI": (("y", "x"), np.ones((n, n), "float32")),
        },
        coords={"y": np.arange(n, dtype="float64"), "x": np.arange(n, dtype="float64")},
    ).to_netcdf(nc_path)

    source = S3(
        start="2024-06-28",
        end="2024-06-28",
        lat_lim=[30, 32],
        lon_lim=[-100, -98],
        dataset="goes",
        variables=["C13"],
        path=str(tmp_path),
    )
    product = RemoteProduct(
        id="x", href="x.nc", metadata={"bucket": "b", "variable": "C13"}
    )
    # pinned nc_variable wins outright:
    assert source._nc_variable_name(NetCDF.read_file(str(nc_path)), product) == "CMI"
    # and even an unpinned token falls back to the tie-break, which avoids DQF:
    unpinned = RemoteProduct(
        id="x", href="x.nc", metadata={"bucket": "b", "variable": "zzz"}
    )
    assert source._nc_variable_name(NetCDF.read_file(str(nc_path)), unpinned) == "CMI"


def test_nc_variable_name_picks_gridded_over_helper(
    tmp_path, fake_client_factory, patch_auth
):
    """With multiple variables and no usable pin, the gridded data var wins over a 1-D helper (M3)."""
    import numpy as np
    import xarray as xr
    from pyramids.netcdf import NetCDF

    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    nc_path = tmp_path / "multivar.nc"
    lat = np.arange(42, 39.75, -0.25)
    lon = np.arange(0, 360, 0.25)
    xr.Dataset(
        {
            "VAR_2T": (
                ("time", "latitude", "longitude"),
                np.zeros((2, lat.size, lon.size), "float32"),
            ),
            "utc_date": (("time",), np.array([0.0, 1.0])),  # 1-D auxiliary
        },
        coords={"time": [0.0, 1.0], "latitude": lat, "longitude": lon},
    ).to_netcdf(nc_path)

    source = S3(
        start="2024-06-01",
        end="2024-06-01",
        lat_lim=[40, 42],
        lon_lim=[12, 14],
        dataset="era5",
        variables=["t2m"],
        path=str(tmp_path),
    )
    # a product whose token has no pinned nc_variable in this file -> gridded fallback
    product = RemoteProduct(
        id="x", href="x.nc", metadata={"bucket": "b", "variable": "unknown"}
    )
    assert source._nc_variable_name(NetCDF.read_file(str(nc_path)), product) == "VAR_2T"


def test_netcdf_localise_rebuilds_and_crops(
    tmp_path, fake_client_factory, patch_auth, tiny_era5_nc
):
    """An ERA5-style NetCDF is rebuilt to WGS84, lon-wrapped, and cropped."""
    from pyramids.dataset import Dataset

    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    source = S3(
        start="2023-12-01",
        end="2023-12-01",
        lat_lim=[40.0, 42.0],
        lon_lim=[12.0, 14.0],
        dataset="era5",
        variables=["t2m"],
        path=str(tmp_path),
    )
    product = RemoteProduct(
        id="t2m_202312",
        href="x.nc",
        metadata={"bucket": "nsf-ncar-era5", "variable": "128_167_2t"},
    )
    out = source._localise(tiny_era5_nc, product)
    assert Path(out).suffix == ".tif"
    cropped = Dataset.read_file(str(out))
    assert cropped.epsg == 4326 and cropped.shape[1] < 50 and cropped.shape[2] < 50


def test_goes_geostationary_localise_warps_to_wgs84(
    tmp_path, fake_client_factory, patch_auth, tiny_goes_nc
):
    """GOES (geostationary NetCDF) localise warps the scan-angle grid to WGS84 + crops."""
    from pyramids.dataset import Dataset

    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    # AOI inside the synthetic frame's warped extent (~[-120, 36, -29, 63]).
    source = S3(
        start="2024-06-28",
        end="2024-06-28",
        lat_lim=[40.0, 42.0],
        lon_lim=[-90.0, -88.0],
        dataset="goes",
        variables=["C13"],
        path=str(tmp_path),
    )
    product = RemoteProduct(
        id="C13_2024180",
        href="x.nc",
        metadata={"bucket": "noaa-goes16", "variable": "C13"},
    )
    out = source._localise(tiny_goes_nc, product)
    cropped = Dataset.read_file(str(out))
    # Smoke check: asserts the geostationary warp yields a non-empty WGS84 raster.
    # Pixel y-orientation (the mirror-about-y bug C1 fixes) is guarded offline by
    # test_goes_geostationary_preserves_north_south_orientation, and end-to-end on
    # real data by e2e test_goes_matches_source_radiance.
    assert cropped.epsg == 4326 and cropped.shape[1] >= 1 and cropped.shape[2] >= 1


def test_goes_geostationary_preserves_north_south_orientation(tmp_path, tiny_goes_nc):
    """GOES warp keeps north-south orientation (offline guard for the C1 y-flip)."""
    import numpy as np

    from earthlens.base import RemoteProduct

    source = S3(
        start="2024-06-28",
        end="2024-06-28",
        lat_lim=[40.0, 42.0],
        lon_lim=[-90.0, -88.0],
        dataset="goes",
        variables=["C13"],
        path=str(tmp_path),
    )
    product = RemoteProduct(
        id="C13", href="x.nc", metadata={"bucket": "noaa-goes16", "variable": "C13"}
    )
    warped = source._geostationary_to_wgs84(tiny_goes_nc, product)
    arr = np.asarray(warped.read_array(), dtype="float64")
    if arr.ndim == 3:
        arr = arr[0]
    # The fixture increases north->south (row 0 = north = 0), so a correctly
    # oriented warp has a lower mean at the top (north) than the bottom (south);
    # a mirror-about-y regression inverts this.
    third = arr.shape[0] // 3
    north = arr[:third][np.isfinite(arr[:third])].mean()
    south = arr[-third:][np.isfinite(arr[-third:])].mean()
    assert south - north > 5.0


def test_variables_accepts_a_single_string(tmp_path, fake_client_factory, patch_auth):
    """A bare string variable is normalised to a one-element list."""
    patch_auth(fake_client_factory())
    source = _dem_source(tmp_path, variables="elevation")
    assert source.vars == ["elevation"]


def test_netcdf_no_lon_wrap_branch(
    tmp_path, fake_client_factory, patch_auth, tiny_era5_nc
):
    """A NetCDF dataset without the 0-360 convention skips the longitude wrap."""
    from pyramids.dataset import Dataset

    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    source = S3(
        start="2023-12-01",
        end="2023-12-01",
        lat_lim=[40.0, 42.0],
        lon_lim=[12.0, 14.0],
        dataset={
            "bucket": "b",
            "format": "netcdf",
            "layout": "prefix_listing",
            "crs": 4326,
        },
        variables=["VAR_2T"],
        path=str(tmp_path),
    )
    assert source._dataset.lon_convention is None
    out = source._localise(
        tiny_era5_nc,
        RemoteProduct(
            id="x", href="x.nc", metadata={"bucket": "b", "variable": "VAR_2T"}
        ),
    )
    assert Dataset.read_file(str(out)).epsg == 4326


def test_resolve_nc_variable_reads_file_when_unpinned(
    tmp_path, fake_client_factory, patch_auth, tiny_era5_nc
):
    """With no pinned nc_variable, _resolve_nc_variable falls back to reading the granule."""
    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    source = S3(
        start="2024-06-01",
        end="2024-06-01",
        lat_lim=[0, 1],
        lon_lim=[0, 1],
        dataset={
            "bucket": "b",
            "format": "netcdf",
            "layout": "prefix_listing",
            "crs": 4326,
        },
        variables=["x"],
        path=str(tmp_path),
    )
    product = RemoteProduct(
        id="x", href="x.nc", metadata={"bucket": "b", "variable": "x"}
    )
    assert source._resolve_nc_variable(tiny_era5_nc, product) == "VAR_2T"


def test_download_aggregate_routes_for_netcdf(
    tmp_path, fake_client_factory, patch_auth, monkeypatch
):
    """download(aggregate=) for a NetCDF dataset routes into _aggregate."""
    patch_auth(fake_client_factory())
    source = S3(
        start="2024-06-01",
        end="2024-06-01",
        lat_lim=[0, 1],
        lon_lim=[0, 1],
        dataset="era5",
        path=str(tmp_path),
    )
    monkeypatch.setattr(source, "_search", lambda: [])
    monkeypatch.setattr(source, "_aggregate", lambda products, agg: ["sentinel"])
    assert source.download(aggregate=AggregationConfig(freq="D", op="mean")) == [
        "sentinel"
    ]


def test_aggregate_skips_missing_and_failed(
    tmp_path, fake_client_factory, patch_auth, monkeypatch
):
    """_aggregate skips a missing granule and a granule whose aggregation raises."""
    import earthlens.aggregate as agg

    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    source = S3(
        start="2024-06-01",
        end="2024-06-01",
        lat_lim=[0, 1],
        lon_lim=[0, 1],
        dataset="era5",
        path=str(tmp_path),
    )
    p_missing = RemoteProduct(
        id="m", href="m.nc", metadata={"bucket": "b", "variable": "128_167_2t"}
    )
    p_fail = RemoteProduct(
        id="f", href="f.nc", metadata={"bucket": "b", "variable": "128_167_2t"}
    )
    raws = {"m": None, "f": tmp_path / "f.nc"}
    (tmp_path / "f.nc").write_bytes(b"x")
    monkeypatch.setattr(source, "_download_raw", lambda c, p, d: raws[p.id])

    def _boom(path, var_info, config):
        raise RuntimeError("bad granule")

    monkeypatch.setattr(agg, "aggregate_netcdf", _boom)
    assert (
        source._aggregate([p_missing, p_fail], AggregationConfig(freq="D", op="mean"))
        == []
    )


def test_aggregate_resolves_in_file_variable_and_runs(
    tmp_path, fake_client_factory, patch_auth, monkeypatch, tiny_era5_nc
):
    """_aggregate resolves the in-file NetCDF variable (VAR_2T, not the native token) (M2)."""
    import earthlens.aggregate as agg

    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    source = S3(
        start="2024-06-01",
        end="2024-06-01",
        lat_lim=[0, 1],
        lon_lim=[0, 1],
        dataset="era5",
        path=str(tmp_path),
    )
    out = tmp_path / "agg_2t.tif"
    out.write_bytes(b"x")
    captured = {}

    def _fake_aggregate(path, var_info, config):
        captured["nc_variable"] = var_info.nc_variable
        return [(None, None, out)]

    monkeypatch.setattr(agg, "aggregate_netcdf", _fake_aggregate)
    # the raw granule is a real NetCDF whose data variable is VAR_2T
    monkeypatch.setattr(source, "_download_raw", lambda c, p, d: tiny_era5_nc)
    products = [
        RemoteProduct(
            id="t2m_202406",
            href="x.nc",
            metadata={"bucket": "nsf-ncar-era5", "variable": "128_167_2t"},
        )
    ]
    results = source._aggregate(products, AggregationConfig(freq="D", op="mean"))
    assert results == [out]
    assert captured["nc_variable"] == "VAR_2T"  # not the native token "128_167_2t"
