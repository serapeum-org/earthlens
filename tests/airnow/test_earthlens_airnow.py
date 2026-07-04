"""Facade-routing tests for the AirNow backend (`EarthLens` -> `AirNow`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import earthlens.airnow
from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens
from tests.airnow.conftest import _FakeAirnow, _FakeSession


def _facade(state: _FakeAirnow, tmp_path: Path, **overrides: Any) -> EarthLens:
    """Construct an EarthLens facade bound to the AirNow backend."""
    params: dict[str, Any] = dict(
        variables=["pm25"],
        data_source="airnow",
        start="2026-01-01",
        end="2026-01-01",
        lat_lim=[33.0, 35.0],
        lon_lim=[-119.0, -117.0],
        path=str(tmp_path),
        api_key="k",
        session=_FakeSession(state),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.airnow
class TestFacadeRouting:
    """The `airnow` key resolves to and constructs the backend."""

    def test_key_present(self):
        """`airnow` is among the registered data sources."""
        assert "airnow" in EarthLens.DataSources

    def test_key_resolves_to_airnow_class(self):
        """The `airnow` key resolves to `earthlens.airnow.AirNow`."""
        assert EarthLens.DataSources["airnow"] is earthlens.airnow.AirNow

    def test_facade_builds_backend(self, tmp_path):
        """The facade binds an AirNow instance as its datasource."""
        assert isinstance(_facade(_FakeAirnow(), tmp_path).datasource, earthlens.airnow.AirNow)

    def test_backend_kwargs_forwarded(self, tmp_path):
        """Filter kwargs ride through to the backend."""
        facade = _facade(_FakeAirnow(), tmp_path, data_type="C")
        assert facade.datasource._data_type == "C"


@pytest.mark.airnow
class TestFacadeDownload:
    """The facade returns the backend's DataFrame and guards aggregate."""

    def test_download_returns_dataframe(self, tmp_path):
        """A facade download returns the long-format DataFrame."""
        df = _facade(_FakeAirnow(), tmp_path).download(progress_bar=False)
        assert isinstance(df, pd.DataFrame)

    def test_aggregate_rejected(self, tmp_path):
        """`download(aggregate=...)` on the tabular backend raises."""
        facade = _facade(_FakeAirnow(), tmp_path)
        with pytest.raises(NotImplementedError):
            facade.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
