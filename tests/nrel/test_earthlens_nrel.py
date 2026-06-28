"""Tests for the `EarthLens` facade entries routing to NREL."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import earthlens.nrel
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.nrel

KEYS = ["nrel", "nsrdb", "wind-toolkit"]


@pytest.mark.unit
class TestRegistry:
    """Tests for the NREL entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every NREL key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_nrel_class(self, key: str) -> None:
        """All keys resolve to `earthlens.nrel.NREL`."""
        assert EarthLens.DataSources[key] is earthlens.nrel.NREL

    def test_nrel_key_hint_no_extra(self) -> None:
        """The backend needs no optional extra, so its default kwargs are empty."""
        assert EarthLens.DataSources.default_kwargs("nrel") == {}

    def test_alias_prebinds_product(self) -> None:
        """The `nsrdb` / `wind-toolkit` aliases pre-bind the right `product=`."""
        assert EarthLens.DataSources.default_kwargs("nsrdb") == {
            "product": "nsrdb-psm3"
        }
        assert EarthLens.DataSources.default_kwargs("wind-toolkit") == {
            "product": "wtk"
        }


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="nrel", ...)`."""

    def test_constructs_nrel_backend(self, nrel_env: None, tmp_path: Path) -> None:
        """The facade builds the NREL backend for a solar request."""
        el = EarthLens(
            data_source="nrel",
            variables=["ghi", "dni"],
            point=(39.74, -105.18),
            start="2020-01-01",
            end="2020-01-01",
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.nrel.NREL)
        assert el.datasource.OUTPUT_KIND == "tabular"

    def test_wind_toolkit_alias_selects_wtk_product(
        self, nrel_env: None, tmp_path: Path
    ) -> None:
        """The `wind-toolkit` alias routes to the WTK product."""
        el = EarthLens(
            data_source="wind-toolkit",
            variables=["windspeed_100m"],
            point=(39.74, -105.18),
            start="2012-01-01",
            end="2012-01-01",
            path=tmp_path,
        )
        assert el.datasource._product_id == "wtk"

    def test_facade_forwards_credentials(self, tmp_path: Path) -> None:
        """`api_key=` / `email=` reach the backend via `**backend_kwargs`."""
        el = EarthLens(
            data_source="nrel",
            variables=["ghi"],
            point=(39.74, -105.18),
            start="2020-01-01",
            end="2020-01-01",
            path=tmp_path,
            api_key="FORWARDED_KEY",
            email="forwarded@example.com",
        )
        assert el.datasource._auth.api_key.get_secret_value() == "FORWARDED_KEY"
        assert el.datasource._auth.email == "forwarded@example.com"

    def test_facade_download_returns_dataframe(
        self, nrel_env: None, nsrdb_csv: str, bind_session, tmp_path: Path
    ) -> None:
        """A faked download routed through the facade returns a long DataFrame."""
        from tests.nrel.conftest import FakeResponse

        bind_session(FakeResponse(text=nsrdb_csv))
        df = EarthLens(
            data_source="nrel",
            variables=["ghi", "dni"],
            point=(39.74, -105.18),
            start="2020-01-01",
            end="2020-01-01",
            path=tmp_path,
        ).download(progress_bar=False)
        assert isinstance(df, pd.DataFrame)
        assert {"lat", "lon", "year", "product"}.issubset(df.columns)
        assert df["product"].unique().tolist() == ["nsrdb-psm3"]
