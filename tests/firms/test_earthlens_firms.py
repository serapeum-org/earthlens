"""Facade routing + vector aggregate-rejection for FIRMS."""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.firms
from earthlens.earthlens import EarthLens


def _facade_kwargs(tmp_path: Path) -> dict[str, object]:
    """Standard EarthLens(...) kwargs targeting the FIRMS backend."""
    return dict(
        data_source="firms",
        variables=["VIIRS_SNPP_NRT"],
        start="2024-08-01",
        end="2024-08-01",
        lat_lim=[33.0, 35.0],
        lon_lim=[-119.0, -117.0],
        path=str(tmp_path),
    )


@pytest.mark.firms
def test_firms_key_registered():
    """The facade registers 'firms' resolving to earthlens.firms.FIRMS."""
    assert EarthLens.DataSources["firms"] is earthlens.firms.FIRMS


@pytest.mark.firms
def test_facade_constructs_firms_backend(tmp_path: Path):
    """EarthLens(data_source='firms', ...) builds the FIRMS backend."""
    el = EarthLens(**_facade_kwargs(tmp_path))
    assert isinstance(el.datasource, earthlens.firms.FIRMS)
    assert el.datasource.OUTPUT_KIND == "vector"


@pytest.mark.firms
def test_facade_rejects_aggregate(tmp_path: Path):
    """A vector backend rejects aggregate= at the facade with NotImplementedError."""
    el = EarthLens(**_facade_kwargs(tmp_path))
    with pytest.raises(NotImplementedError, match="aggregate= is not supported"):
        el.download(aggregate=object())


@pytest.mark.firms
def test_facade_authenticate_forwards_api_key(tmp_path: Path):
    """EarthLens.authenticate(api_key=...) forwards the key to the FIRMS backend."""
    el = EarthLens(**_facade_kwargs(tmp_path)).authenticate(api_key="facade-key")
    assert el.datasource.client.map_key == "facade-key"


@pytest.mark.firms
def test_facade_authenticate_reads_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """EarthLens.authenticate() with no api_key reads FIRMS_MAP_KEY."""
    monkeypatch.setenv("FIRMS_MAP_KEY", "facade-env")
    el = EarthLens(**_facade_kwargs(tmp_path)).authenticate()
    assert el.datasource.client.map_key == "facade-env"
