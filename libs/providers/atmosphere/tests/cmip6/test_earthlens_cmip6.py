"""Tests for the `EarthLens` facade entries that route to the CMIP6 backend."""

from __future__ import annotations

import pytest

import earthlens.cmip6
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.cmip6, pytest.mark.unit]

KEYS = ["cmip6", "pangeo-cmip6", "cmip6:climate-projections"]


@pytest.mark.parametrize("key", KEYS)
def test_keys_present(key):
    """Every CMIP6 key is registered in `EarthLens.DataSources`."""
    assert key in EarthLens.DataSources


@pytest.mark.parametrize("key", KEYS)
def test_keys_resolve_to_cmip6_class(key):
    """All CMIP6 keys resolve to `earthlens.cmip6.CMIP6`."""
    assert EarthLens.DataSources[key] is earthlens.cmip6.CMIP6


@pytest.mark.parametrize("key", KEYS)
def test_keys_hint_no_extra(key):
    """The CMIP6 backend needs no optional extra, so its hint is empty."""
    assert EarthLens.DataSources.default_kwargs(key) == {}


def test_facade_facet_call_builds_backend():
    """The facet-only facade call (no variables=) builds the CMIP6 backend."""
    el = EarthLens(
        "cmip6",
        source_id="CanESM5",
        experiment_id="ssp585",
        variable_id="tas",
        table_id="Amon",
        member_id="r1i1p1f1",
        start="2050-01-01",
        end="2050-12-31",
        lat_lim=[35, 60],
        lon_lim=[-10, 30],
    )
    backend = el.datasource
    assert isinstance(backend, earthlens.cmip6.CMIP6)
    assert backend.OUTPUT_KIND == "raster"
    assert backend._variable_id == "tas"
    assert backend._source_id == "CanESM5"
    assert backend._member_id == "r1i1p1f1"


@pytest.mark.parametrize("alias", ["pangeo-cmip6", "cmip6:climate-projections"])
def test_facade_aliases_build_backend(alias):
    """The CMIP6 aliases build the same backend as the canonical key."""
    el = EarthLens(
        alias,
        source_id="GFDL-ESM4",
        experiment_id="ssp585",
        variable_id="pr",
        table_id="day",
        start="2050-01-01",
        end="2050-12-31",
    )
    assert isinstance(el.datasource, earthlens.cmip6.CMIP6)


def test_facade_rejects_variables():
    """Passing variables= to the facet-only CMIP6 backend is rejected."""
    with pytest.raises(ValueError, match="facet keyword arguments"):
        EarthLens(
            "cmip6",
            variables=["tas"],
            source_id="CanESM5",
            experiment_id="ssp585",
            variable_id="tas",
            table_id="Amon",
            start="2050-01-01",
            end="2050-12-31",
        )


def test_facade_rejects_dataset():
    """Passing dataset= to the facet-only CMIP6 backend is rejected."""
    with pytest.raises(ValueError, match="facet keyword arguments"):
        EarthLens(
            "cmip6",
            dataset="Amon",
            source_id="CanESM5",
            experiment_id="ssp585",
            variable_id="tas",
            table_id="Amon",
            start="2050-01-01",
            end="2050-12-31",
        )


def test_facade_forwards_optional_facets():
    """Optional facets (grid_label, version, whole_time) reach the backend."""
    el = EarthLens(
        "cmip6",
        source_id="CanESM5",
        experiment_id="ssp585",
        variable_id="tas",
        table_id="Amon",
        grid_label="gn",
        version="20190429",
        whole_time=True,
        start="2050-01-01",
        end="2050-12-31",
    )
    assert el.datasource._grid_label == "gn"
    assert el.datasource._version == "20190429"
    assert el.datasource._whole_time is True


def test_facade_missing_facet_friendly_error():
    """Omitting a required facet entirely raises a friendly ValueError."""
    with pytest.raises(ValueError, match="requires a non-empty variable_id"):
        EarthLens(
            "cmip6",
            source_id="CanESM5",
            experiment_id="ssp585",
            table_id="Amon",
            start="2050-01-01",
            end="2050-12-31",
        )


def test_variables_backend_still_requires_variables():
    """A normal (variables-based) backend still requires variables=."""
    with pytest.raises(ValueError, match="variables= is required"):
        EarthLens("chc", start="2020-01-01", end="2020-01-02")
