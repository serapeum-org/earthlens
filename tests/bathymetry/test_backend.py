"""Unit tests for the bathymetry backend (faked requests + pyramids)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import requests

from earthlens.bathymetry import backend as backend_module
from earthlens.bathymetry.backend import Bathymetry
from earthlens.bathymetry.catalog import Catalog, Dataset

pytestmark = pytest.mark.bathymetry

#: A minimal NetCDF-3 header (magic + padding) the magic-byte guard accepts.
_NETCDF_BODY = b"CDF\x01" + b"\x00" * 64


class _FakeResponse:
    """Stand-in for a `requests.Response` carrying canned bytes / status."""

    def __init__(self, content: bytes = _NETCDF_BODY, status: int = 200):
        self.content = content
        self._status = status

    def raise_for_status(self) -> None:
        """Raise an `HTTPError` for a 4xx/5xx status, like requests does."""
        if self._status >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self._status}")


class _FakeBand:
    """Stand-in for a pyramids single-variable NetCDF, writing a stub GeoTIFF."""

    def __init__(self, recorder: dict):
        self._recorder = recorder

    def to_file(self, path: str) -> None:
        """Write a tiny stub file and record the destination."""
        Path(path).write_bytes(b"II*\x00stub-geotiff")
        self._recorder.setdefault("written", []).append(path)


class _FakeNetCDFInstance:
    """Stand-in for a multi-variable pyramids NetCDF container."""

    def __init__(self, recorder: dict):
        self._recorder = recorder

    def get_variable(self, name: str) -> _FakeBand:
        """Record the extracted band name and return a writable stub."""
        self._recorder["variable"] = name
        return _FakeBand(self._recorder)


class _FakeNetCDF:
    """Stand-in for `pyramids.netcdf.NetCDF`, recording the read path."""

    recorder: dict = {}

    @classmethod
    def read_file(cls, path: str) -> _FakeNetCDFInstance:
        """Record the read path and return a fake container."""
        cls.recorder["read_file"] = path
        return _FakeNetCDFInstance(cls.recorder)


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> type[_FakeNetCDF]:
    """Inject a fake `pyramids.netcdf` module so no real GDAL is touched."""
    _FakeNetCDF.recorder = {}
    module = types.ModuleType("pyramids.netcdf")
    module.NetCDF = _FakeNetCDF
    monkeypatch.setitem(sys.modules, "pyramids.netcdf", module)
    return _FakeNetCDF


@pytest.fixture
def captured_get(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture the griddap URL and return a fixture NetCDF body."""
    captured: dict = {}

    def _fake_get(url: str, timeout: float = 0.0) -> _FakeResponse:
        captured["url"] = url
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(backend_module.requests, "get", _fake_get)
    return captured


def _make(dataset: str, tmp_path: Path, **kwargs) -> Bathymetry:
    """Construct a Bathymetry over a tiny bbox writing under tmp_path."""
    return Bathymetry(
        dataset=dataset,
        lat_lim=[25.0, 26.0],
        lon_lim=[-18.0, -17.0],
        path=tmp_path,
        **kwargs,
    )


def test_gebco_download_builds_url_and_writes_geotiff(
    tmp_path: Path, captured_get: dict, fake_pyramids: type[_FakeNetCDF]
):
    """A GEBCO request builds the griddap URL and returns a .tif path."""
    result = _make("gebco_2020", tmp_path).download()
    assert captured_get["url"] == (
        "https://coastwatch.pfeg.noaa.gov/erddap/griddap/GEBCO_2020.nc?"
        "elevation[(25.0):1:(26.0)][(-18.0):1:(-17.0)]"
    )
    assert result == [tmp_path.absolute() / "gebco_2020.tif"]
    assert result[0].suffix == ".tif"
    assert result[0].exists()
    assert fake_pyramids.recorder["variable"] == "elevation"


def test_etopo_ice_routes_with_z_band(
    tmp_path: Path, captured_get: dict, fake_pyramids: type[_FakeNetCDF]
):
    """ETOPO ice builds the upwell griddap URL with the z band."""
    _make("etopo1_ice", tmp_path).download()
    assert captured_get["url"] == (
        "https://upwell.pfeg.noaa.gov/erddap/griddap/etopo1_ice.nc?"
        "z[(25.0):1:(26.0)][(-18.0):1:(-17.0)]"
    )
    assert fake_pyramids.recorder["variable"] == "z"


def test_etopo_bedrock_uses_second_id(
    tmp_path: Path, captured_get: dict, fake_pyramids: type[_FakeNetCDF]
):
    """ETOPO bedrock routes through the distinct bedrock coverage id."""
    _make("etopo1_bedrock", tmp_path).download()
    assert "/griddap/etopo1_bedrock.nc?" in captured_get["url"]


def test_download_rejects_aggregate(tmp_path: Path):
    """A non-None aggregate is rejected for the static DEM."""
    backend = _make("gebco_2020", tmp_path)
    with pytest.raises(NotImplementedError, match="static"):
        backend.download(aggregate=object())


def test_oversize_non_netcdf_body_raises_valueerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_pyramids: type[_FakeNetCDF]
):
    """A non-NetCDF (HTML error) body surfaces as a clear ValueError."""
    monkeypatch.setattr(
        backend_module.requests,
        "get",
        lambda url, timeout=0.0: _FakeResponse(content=b"<html>error</html>"),
    )
    with pytest.raises(ValueError, match="non-NetCDF"):
        _make("gebco_2020", tmp_path).download()


def test_http_error_raises_valueerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_pyramids: type[_FakeNetCDF]
):
    """An HTTP error during download surfaces as a clear ValueError."""
    monkeypatch.setattr(
        backend_module.requests,
        "get",
        lambda url, timeout=0.0: _FakeResponse(status=413),
    )
    with pytest.raises(ValueError, match="coverage|too large"):
        _make("gebco_2020", tmp_path).download()


def test_missing_dataset_raises():
    """Omitting dataset= raises a clear ValueError."""
    with pytest.raises(ValueError, match="dataset="):
        Bathymetry(lat_lim=[0.0, 1.0], lon_lim=[0.0, 1.0])


def test_missing_bbox_raises():
    """Omitting the bounding box raises a clear ValueError."""
    with pytest.raises(ValueError, match="bounding box"):
        Bathymetry(dataset="gebco_2020")


def test_unknown_dataset_did_you_mean():
    """An unknown dataset id raises with a did-you-mean naming a real id."""
    with pytest.raises(ValueError, match="gebco_2020"):
        Bathymetry(dataset="gebco2020", lat_lim=[0.0, 1.0], lon_lim=[0.0, 1.0])


def test_unexpected_variable_rejected():
    """A variable that is not the row's band raises with a did-you-mean."""
    with pytest.raises(ValueError, match="elevation"):
        Bathymetry(
            dataset="gebco_2020",
            variables=["temperature"],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
        )


def test_variables_mapping_rejected():
    """Passing a mapping for variables raises a TypeError."""
    with pytest.raises(TypeError, match="mapping"):
        Bathymetry(
            dataset="gebco_2020",
            variables={"gebco_2020": ["elevation"]},
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
        )


def test_static_temporal_extent_has_no_dates(tmp_path: Path):
    """The DEM backend builds a timeless temporal extent."""
    backend = _make("gebco_2020", tmp_path)
    assert backend.time.start_date is None
    assert backend.time.end_date is None
    assert len(backend.time.dates) == 0


def test_output_kind_is_raster(tmp_path: Path):
    """The backend declares raster output (so the facade gates aggregate)."""
    assert _make("gebco_2020", tmp_path).OUTPUT_KIND == "raster"


def test_near_miss_variable_offers_did_you_mean():
    """A near-miss band name surfaces a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'elevation'"):
        Bathymetry(
            dataset="gebco_2020",
            variables=["elevaton"],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
        )


def test_large_subset_warns(
    tmp_path: Path,
    captured_get: dict,
    fake_pyramids: type[_FakeNetCDF],
    monkeypatch: pytest.MonkeyPatch,
):
    """A near-global 15-arc-second request warns about its size."""
    seen: list[str] = []
    monkeypatch.setattr(backend_module.logger, "warning", seen.append)
    Bathymetry(
        dataset="gebco_2020",
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=tmp_path,
    ).download()
    assert any("large subset" in message for message in seen)


def test_unparseable_resolution_skips_size_log(
    tmp_path: Path, captured_get: dict, fake_pyramids: type[_FakeNetCDF]
):
    """A row with no parseable resolution still downloads (no size estimate)."""
    catalog = Catalog(
        datasets={
            "custom": Dataset(
                id="custom",
                endpoint="https://coastwatch.pfeg.noaa.gov/erddap",
                dataset_id="CUSTOM",
                variable="z",
                native_resolution="",
            )
        },
        available_datasets=["custom"],
    )
    result = Bathymetry(
        dataset="custom",
        catalog=catalog,
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    ).download()
    assert result[0].name == "custom.tif"
