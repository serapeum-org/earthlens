"""Facade-routing tests for the FDSN backend (`EarthLens` -> `FDSN`).

The headline test is the aggregate-rejection path: FDSN is the first
`vector` backend, so this is the first end-to-end exercise of the
facade's `OUTPUT_KIND`-driven `aggregate=` guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from geopandas import GeoDataFrame

import earthlens.fdsn
from earthlens.earthlens import EarthLens

from .conftest import _FakeFdsn


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the FDSN backend."""
    params: dict[str, object] = dict(
        variables=["USGS"],
        data_source="fdsn",
        start="2024-01-01",
        end="2024-01-31",
        lat_lim=[30.0, 45.0],
        lon_lim=[130.0, 145.0],
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.fdsn
class TestFacadeRouting:
    """The `fdsn` key resolves to and constructs the FDSN backend."""

    def test_key_registered(self):
        """`fdsn` is among the registered data sources."""
        assert "fdsn" in EarthLens.DataSources

    def test_key_resolves_to_fdsn_class(self):
        """The `fdsn` key resolves to `earthlens.fdsn.FDSN`."""
        assert EarthLens.DataSources["fdsn"] is earthlens.fdsn.FDSN

    def test_facade_builds_fdsn_backend(self, tmp_path: Path):
        """The facade binds an FDSN instance as its datasource."""
        facade = _make_facade(tmp_path)
        assert isinstance(facade.datasource, earthlens.fdsn.FDSN)

    def test_backend_kwargs_forwarded(self, tmp_path: Path):
        """Filter kwargs ride through `**backend_kwargs` to the backend."""
        facade = _make_facade(tmp_path, min_magnitude=6.0, event_type="earthquake")
        assert facade.datasource._min_magnitude == 6.0
        assert facade.datasource._event_type == "earthquake"


@pytest.mark.fdsn
class TestFacadeAggregateRejection:
    """The headline guard: a vector backend rejects `aggregate=`."""

    def test_aggregate_raises_not_implemented(self, tmp_path: Path):
        """`download(aggregate=...)` on the vector backend raises."""
        facade = _make_facade(tmp_path)
        with pytest.raises(NotImplementedError) as exc:
            facade.download(aggregate=object())
        assert "vector" in str(exc.value), (
            f"rejection message should mention 'vector', got: {exc.value}"
        )

    def test_aggregate_none_is_allowed(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """`aggregate=None` is fine and a normal download runs."""
        facade = _make_facade(tmp_path)
        fc = facade.download()
        assert isinstance(fc, GeoDataFrame)


@pytest.mark.fdsn
class TestFacadeDownload:
    """The facade returns the backend's FeatureCollection."""

    def test_download_returns_feature_collection(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn
    ):
        """A facade download returns the unioned FeatureCollection."""
        fc = _make_facade(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1, f"expected 1 event, got {len(fc)}"


@pytest.mark.fdsn
class TestFacadeShakemapKeywords:
    """The ShakeMap keywords ride through the facade's `**backend_kwargs`."""

    def test_shakemap_keywords_forwarded(self, tmp_path: Path):
        """with_shakemap, layers and the ceiling reach the backend."""
        facade = _make_facade(
            tmp_path,
            with_shakemap=True,
            shakemap_layers=["mmi_mean", "pga_mean"],
            max_shakemap_events=7,
        )
        backend = facade.datasource
        assert backend._with_shakemap is True
        assert backend._shakemap_layers == ("mmi_mean", "pga_mean")
        assert backend._max_shakemap_events == 7

    def test_output_kind_unchanged_by_shakemap(self, tmp_path: Path):
        """The facade still sees a vector backend with the flag on."""
        facade = _make_facade(tmp_path, with_shakemap=True)
        assert facade.datasource.OUTPUT_KIND == "vector"

    def test_force_is_a_download_argument(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """force= is accepted by download(), not by the constructor."""
        facade = _make_facade(tmp_path)
        fc = facade.download(force=True)
        assert isinstance(fc, GeoDataFrame)

    def test_force_with_shakemap_refetches(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """force= through the facade actually causes a second fetch."""
        facade = _make_facade(tmp_path, with_shakemap=True)
        facade.download()
        facade.download()
        facade.download(force=True)

        assert facade.datasource._force is True, "force should reach the backend"
