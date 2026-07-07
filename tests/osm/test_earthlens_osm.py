"""Facade-routing tests for the OSM backend (`EarthLens` -> `OSM`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from geopandas import GeoDataFrame

import earthlens.osm
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.osm

#: Every facade key that must resolve to the OSM backend.
OSM_KEYS = ["osm", "openstreetmap", "overpass", "ohsome"]


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the OSM backend."""
    params: dict[str, object] = dict(
        data_source="osm",
        variables=["overpass:hospitals"],
        lat_lim=[49.40, 49.42],
        lon_lim=[8.67, 8.71],
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


class TestKeysPresent:
    """The osm key and its aliases are registered and resolve to OSM."""

    @pytest.mark.parametrize("key", OSM_KEYS)
    def test_key_registered(self, key):
        """Each OSM key is among the registered data sources."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", OSM_KEYS)
    def test_key_resolves_to_osm_class(self, key):
        """Each OSM key resolves to earthlens.osm.OSM."""
        assert EarthLens.DataSources[key] is earthlens.osm.OSM


class TestFacadeRouting:
    """Constructing and forwarding through the facade."""

    def test_facade_builds_osm_backend(self, tmp_path: Path):
        """The facade binds an OSM instance as its datasource."""
        assert isinstance(_make_facade(tmp_path).datasource, earthlens.osm.OSM)

    def test_backend_kwargs_forwarded(self, tmp_path: Path):
        """The endpoint kwarg rides through **backend_kwargs."""
        facade = _make_facade(tmp_path, endpoint="https://example.org/api")
        assert facade.datasource._endpoint == "https://example.org/api"

    def test_pbf_kwargs_forwarded(self, tmp_path: Path):
        """region= / engine= / cache_dir= ride through to the pbf backend."""
        facade = _make_facade(
            tmp_path,
            variables=["pbf:buildings"],
            region="malta",
            engine="pyosmium",
            cache_dir=str(tmp_path / "cache"),
        )
        assert facade.datasource._region == "malta"
        assert facade.datasource._engine == "pyosmium"
        assert facade.datasource._cache_dir == tmp_path / "cache"


class TestFacadeDownload:
    """The facade returns the backend's FeatureCollection."""

    def test_download_returns_feature_collection(
        self, tmp_path: Path, fake_overpy, fake_overpass_post
    ):
        """A facade download returns the OSM FeatureCollection."""
        fc = _make_facade(tmp_path).download(progress_bar=False)
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 3

    def test_aggregate_rejected_via_facade(self, tmp_path: Path):
        """A vector backend rejects aggregate= through the facade guard."""
        with pytest.raises(NotImplementedError, match="vector"):
            _make_facade(tmp_path).download(aggregate=object())
