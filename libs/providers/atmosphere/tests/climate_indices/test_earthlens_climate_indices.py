"""Tests for the `EarthLens` facade entries routing to ClimateIndices."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import earthlens.climate_indices
from earthlens.climate_indices import backend
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.climate_indices

KEYS = ["climate-indices", "climate_indices", "climate-indices:teleconnections"]

DATA = Path(__file__).parent / "data"


class _FakeResponse:
    """A minimal stand-in for `requests.Response`."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        """No-op: the fixture responses are always 200."""

    def close(self) -> None:
        """No-op close — the fake owns no socket."""


def _fake_get(url: str, timeout: float | None = None, **_kwargs) -> _FakeResponse:
    """Return the ONI fixture for any URL."""
    return _FakeResponse((DATA / "psl" / "oni.data").read_text())


@pytest.mark.unit
class TestRegistry:
    """Tests for the climate-indices entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every climate-indices key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_climate_indices_class(self, key: str) -> None:
        """All keys resolve to `earthlens.climate_indices.ClimateIndices`."""
        assert EarthLens.DataSources[key] is earthlens.climate_indices.ClimateIndices

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_no_extra(self, key: str) -> None:
        """The backend needs no optional extra, so its hint is empty."""
        assert EarthLens.DataSources.default_kwargs(key) == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="climate-indices", ...)`."""

    def test_constructs_climate_indices_backend(self, tmp_path: Path) -> None:
        """The facade builds the ClimateIndices backend for an index request."""
        el = EarthLens(
            data_source="climate-indices",
            variables=["oni"],
            start="2000-01-01",
            end="2001-12-31",
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.climate_indices.ClimateIndices)
        assert el.datasource.OUTPUT_KIND == "tabular"

    def test_facade_download_returns_dataframe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A faked download routed through the facade returns a long DataFrame."""
        monkeypatch.setattr(backend.requests, "get", _fake_get)
        df = EarthLens(
            data_source="climate-indices:teleconnections",
            variables=["oni"],
            start="2000-01-01",
            end="2001-12-31",
            path=tmp_path,
        ).download()
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["date", "index", "value", "source"]
        assert set(df["index"].unique()) == {"oni"}
