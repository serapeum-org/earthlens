"""Facade-routing tests for the OBIS backend (`EarthLens` -> `OBIS`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from geopandas import GeoDataFrame

import earthlens.obis
from earthlens.earthlens import EarthLens


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the OBIS backend."""
    params: dict[str, object] = dict(
        variables=["common-dolphin"],
        data_source="obis",
        start="2015-01-01",
        end="2020-12-31",
        lat_lim=[30.0, 45.0],
        lon_lim=[-10.0, 5.0],
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.obis
class TestFacadeRouting:
    """The `obis` key resolves to and constructs the OBIS backend."""

    def test_key_registered(self):
        """`obis` is among the registered data sources."""
        assert "obis" in EarthLens.DataSources

    def test_key_resolves_to_obis_class(self):
        """The `obis` key resolves to `earthlens.obis.OBIS`."""
        assert EarthLens.DataSources["obis"] is earthlens.obis.OBIS

    def test_facade_builds_obis_backend(self, tmp_path: Path):
        """The facade binds an OBIS instance as its datasource."""
        assert isinstance(_make_facade(tmp_path).datasource, earthlens.obis.OBIS)


@pytest.mark.obis
class TestFacadeDownload:
    """The facade rejects `aggregate=` and returns the FeatureCollection."""

    def test_aggregate_raises_not_implemented(self, tmp_path: Path):
        """`download(aggregate=...)` on the vector backend raises."""
        with pytest.raises(NotImplementedError, match="vector"):
            _make_facade(tmp_path).download(aggregate=object())

    def test_download_returns_feature_collection(self, tmp_path: Path, fake_obis):
        """A facade download returns the occurrence FeatureCollection."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        fc = _make_facade(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1
