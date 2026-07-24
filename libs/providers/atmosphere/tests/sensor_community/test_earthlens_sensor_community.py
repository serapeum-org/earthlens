"""Facade tests for Sensor.Community (`EarthLens` -> `SensorCommunity`)."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import earthlens.sensor_community
from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens


def _facade(client, tmp_path: Path, **overrides: Any) -> EarthLens:
    """Construct an EarthLens facade bound to the Sensor.Community backend."""
    params: dict[str, Any] = dict(
        variables=["pm25"],
        data_source="sensor-community",
        start="2026-06-30",
        end="2026-06-30",
        lat_lim=[48.5, 48.9],
        lon_lim=[9.0, 9.3],
        path=str(tmp_path),
        client=client,
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.sensor_community
class TestFacadeRouting:
    """The `sensor-community` key resolves to and builds the backend."""

    def test_key_present(self):
        """`sensor-community` is among the registered data sources."""
        assert "sensor-community" in EarthLens.DataSources

    def test_key_resolves(self):
        """The key resolves to `SensorCommunity`."""
        assert (
            EarthLens.DataSources["sensor-community"]
            is earthlens.sensor_community.SensorCommunity
        )

    def test_facade_builds_backend(self, tmp_path, fake_client):
        """The facade binds a SensorCommunity instance."""
        facade = _facade(fake_client, tmp_path)
        assert isinstance(facade.datasource, earthlens.sensor_community.SensorCommunity)


@pytest.mark.sensor_community
class TestFacadeDownload:
    """The facade returns the DataFrame and guards aggregate."""

    def test_download_returns_dataframe(self, tmp_path, fake_client):
        """A facade download returns the long-format DataFrame."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = _facade(fake_client, tmp_path).download(progress_bar=False)
        assert isinstance(df, pd.DataFrame)

    def test_aggregate_rejected(self, tmp_path, fake_client):
        """`download(aggregate=...)` on the tabular backend raises."""
        with pytest.raises(NotImplementedError):
            _facade(fake_client, tmp_path).download(
                aggregate=AggregationConfig(freq="1MS", op="mean")
            )
