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
        """stac plus the three endpoint aliases are registered."""
        for key in ("stac", "planetary-computer", "earth-search", "cdse"):
            assert key in EarthLens.DataSources

    def test_keys_resolve_to_stac_class(self):
        """Every STAC key resolves to earthlens.stac.STAC."""
        for key in ("stac", "planetary-computer", "earth-search", "cdse"):
            assert EarthLens.DataSources[key] is STAC


@pytest.mark.stac
@pytest.mark.integration
class TestAliasEndpointBinding:
    """The endpoint aliases pre-bind endpoint= via the registry default kwargs."""

    def test_cdse_alias_prebinds_endpoint(self, fake_pyramids, tmp_path):
        """data_source='cdse' constructs the backend bound to the cdse endpoint."""
        el = _facade(
            fake_pyramids, tmp_path, "cdse",
            variables={"sentinel-1-grd": ["vv"]}, access_key="ak", secret_key="sk",
        )
        assert isinstance(el.datasource, STAC)
        assert el.datasource._endpoint == "cdse"

    def test_earth_search_alias_prebinds_endpoint(self, fake_pyramids, tmp_path):
        """data_source='earth-search' binds the earth-search endpoint."""
        el = _facade(fake_pyramids, tmp_path, "earth-search")
        assert el.datasource._endpoint == "earth-search"

    def test_explicit_endpoint_overrides_alias_default(self, fake_pyramids, tmp_path):
        """A user endpoint= kwarg wins over the alias default."""
        el = _facade(fake_pyramids, tmp_path, "planetary-computer", endpoint="earth-search")
        assert el.datasource._endpoint == "earth-search"


@pytest.mark.stac
@pytest.mark.integration
class TestAggregateForwarded:
    """For OUTPUT_KIND='raster' the facade forwards aggregate to the backend."""

    def test_aggregate_is_forwarded_not_rejected_at_guard(self, fake_pyramids, tmp_path):
        """download(aggregate=...) passes the facade guard and reaches the STAC backend.

        The facade rejects aggregate only for vector/tabular backends; STAC is
        raster, so the kwarg is forwarded. The backend then raises its own
        NotImplementedError naming COG reduction (D6) — a different message
        from the facade guard — proving the guard did not stop it.
        """
        fake_pyramids.items_by_collection["sentinel-2-l2a"] = []
        el = _facade(fake_pyramids, tmp_path, "planetary-computer")
        with pytest.raises(NotImplementedError, match="COG") as exc:
            el.download(aggregate=object())
        assert "vector / tabular" not in str(exc.value)
