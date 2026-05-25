"""Facade-routing tests for the OpenAQ backend (`EarthLens` -> `OpenAQ`).

The headline test is the aggregate-rejection path: OpenAQ is the
package's first `tabular` backend, so this is the first end-to-end
exercise of the facade's `OUTPUT_KIND`-driven `aggregate=` guard for
the tabular case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import earthlens.openaq
from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens

from .conftest import _FakeOpenaq


def _facade(tmp_path: Path, **overrides: Any) -> EarthLens:
    """Construct an EarthLens facade bound to the OpenAQ backend."""
    params: dict[str, Any] = dict(
        variables=["pm25"],
        data_source="openaq",
        start="2024-01-01",
        end="2024-01-07",
        lat_lim=[34.0, 34.3],
        lon_lim=[-118.5, -118.1],
        path=str(tmp_path),
        api_key="k",
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.openaq
class TestFacadeRouting:
    """The `openaq` key resolves to and constructs the OpenAQ backend."""

    def test_key_registered(self):
        """`openaq` is among the registered data sources."""
        assert "openaq" in EarthLens.DataSources

    def test_key_resolves_to_openaq_class(self):
        """The `openaq` key resolves to `earthlens.openaq.OpenAQ`."""
        assert EarthLens.DataSources["openaq"] is earthlens.openaq.OpenAQ

    def test_facade_builds_openaq_backend(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """The facade binds an OpenAQ instance as its datasource."""
        assert isinstance(_facade(tmp_path).datasource, earthlens.openaq.OpenAQ)

    def test_backend_kwargs_forwarded(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """Filter kwargs ride through **backend_kwargs to the backend."""
        facade = _facade(tmp_path, max_locations=5, file_format="parquet")
        assert facade.datasource._max_locations == 5
        assert facade.datasource._file_format == "parquet"


@pytest.mark.openaq
class TestFacadeAggregateRejection:
    """The headline guard: a tabular backend rejects `aggregate=`."""

    def test_aggregate_raises_not_implemented(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """`download(aggregate=...)` on the tabular backend raises."""
        facade = _facade(tmp_path)
        with pytest.raises(NotImplementedError) as exc:
            facade.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
        assert "tabular" in str(exc.value), (
            f"rejection message should mention 'tabular', got: {exc.value}"
        )

    def test_aggregate_none_reaches_backend(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """`aggregate=None` is fine and a normal download runs."""
        df = _facade(tmp_path).download(progress_bar=False)
        assert isinstance(df, pd.DataFrame)


@pytest.mark.openaq
class TestFacadeDownload:
    """The facade returns the backend's DataFrame."""

    def test_download_returns_dataframe(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """A facade download returns the long-format DataFrame."""
        df = _facade(tmp_path).download(progress_bar=False)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1, f"expected 1 measurement, got {len(df)}"
