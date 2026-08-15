"""Facade-routing tests for the Aqueduct backend (`EarthLens` -> `Aqueduct`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pyramids.feature.collection import FeatureCollection

import earthlens.aqueduct
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.aqueduct


def _make_facade(tmp_path: Path, cache: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the Aqueduct backend."""
    params: dict[str, object] = dict(
        data_source="aqueduct",
        path=str(tmp_path),
        cache_dir=cache,
        return_period=100,
    )
    params.update(overrides)
    return EarthLens(**params)


class TestFacadeRouting:
    """The aqueduct keys resolve to and construct the Aqueduct backend."""

    @pytest.mark.parametrize(
        "key", ["aqueduct", "aqueduct-floods", "aqueduct-flood-risk"]
    )
    def test_keys_registered(self, key: str) -> None:
        """aqueduct and its aliases are registered data sources."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize(
        "key", ["aqueduct", "aqueduct-floods", "aqueduct-flood-risk"]
    )
    def test_keys_resolve_to_aqueduct_class(self, key: str) -> None:
        """Each aqueduct key resolves to earthlens.aqueduct.Aqueduct."""
        assert EarthLens.DataSources[key] is earthlens.aqueduct.Aqueduct

    def test_facade_builds_aqueduct_backend(
        self, tmp_path: Path, country_cache: Path
    ) -> None:
        """The facade binds an Aqueduct instance as its datasource."""
        facade = _make_facade(tmp_path, country_cache)
        assert isinstance(facade.datasource, earthlens.aqueduct.Aqueduct)

    def test_selection_kwargs_forwarded(
        self, tmp_path: Path, country_cache: Path
    ) -> None:
        """The selection kwargs ride through **backend_kwargs to the backend."""
        facade = _make_facade(
            tmp_path,
            country_cache,
            admin_level="country",
            metric="gdp_affected",
            year=2030,
            scenario="ssp2-rcp8p5",
        )
        backend = facade.datasource
        assert backend._admin_level == "country"
        assert backend._metric == "gdp_affected"
        assert backend._year == "2030"
        assert backend._scenario == "ssp2-rcp8p5"


class TestFacadeAggregateRejection:
    """A vector backend rejects `aggregate=` through the facade."""

    def test_aggregate_raises_not_implemented(
        self, tmp_path: Path, country_cache: Path
    ) -> None:
        """download(aggregate=...) on the vector backend raises."""
        facade = _make_facade(tmp_path, country_cache)
        with pytest.raises(NotImplementedError) as exc:
            facade.download(aggregate=object())
        assert "vector" in str(exc.value)


class TestFacadeDownload:
    """The facade returns the backend's FeatureCollection."""

    def test_download_returns_feature_collection(
        self, tmp_path: Path, country_cache: Path
    ) -> None:
        """A facade download routes to Aqueduct and returns the collection."""
        fc = _make_facade(tmp_path, country_cache).download(progress_bar=False)
        assert isinstance(fc, FeatureCollection)
        assert "rp_100" in fc.columns
