"""Facade-routing tests for the PVGIS backend (`EarthLens` -> `PVGIS`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import earthlens.pvgis
from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens

from .conftest import FakeResponse

pytestmark = pytest.mark.pvgis


def _facade(tmp_path: Path, **overrides: Any) -> EarthLens:
    """Construct an EarthLens facade bound to the PVGIS backend."""
    params: dict[str, Any] = dict(
        data_source="pvgis",
        variables=["seriescalc"],
        start="2020-01-01",
        end="2020-12-31",
        path=str(tmp_path),
        point=(45.0, 8.0),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.parametrize("key", ["pvgis", "pvgis:solar-pv"])
def test_keys_resolve_to_pvgis(key):
    """Both the canonical key and the alias resolve to the PVGIS class."""
    assert EarthLens.DataSources[key] is earthlens.pvgis.PVGIS


def test_facade_binds_pvgis_backend(tmp_path):
    """The facade binds a PVGIS instance as its datasource."""
    assert isinstance(_facade(tmp_path).datasource, earthlens.pvgis.PVGIS)


def test_backend_kwargs_forwarded(tmp_path):
    """PV knobs ride through **backend_kwargs to the backend."""
    facade = _facade(tmp_path, peakpower=2, spacing_deg=0.25)
    assert facade.datasource._knobs.get("peakpower") == 2, "peakpower not forwarded"
    assert facade.datasource._spacing_deg == 0.25, "spacing_deg not forwarded"


def test_facade_download_returns_dataframe(tmp_path, bind_session, seriescalc_payload):
    """A faked backend routes through the facade and returns a DataFrame."""
    bind_session(FakeResponse(seriescalc_payload))
    df = _facade(tmp_path).download(progress_bar=False)
    assert isinstance(df, pd.DataFrame), f"expected a DataFrame, got {type(df)}"
    assert not df.empty, "expected hourly rows"


def test_aggregate_rejected_for_tabular(tmp_path):
    """The facade rejects aggregate= for the tabular PVGIS backend."""
    with pytest.raises(NotImplementedError):
        _facade(tmp_path).download(aggregate=AggregationConfig(freq="1MS", op="mean"))
