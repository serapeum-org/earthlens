"""Facade-routing tests for the `"eumetsat"` data_source key."""

from __future__ import annotations

import pytest
from earthlens.earthlens import EarthLens

import earthlens.eumetsat

from .conftest import _FakeProduct

pytestmark = pytest.mark.eumetsat


def test_key_registered():
    """The eumetsat key is present in the facade registry."""
    assert "eumetsat" in EarthLens.DataSources


def test_key_resolves_to_eumetsat_class():
    """The key resolves to earthlens.eumetsat.EUMETSAT."""
    assert EarthLens.DataSources["eumetsat"] is earthlens.eumetsat.EUMETSAT
    assert EarthLens.DataSources["eumetsat"].__name__ == "EUMETSAT"


def test_facade_routes_download(fake_eumdac, tmp_path):
    """EarthLens(data_source='eumetsat').download() routes to the backend."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("p")]
    el = EarthLens(
        data_source="eumetsat",
        start="2024-01-01",
        end="2024-01-02",
        variables={"msg-hrseviri": ["HRSEVIRI"]},
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        consumer_key="k",
        consumer_secret="s",
    )
    paths = el.download(progress_bar=False)
    assert [p.name for p in paths] == ["p"]


def test_facade_rejects_aggregate_via_not_implemented(fake_eumdac, tmp_path):
    """aggregate= reaches the backend and raises NotImplementedError."""
    el = EarthLens(
        data_source="eumetsat",
        start="2024-01-01",
        end="2024-01-02",
        variables={"msg-hrseviri": ["HRSEVIRI"]},
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        consumer_key="k",
        consumer_secret="s",
    )
    with pytest.raises(NotImplementedError):
        el.download(aggregate=object())
