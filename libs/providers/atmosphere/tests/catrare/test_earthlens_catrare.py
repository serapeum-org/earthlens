"""Facade-routing tests for the CatRaRE backend (`EarthLens` -> `CatRaRE`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pyramids.feature.collection import FeatureCollection

import earthlens.catrare
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.catrare


def _make_facade(cache: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the CatRaRE backend."""
    params: dict[str, object] = dict(data_source="catrare", cache_dir=cache)
    params.update(overrides)
    return EarthLens(**params)


class TestFacadeRouting:
    """The catrare key resolves to and constructs the CatRaRE backend."""

    def test_key_registered(self) -> None:
        """catrare is a registered data source."""
        assert "catrare" in EarthLens.DataSources

    def test_key_resolves_to_catrare_class(self) -> None:
        """The catrare key resolves to earthlens.catrare.CatRaRE."""
        assert EarthLens.DataSources["catrare"] is earthlens.catrare.CatRaRE

    def test_facade_builds_catrare_backend(self, catrare_cache: Path) -> None:
        """The facade binds a CatRaRE instance as its datasource."""
        assert isinstance(_make_facade(catrare_cache).datasource, earthlens.catrare.CatRaRE)

    def test_selection_kwargs_forwarded(self, catrare_cache: Path) -> None:
        """`threshold` / `geometry_layer` ride through to the backend."""
        backend = _make_facade(
            catrare_cache, threshold="w3", geometry_layer="points"
        ).datasource
        assert backend._threshold == "w3"
        assert backend._geometry_layer == "points"


class TestFacadeAggregateRejection:
    """The vector backend rejects `aggregate=` through the facade."""

    def test_aggregate_raises_not_implemented(self, catrare_cache: Path) -> None:
        """download(aggregate=...) on the vector backend raises."""
        facade = _make_facade(catrare_cache)
        with pytest.raises(NotImplementedError) as exc:
            facade.download(aggregate=object())
        assert "vector" in str(exc.value)


class TestFacadeDownload:
    """The facade returns the backend's FeatureCollection."""

    def test_download_returns_feature_collection(self, catrare_cache: Path) -> None:
        """A facade download routes to CatRaRE and returns the events."""
        fc = _make_facade(catrare_cache).download(progress_bar=False)
        assert isinstance(fc, FeatureCollection)
        assert "Event_ID" in fc.columns
