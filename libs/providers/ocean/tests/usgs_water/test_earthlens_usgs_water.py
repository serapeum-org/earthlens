"""Facade-routing tests for the USGS Water backend (`EarthLens` -> `USGSWater`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import earthlens.usgs_water
from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens

from .conftest import FakeUSGS

pytestmark = pytest.mark.usgs_water


def _facade(tmp_path: Path, **overrides: Any) -> EarthLens:
    """Construct an EarthLens facade bound to the USGS Water backend."""
    params: dict[str, Any] = dict(
        variables=["discharge"],
        data_source="usgs-water",
        start="2023-01-01",
        end="2023-01-05",
        lat_lim=[38.9, 39.0],
        lon_lim=[-77.2, -77.0],
        path=str(tmp_path),
        sites="01646500",
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.parametrize("key", ["usgs-water", "usgs-nwis", "nwis"])
def test_keys_resolve_to_usgswater(key):
    """Every alias resolves to the USGSWater backend class."""
    assert EarthLens.DataSources[key] is earthlens.usgs_water.USGSWater


def test_facade_builds_usgs_backend(tmp_path, fake_usgs: FakeUSGS):
    """The facade binds a USGSWater instance as its datasource."""
    assert isinstance(_facade(tmp_path).datasource, earthlens.usgs_water.USGSWater)


def test_backend_kwargs_forwarded(tmp_path, fake_usgs: FakeUSGS):
    """service= / api= ride through **backend_kwargs to the backend."""
    facade = _facade(tmp_path, service="instantaneous", api="legacy")
    assert facade.datasource._service == "instantaneous"
    assert facade.datasource._api_flavour == "legacy"


def test_aggregate_rejected_for_tabular(tmp_path, fake_usgs: FakeUSGS):
    """The facade rejects aggregate= for the tabular backend."""
    facade = _facade(tmp_path)
    with pytest.raises(NotImplementedError) as exc:
        facade.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
    assert "tabular" in str(exc.value)


def test_facade_download_returns_dataframe(tmp_path, fake_usgs: FakeUSGS):
    """A facade download returns the long-format DataFrame."""
    df = _facade(tmp_path).download(progress_bar=False)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 3
