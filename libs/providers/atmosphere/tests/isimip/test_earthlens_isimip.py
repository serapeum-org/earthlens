"""Tests for the `EarthLens` facade entry that routes to the ISIMIP backend."""

from __future__ import annotations

import pytest

import earthlens.isimip
from earthlens.earthlens import EarthLens

from .conftest import FakeClient

pytestmark = [pytest.mark.isimip, pytest.mark.unit]


def test_keys_present():
    """The `isimip` key is registered in `EarthLens.DataSources`."""
    assert "isimip" in EarthLens.DataSources


def test_key_resolves_to_isimip_class():
    """The `isimip` key resolves to `earthlens.isimip.ISIMIP`."""
    assert EarthLens.DataSources["isimip"] is earthlens.isimip.ISIMIP


def test_key_hint_names_the_extra():
    """The `isimip` key advertises its `isimip` extra hint via `entries()`."""
    hints = {key: extras for key, _module, extras in EarthLens.DataSources.entries()}
    assert hints["isimip"] == "isimip", hints["isimip"]


def test_facade_builds_backend(tmp_path):
    """The facade forwards the facet set + injected client to the backend."""
    el = EarthLens(
        "isimip",
        dataset="ISIMIP3b",
        variables=["pr"],
        scenario="ssp585",
        gcm="gfdl-esm4",
        start="2016-01-01",
        end="2018-12-31",
        lat_lim=[51.0, 53.0],
        lon_lim=[6.0, 8.0],
        path=str(tmp_path),
        client=FakeClient(),
    )
    backend = el.datasource
    assert isinstance(backend, earthlens.isimip.ISIMIP)
    assert backend.OUTPUT_KIND == "raster"
    assert backend._gcm == "gfdl-esm4"


def test_facade_download_returns_paths(tmp_path):
    """A facade `download()` returns the cut NetCDF paths."""
    out = EarthLens(
        "isimip",
        dataset="ISIMIP3b",
        variables=["pr"],
        scenario="ssp585",
        gcm="gfdl-esm4",
        start="2016-01-01",
        end="2018-12-31",
        lat_lim=[51.0, 53.0],
        lon_lim=[6.0, 8.0],
        path=str(tmp_path),
        client=FakeClient(),
    ).download(progress_bar=False)
    assert out, out
    assert all(p.suffix == ".nc" for p in out), out


def test_facade_rejects_aggregate(tmp_path):
    """The ISIMIP backend refuses a non-None `aggregate` through the facade."""
    el = EarthLens(
        "isimip",
        dataset="ISIMIP3b",
        variables=["pr"],
        scenario="ssp585",
        gcm="gfdl-esm4",
        start="2016-01-01",
        end="2018-12-31",
        lat_lim=[51.0, 53.0],
        lon_lim=[6.0, 8.0],
        path=str(tmp_path),
        client=FakeClient(),
    )
    with pytest.raises(NotImplementedError, match="reduce|aggregate|separately"):
        el.download(aggregate={"reducer": "mean"}, progress_bar=False)
