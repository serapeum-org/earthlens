"""Tests for the `EarthLens` facade entries that route to the admin backend."""

from __future__ import annotations

import pytest

import earthlens.admin
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.admin

KEYS = ["admin", "admin-boundaries", "geoboundaries", "natural-earth", "tiger"]


@pytest.mark.unit
class TestRegistry:
    """Tests for the admin entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every admin key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_admin_class(self, key: str) -> None:
        """All keys resolve to `earthlens.admin.AdminBoundaries`."""
        assert EarthLens.DataSources[key] is earthlens.admin.AdminBoundaries

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_no_extra(self, key: str) -> None:
        """The admin backend needs no optional extra, so its hint is empty."""
        assert EarthLens.DataSources.default_kwargs(key) == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="admin", ...)`."""

    def test_constructs_admin_backend(self):
        """The facade builds the AdminBoundaries backend for a boundary request."""
        el = EarthLens(
            data_source="admin",
            variables=["cgaz:adm0"],
        )
        assert isinstance(el.datasource, earthlens.admin.AdminBoundaries)
        assert el.datasource.OUTPUT_KIND == "vector"

    def test_forwards_country_selector(self):
        """The facade forwards country= to the backend via backend_kwargs."""
        el = EarthLens(
            data_source="geoboundaries",
            variables=["geoboundaries:adm1"],
            country="KEN",
        )
        assert el.datasource._country == "KEN"

    def test_forwards_scale_selector(self):
        """The facade forwards scale= to the Natural Earth backend."""
        el = EarthLens(
            data_source="natural-earth",
            variables=["natural_earth:countries"],
            scale="50m",
        )
        assert el.datasource._scale == "50m"

    def test_forwards_year_and_state_selectors(self):
        """The facade forwards year= and state= to the TIGER backend."""
        el = EarthLens(
            data_source="tiger",
            variables=["tiger:tract"],
            year=2022,
            state=6,
        )
        assert el.datasource._year == 2022
        assert el.datasource._state == "06"

    def test_aggregate_rejected_via_facade(self):
        """The facade rejects aggregate= for the vector admin backend."""
        el = EarthLens(data_source="admin", variables=["cgaz:adm0"])
        with pytest.raises(NotImplementedError):
            el.download(aggregate=object())
