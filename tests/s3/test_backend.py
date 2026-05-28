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
        start="2021-01-01", end="2021-01-01",
        lat_lim=[0.4, 0.6], lon_lim=[6.4, 6.6],
        dataset="copernicus-dem", path=str(path), **kwargs,
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
    assert cropped.shape[1] < 10 and cropped.shape[2] < 10  # smaller than the 10x10 tile


def test_download_is_idempotent_on_the_raw_file(tmp_path, fake_client_factory, patch_auth):
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
        start="2021-01-01", end="2021-01-01",
        lat_lim=[0.4, 0.6], lon_lim=[6.4, 6.6],
        dataset={
            "bucket": "copernicus-dem-30m", "format": "cog",
            "layout": "deterministic_tiles", "crs": None,
            "params": {"key_template": "any.tif"},
        },
        variables=["band"], path=str(tmp_path),
    )
    # the passthrough variable token is opaque; resolve_variables passes it raw
    paths = source.download(progress_bar=False)
    assert len(paths) == 1 and Path(paths[0]).exists()


def test_aggregate_runs_per_window(tmp_path, fake_client_factory, patch_auth, monkeypatch):
    """_aggregate feeds each cropped NetCDF through aggregate_netcdf."""
    import earthlens.aggregate as agg
    from earthlens.base import RemoteProduct

    patch_auth(fake_client_factory())
    source = S3(
        start="2024-06-01", end="2024-06-01",
        lat_lim=[0, 1], lon_lim=[0, 1], dataset="era5", path=str(tmp_path),
    )
    out = tmp_path / "agg_2t.tif"
    out.write_bytes(b"x")
    captured = {}

    def _fake_aggregate(path, var_info, config):
        captured["nc_variable"] = var_info.nc_variable
        return [(None, None, out)]

    monkeypatch.setattr(agg, "aggregate_netcdf", _fake_aggregate)
    fake_path = tmp_path / "t2m_202406.nc"
    fake_path.write_bytes(b"x")
    source._product_by_output = {
        str(fake_path): RemoteProduct(
            id="t2m_202406", href="x", metadata={"variable": "128_167_2t"}
        )
    }
    cfg = AggregationConfig(freq="D", op="mean")
    results = source._aggregate([fake_path], cfg)
    assert results == [out] and captured["nc_variable"] == "128_167_2t"
