"""Tests for the `EarthLens` facade entries routing to Glaciers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

import earthlens.glaciers
from earthlens.earthlens import EarthLens
from earthlens.glaciers import _helpers

pytestmark = pytest.mark.glaciers

KEYS = ["glaciers", "rgi", "glims", "wgms"]

DATA = Path(__file__).parent / "data"


@pytest.mark.unit
class TestRegistry:
    """Tests for the glaciers entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every glaciers key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_glaciers_class(self, key: str) -> None:
        """All keys resolve to `earthlens.glaciers.Glaciers`."""
        assert EarthLens.DataSources[key] is earthlens.glaciers.Glaciers

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_no_extra(self, key: str) -> None:
        """The backend needs no optional extra, so its hint is empty."""
        assert EarthLens.DataSources.default_kwargs(key) == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="glaciers", ...)`."""

    def test_constructs_backend_with_vector_output_kind(self, tmp_path: Path) -> None:
        """The facade builds the backend and sets OUTPUT_KIND from the dataset."""
        el = EarthLens(
            data_source="glaciers",
            variables=["rgi:outlines"],
            region="11",
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.glaciers.Glaciers)
        assert el.datasource.OUTPUT_KIND == "vector"

    def test_facade_forwards_region_and_max_features(self, tmp_path: Path) -> None:
        """`region=` / `max_features=` reach the backend via `**backend_kwargs`."""
        el = EarthLens(
            data_source="glaciers",
            variables=["glims:outlines"],
            lat_lim=[46.3, 46.5],
            lon_lim=[7.9, 8.1],
            max_features=42,
            path=tmp_path,
        )
        assert el.datasource._max_features == 42

    def test_facade_rgi_download_returns_feature_collection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An RGI download via the facade returns a FeatureCollection."""
        monkeypatch.setattr(
            _helpers, "download_zip", lambda *a, **k: DATA / "rgi_sample.zip"
        )
        result = EarthLens(
            data_source="rgi",
            variables=["rgi:outlines"],
            region="11",
            path=tmp_path,
        ).download()
        assert isinstance(result, FeatureCollection)

    def test_facade_wgms_download_returns_dataframe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A WGMS download via the facade returns a fluctuations DataFrame."""
        monkeypatch.setattr(
            _helpers, "download_zip", lambda *a, **k: DATA / "wgms_sample.zip"
        )
        df = EarthLens(
            data_source="wgms",
            variables=["wgms:mass_balance"],
            path=tmp_path,
        ).download()
        assert isinstance(df, pd.DataFrame)
        assert "glacier_id" in df.columns
