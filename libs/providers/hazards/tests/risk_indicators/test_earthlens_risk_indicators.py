"""Tests for the `EarthLens` facade entries routing to RiskIndicators."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

import earthlens.risk_indicators
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.risk_indicators

KEYS = ["risk-indicators", "thinkhazard", "inform", "gfw", "global-forest-watch"]


@pytest.mark.unit
class TestRegistry:
    """Tests for the risk-indicators entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every risk-indicators key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_risk_indicators_class(self, key: str) -> None:
        """All keys resolve to `earthlens.risk_indicators.RiskIndicators`."""
        assert EarthLens.DataSources[key] is earthlens.risk_indicators.RiskIndicators

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_no_extra(self, key: str) -> None:
        """The backend needs no optional extra, so its hint is empty."""
        assert EarthLens.DataSources.default_kwargs(key) == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="risk-indicators", ...)`."""

    def test_constructs_backend_with_per_instance_output_kind(
        self, tmp_path: Path
    ) -> None:
        """The facade builds the backend and sets OUTPUT_KIND from the dataset."""
        el = EarthLens(
            data_source="risk-indicators",
            variables=["thinkhazard:flood_river"],
            country="KEN",
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.risk_indicators.RiskIndicators)
        assert el.datasource.OUTPUT_KIND == "tabular"

    def test_facade_forwards_country(self, tmp_path: Path) -> None:
        """`country=` reaches the backend via `**backend_kwargs`."""
        el = EarthLens(
            data_source="inform",
            variables=["inform:risk"],
            country="KEN",
            path=tmp_path,
        )
        assert el.datasource._country == "KEN"

    def test_facade_forwards_workflow_id(self, fake_http, tmp_path: Path) -> None:
        """`workflow_id=` reaches the backend and replaces the catalog pin."""
        EarthLens(
            data_source="inform",
            variables=["inform:risk"],
            country="KEN",
            workflow_id=493,
            path=tmp_path,
        ).download()
        assert fake_http.calls[0]["params"]["WorkflowId"] == 493

    def test_facade_thinkhazard_download_returns_dataframe(
        self, fake_http, tmp_path: Path
    ) -> None:
        """A faked ThinkHazard download via the facade returns a hazard table."""
        df = EarthLens(
            data_source="risk-indicators",
            variables=["thinkhazard:flood_river"],
            country="KEN",
            path=tmp_path,
        ).download()
        assert isinstance(df, pd.DataFrame)
        assert df.iloc[0]["hazard"] == "FL"

    def test_facade_gfw_vector_download_returns_feature_collection(
        self, fake_http, tmp_path: Path
    ) -> None:
        """A faked GFW geometry download via the facade returns a vector result."""
        result = EarthLens(
            data_source="gfw",
            variables=["gfw:admin_boundary"],
            country="KEN",
            api_key="fake-key",
            path=tmp_path,
        ).download()
        assert isinstance(result, FeatureCollection)
