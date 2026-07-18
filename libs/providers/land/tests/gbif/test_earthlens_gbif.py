"""Facade-routing tests for the GBIF backend (`EarthLens` -> `GBIF`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from geopandas import GeoDataFrame

import earthlens.gbif
from earthlens.earthlens import EarthLens


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the GBIF backend."""
    params: dict[str, object] = dict(
        variables=["birds"],
        data_source="gbif",
        start="2020-01-01",
        end="2020-12-31",
        lat_lim=[0.0, 10.0],
        lon_lim=[0.0, 10.0],
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.gbif
class TestFacadeRouting:
    """The `gbif` key resolves to and constructs the GBIF backend."""

    def test_key_registered(self):
        """`gbif` is among the registered data sources."""
        assert "gbif" in EarthLens.DataSources

    def test_key_resolves_to_gbif_class(self):
        """The `gbif` key resolves to `earthlens.gbif.GBIF`."""
        assert EarthLens.DataSources["gbif"] is earthlens.gbif.GBIF

    def test_facade_builds_gbif_backend(self, tmp_path: Path):
        """The facade binds a GBIF instance as its datasource."""
        assert isinstance(_make_facade(tmp_path).datasource, earthlens.gbif.GBIF)


@pytest.mark.gbif
class TestFacadeDownload:
    """The facade rejects `aggregate=` and returns the FeatureCollection."""

    def test_aggregate_raises_not_implemented(self, tmp_path: Path):
        """`download(aggregate=...)` on the vector backend raises."""
        with pytest.raises(NotImplementedError, match="vector"):
            _make_facade(tmp_path).download(aggregate=object())

    def test_download_returns_feature_collection(self, tmp_path: Path, fake_gbif):
        """A facade download returns the occurrence FeatureCollection."""
        fake_gbif.occurrences.set_pages(
            [{"results": [fake_gbif.record()], "count": 1, "endOfRecords": True}]
        )
        fc = _make_facade(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1
