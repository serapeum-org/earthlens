"""Integration tests for the STAC backend through `EarthLens`."""

from __future__ import annotations

import pytest

from earthlens.earthlens import EarthLens
from earthlens.stac import STAC


def _facade(fake_pyramids, tmp_path, data_source, variables=None, **kwargs):
    """Build an EarthLens facade bound to a STAC data source over a small AOI."""
    return EarthLens(
        data_source=data_source,
        start="2024-01-01",
        end="2024-01-02",
        variables=variables or {"sentinel-2-l2a": ["B04"]},
        lat_lim=[40.0, 41.0],
        lon_lim=[-4.0, -3.0],
        path=str(tmp_path),
        **kwargs,
    )


@pytest.mark.stac
@pytest.mark.integration
class TestRouting:
    """The facade registers and resolves the STAC keys."""

    def test_keys_present(self):
        """stac plus the endpoint aliases (incl. deafrica, dea) are registered."""
        for key in (
            "stac", "planetary-computer", "earth-search", "cdse",
            "deafrica", "digital-earth-africa",
            "dea", "digital-earth-australia",
        ):
            assert key in EarthLens.DataSources

    def test_keys_resolve_to_stac_class(self):
        """Every STAC key resolves to earthlens.stac.STAC."""
        for key in (
            "stac", "planetary-computer", "earth-search", "cdse",
            "deafrica", "digital-earth-africa",
            "dea", "digital-earth-australia",
        ):
            assert EarthLens.DataSources[key] is STAC


@pytest.mark.stac
@pytest.mark.integration
class TestAliasEndpointBinding:
    """The endpoint aliases pre-bind endpoint= via the registry default kwargs."""

    def test_cdse_alias_prebinds_endpoint(self, fake_pyramids, tmp_path):
        """data_source='cdse' constructs the backend bound to the cdse endpoint."""
        el = _facade(
            fake_pyramids,
            tmp_path,
            "cdse",
            variables={"sentinel-1-grd": ["vv"]},
            access_key="ak",
            secret_key="sk",
        )
        assert isinstance(el.datasource, STAC)
        assert el.datasource._endpoint == "cdse"

    def test_earth_search_alias_prebinds_endpoint(self, fake_pyramids, tmp_path):
        """data_source='earth-search' binds the earth-search endpoint."""
        el = _facade(fake_pyramids, tmp_path, "earth-search")
        assert el.datasource._endpoint == "earth-search"

    def test_deafrica_alias_prebinds_endpoint(self, fake_pyramids, tmp_path):
        """data_source='deafrica' / 'digital-earth-africa' bind the deafrica endpoint."""
        el = _facade(
            fake_pyramids, tmp_path, "deafrica",
            variables={"deafrica/wofs_ls": ["water"]},
        )
        assert el.datasource._endpoint == "deafrica"
        el2 = _facade(
            fake_pyramids, tmp_path, "digital-earth-africa",
            variables={"deafrica/wofs_ls": ["water"]},
        )
        assert el2.datasource._endpoint == "deafrica"

    def test_dea_alias_prebinds_endpoint(self, fake_pyramids, tmp_path):
        """data_source='dea' / 'digital-earth-australia' bind the dea endpoint."""
        el = _facade(
            fake_pyramids, tmp_path, "dea",
            variables={"dea/ga_ls_wo_3": ["water"]},
        )
        assert el.datasource._endpoint == "dea"
        el2 = _facade(
            fake_pyramids, tmp_path, "digital-earth-australia",
            variables={"dea/ga_ls_wo_3": ["water"]},
        )
        assert el2.datasource._endpoint == "dea"

    def test_explicit_endpoint_overrides_alias_default(self, fake_pyramids, tmp_path):
        """A user endpoint= kwarg wins over the alias default."""
        el = _facade(
            fake_pyramids, tmp_path, "planetary-computer", endpoint="earth-search"
        )
        assert el.datasource._endpoint == "earth-search"


@pytest.mark.stac
@pytest.mark.integration
class TestAggregateForwarded:
    """For OUTPUT_KIND='raster' the facade forwards aggregate to the backend."""

    def test_aggregate_is_forwarded_and_reduced(self, fake_pyramids, tmp_path):
        """download(aggregate=...) passes the facade guard and runs the COG reducer.

        The facade rejects aggregate only for vector/tabular backends; STAC is
        raster, so the kwarg is forwarded and the backend reduces the per-date
        COGs into per-window COGs. With no matching items there are no windows,
        so the call returns an empty list (proving it was forwarded + executed,
        not rejected at the guard).
        """
        from earthlens.aggregate import AggregationConfig

        fake_pyramids.items_by_collection["sentinel-2-l2a"] = []
        el = _facade(fake_pyramids, tmp_path, "planetary-computer")
        result = el.download(aggregate=AggregationConfig(freq="1MS"))
        assert result == [], f"no items should reduce to no windows, got {result}"
