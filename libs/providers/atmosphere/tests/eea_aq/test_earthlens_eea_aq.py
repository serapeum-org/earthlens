"""Facade-routing tests for the EEA backend (`EarthLens` -> `EEA_AQ`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens

import earthlens.eea_aq


def _facade(client, tmp_path: Path, **overrides: Any) -> EarthLens:
    """Construct an EarthLens facade bound to the EEA backend."""
    params: dict[str, Any] = dict(
        variables=["pm25"],
        data_source="eea-aq",
        start="2023-06-01",
        end="2023-06-30",
        lat_lim=[35.7, 36.1],
        lon_lim=[14.1, 14.6],
        path=str(tmp_path),
        country="MT",
        client=client,
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.eea
class TestFacadeRouting:
    """The `eea-aq` key resolves to and constructs the backend."""

    def test_key_present(self):
        """`eea-aq` is among the registered data sources."""
        assert "eea-aq" in EarthLens.DataSources

    def test_key_resolves(self):
        """The `eea-aq` key resolves to `earthlens.eea_aq.EEA_AQ`."""
        assert EarthLens.DataSources["eea-aq"] is earthlens.eea_aq.EEA_AQ

    def test_facade_builds_backend(self, tmp_path, fake_client):
        """The facade binds an EEA_AQ instance as its datasource."""
        facade = _facade(fake_client, tmp_path)
        assert isinstance(facade.datasource, earthlens.eea_aq.EEA_AQ)


@pytest.mark.eea
class TestFacadeDownload:
    """The facade returns the DataFrame and guards aggregate."""

    def test_download_returns_dataframe(self, tmp_path, fake_client):
        """A facade download returns the long-format DataFrame."""
        df = _facade(fake_client, tmp_path).download(progress_bar=False)
        assert isinstance(df, pd.DataFrame) and set(df["parameter"]) == {"pm25"}

    def test_aggregate_rejected(self, tmp_path, fake_client):
        """`download(aggregate=...)` on the tabular backend raises."""
        with pytest.raises(NotImplementedError):
            _facade(fake_client, tmp_path).download(
                aggregate=AggregationConfig(freq="1MS", op="mean")
            )
