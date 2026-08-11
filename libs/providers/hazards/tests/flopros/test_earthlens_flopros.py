"""Facade-routing tests for the FLOPROS backend (`EarthLens` -> `FLOPROS`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pyramids.feature.collection import FeatureCollection

import earthlens.flopros
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.flopros


def _make_facade(cache: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the FLOPROS backend."""
    params: dict[str, object] = dict(data_source="flopros", cache_dir=cache)
    params.update(overrides)
    return EarthLens(**params)


class TestFacadeRouting:
    """The flopros key resolves to and constructs the FLOPROS backend."""

    def test_key_registered(self) -> None:
        """flopros is a registered data source."""
        assert "flopros" in EarthLens.DataSources

    def test_key_resolves_to_flopros_class(self) -> None:
        """The flopros key resolves to earthlens.flopros.FLOPROS."""
        assert EarthLens.DataSources["flopros"] is earthlens.flopros.FLOPROS

    def test_facade_builds_flopros_backend(self, flopros_cache: Path) -> None:
        """The facade binds a FLOPROS instance as its datasource."""
        facade = _make_facade(flopros_cache)
        assert isinstance(facade.datasource, earthlens.flopros.FLOPROS)

    def test_selection_kwargs_forwarded(self, flopros_cache: Path) -> None:
        """`layer` / `country` ride through **backend_kwargs to the backend."""
        backend = _make_facade(
            flopros_cache, layer="merged_riverine", country="Betaland"
        ).datasource
        assert list(backend._layers) == ["merged_riverine"]
        assert backend._country == "Betaland"


class TestFacadeAggregateRejection:
    """The vector backend rejects `aggregate=` through the facade."""

    def test_aggregate_raises_not_implemented(self, flopros_cache: Path) -> None:
        """download(aggregate=...) on the vector backend raises."""
        facade = _make_facade(flopros_cache)
        with pytest.raises(NotImplementedError) as exc:
            facade.download(aggregate=object())
        assert "vector" in str(exc.value)


class TestFacadeDownload:
    """The facade returns the backend's FeatureCollection."""

    def test_download_returns_feature_collection(self, flopros_cache: Path) -> None:
        """A facade download routes to FLOPROS and returns the collection."""
        fc = _make_facade(flopros_cache, layer="merged_riverine").download(
            progress_bar=False
        )
        assert isinstance(fc, FeatureCollection)
        assert "merged_riverine" in fc.columns
