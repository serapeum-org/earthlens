"""Tests for the `EarthLens` facade entries that route to the soilgrids backend."""

from __future__ import annotations

import pytest
from earthlens.earthlens import EarthLens

import earthlens.soilgrids

pytestmark = pytest.mark.soilgrids

KEYS = ["soilgrids", "isric"]


@pytest.mark.unit
class TestRegistry:
    """Tests for the soilgrids entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every soilgrids key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_soilgrids_class(self, key: str) -> None:
        """Both keys resolve to `earthlens.soilgrids.SoilGrids`."""
        assert EarthLens.DataSources[key] is earthlens.soilgrids.SoilGrids

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_no_extra(self, key: str) -> None:
        """The soilgrids backend needs no optional extra, so its hint is empty."""
        assert EarthLens.DataSources.default_kwargs(key) == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="soilgrids", ...)`."""

    def test_constructs_soilgrids_backend(self) -> None:
        """The facade builds the SoilGrids backend for a property request."""
        el = EarthLens(
            data_source="soilgrids",
            variables=["clay"],
            lat_lim=[51.0, 52.0],
            lon_lim=[5.0, 6.0],
        )
        assert isinstance(el.datasource, earthlens.soilgrids.SoilGrids)
        assert el.datasource.OUTPUT_KIND == "raster"

    def test_isric_alias_constructs_same_backend(self) -> None:
        """The `isric` alias builds the same SoilGrids backend."""
        el = EarthLens(
            data_source="isric",
            variables=["phh2o"],
            lat_lim=[51.0, 52.0],
            lon_lim=[5.0, 6.0],
        )
        assert isinstance(el.datasource, earthlens.soilgrids.SoilGrids)

    def test_forwards_depths_and_quantiles(self) -> None:
        """The facade forwards depths= / quantiles= to the backend."""
        el = EarthLens(
            data_source="soilgrids",
            variables=["clay"],
            lat_lim=[51.0, 52.0],
            lon_lim=[5.0, 6.0],
            depths=["0-5cm"],
            quantiles=["Q0.05", "Q0.95"],
        )
        assert el.datasource._depths_arg == ["0-5cm"]
        assert el.datasource._quantiles_arg == ["Q0.05", "Q0.95"]

    def test_aggregate_rejected_via_facade(self) -> None:
        """The facade rejects aggregate= for the static soilgrids backend."""
        el = EarthLens(
            data_source="soilgrids",
            variables=["clay"],
            lat_lim=[51.0, 52.0],
            lon_lim=[5.0, 6.0],
        )
        with pytest.raises(NotImplementedError):
            el.download(aggregate=object())
