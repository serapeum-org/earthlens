"""Unit tests for the SolarWindAtlas backend (faked pyramids + requests)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.solar_wind_atlas import SolarWindAtlas

from .conftest import FakeDataset, FakeGet

pytestmark = pytest.mark.solar_wind_atlas

#: A small Denmark AOI used across the backend tests.
LAT = [55.0, 55.5]
LON = [12.0, 12.5]


def _backend(tmp_path: Path, variables: list[str]) -> SolarWindAtlas:
    """Build a SolarWindAtlas over the standard AOI writing into tmp_path."""
    return SolarWindAtlas(
        variables=variables, lat_lim=LAT, lon_lim=LON, path=str(tmp_path)
    )


def test_wind_variable_reads_windowed_vsicurl(
    fake_pyramids: type[FakeDataset], tmp_path: Path
) -> None:
    """A wind layer is read windowed over /vsicurl with no ZIP download."""
    backend = _backend(tmp_path, ["wind_100m"])
    paths = backend.download()
    assert paths == [tmp_path / "wind_100m.tif"]
    opened = fake_pyramids.recorder["opened"]
    assert len(opened) == 1
    assert opened[0] == "/vsicurl/https://ndownloader.figshare.com/files/17247017"
    crop = fake_pyramids.recorder["crop"][0]
    assert crop["bbox"] == [12.0, 55.0, 12.5, 55.5], crop["bbox"]
    assert crop["epsg"] == 4326, crop["epsg"]


def test_solar_variable_downloads_then_crops(
    fake_pyramids: type[FakeDataset], fake_get: FakeGet, tmp_path: Path
) -> None:
    """A solar layer downloads its ZIP once and reads the local member."""
    backend = _backend(tmp_path, ["ghi"])
    paths = backend.download()
    assert paths == [tmp_path / "ghi.tif"]
    assert fake_get.calls == 1
    assert fake_pyramids.recorder["opened"][0].startswith("/vsizip/")


def test_mixed_request_exercises_both_transports(
    fake_pyramids: type[FakeDataset], fake_get: FakeGet, tmp_path: Path
) -> None:
    """A solar + wind request writes two files via the two transports."""
    backend = _backend(tmp_path, ["ghi", "wind_100m"])
    paths = backend.download()
    assert paths == [tmp_path / "ghi.tif", tmp_path / "wind_100m.tif"]
    opened = fake_pyramids.recorder["opened"]
    assert any(p.startswith("/vsizip/") for p in opened)
    assert any(p.startswith("/vsicurl/") for p in opened)
    assert fake_get.calls == 1


def test_aggregate_is_rejected(tmp_path: Path) -> None:
    """download(aggregate=...) raises NotImplementedError naming the cause."""
    backend = _backend(tmp_path, ["wind_100m"])
    with pytest.raises(NotImplementedError, match="static .*climatology"):
        backend.download(aggregate=object())


def test_solar_request_warns_about_large_download(
    fake_pyramids: type[FakeDataset],
    fake_get: FakeGet,
    info_log: list[str],
    tmp_path: Path,
) -> None:
    """A solar request logs the one-time multi-GB download heads-up."""
    _backend(tmp_path, ["ghi"]).download()
    assert any("downloads its full global archive" in m for m in info_log)


def test_attribution_logged_once_per_atlas(
    fake_pyramids: type[FakeDataset],
    fake_get: FakeGet,
    info_log: list[str],
    tmp_path: Path,
) -> None:
    """Each fetched atlas's CC-BY attribution is logged exactly once."""
    _backend(tmp_path, ["ghi", "dni", "wind_100m"]).download()
    attribution = [m for m in info_log if m.startswith("solar_wind_atlas attribution:")]
    gsa = [m for m in attribution if "Global Solar Atlas" in m]
    gwa = [m for m in attribution if "Global Wind Atlas" in m]
    assert len(gsa) == 1
    assert len(gwa) == 1


def test_unknown_variable_raises_did_you_mean(tmp_path: Path) -> None:
    """An unknown layer id raises ValueError with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'ghi'"):
        _backend(tmp_path, ["gho"])


def test_variables_mapping_is_rejected(tmp_path: Path) -> None:
    """A mapping passed as variables raises TypeError."""
    with pytest.raises(TypeError, match="list of layer ids"):
        SolarWindAtlas(
            variables={"gsa": ["ghi"]}, lat_lim=LAT, lon_lim=LON, path=str(tmp_path)
        )


def test_empty_variables_is_rejected(tmp_path: Path) -> None:
    """An empty variables list raises ValueError."""
    with pytest.raises(ValueError, match="requires variables"):
        _backend(tmp_path, [])


def test_missing_bbox_is_rejected(tmp_path: Path) -> None:
    """A missing bounding box raises ValueError."""
    with pytest.raises(ValueError, match="bounding box"):
        SolarWindAtlas(
            variables=["ghi"], lat_lim=None, lon_lim=None, path=str(tmp_path)
        )


def test_cache_dir_defaults_under_the_shared_cache_dir(tmp_path: Path) -> None:
    """cache_dir defaults to solar_wind_atlas/ under the shared cache directory."""
    from earthlens.config import cache_dir as shared_cache_dir

    backend = _backend(tmp_path, ["ghi"])
    assert backend.cache_dir == shared_cache_dir() / "solar_wind_atlas", (
        f"got {backend.cache_dir}"
    )


def test_cache_dir_override_is_honoured(tmp_path: Path) -> None:
    """An explicit cache_dir= overrides the default."""
    custom = tmp_path / "custom_cache"
    backend = SolarWindAtlas(
        variables=["ghi"],
        lat_lim=LAT,
        lon_lim=LON,
        path=str(tmp_path),
        cache_dir=custom,
    )
    assert backend.cache_dir == custom
