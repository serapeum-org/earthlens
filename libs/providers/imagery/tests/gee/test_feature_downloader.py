"""Tests for the FeatureDownloader vector-download class."""

from __future__ import annotations

import geopandas as gpd
import pytest
from pyramids.feature import FeatureCollection

from earthlens.gee.vector import download as download_module
from earthlens.gee.vector.download import FeatureDownloader, FeatureDownloaderConfig


class _FakeFC:
    """Stands in for an `ee.FeatureCollection`, returning a GeoJSON payload."""

    def __init__(self, info):
        self._info = info

    def getInfo(self):  # noqa: N802 - mirrors the Earth Engine method name
        return self._info


class TestFeatureDownloaderConfig:
    """The frozen budget a downloader carries."""

    def test_defaults_match_the_io_helpers(self):
        """The default pool/retry budget equals the io functions' defaults."""
        config = FeatureDownloaderConfig()
        assert (config.pool_size, config.tries, config.backoff) == (25, 5, 2.0)

    def test_it_is_frozen(self):
        """The budget cannot drift after construction."""
        config = FeatureDownloaderConfig(tries=3)
        with pytest.raises(Exception, match="frozen"):
            config.tries = 9


class TestDelegation:
    """to_dataframe / to_dataframes delegate to the io helpers."""

    def test_to_dataframe_forwards_fc_and_selectors(self, monkeypatch):
        """to_dataframe passes the collection and selectors straight through."""
        calls = []
        monkeypatch.setattr(
            download_module._io,
            "feature_collection_to_dataframe",
            lambda fc, selectors: calls.append((fc, selectors)) or "frame",
        )
        result = FeatureDownloader().to_dataframe("fc", selectors=["a"])
        assert result == "frame"
        assert calls == [("fc", ["a"])]

    def test_to_dataframes_threads_the_instance_budget(self, monkeypatch):
        """to_dataframes passes this instance's pool/tries/backoff to the io path."""
        captured = {}
        monkeypatch.setattr(
            download_module._io,
            "feature_collections_to_dataframe",
            lambda fcs, *, pool_size, tries, backoff: (
                captured.update(
                    fcs=list(fcs), pool_size=pool_size, tries=tries, backoff=backoff
                )
                or "frames"
            ),
        )
        result = FeatureDownloader(pool_size=7, tries=3, backoff=1.5).to_dataframes(
            ["a", "b"]
        )
        assert result == "frames"
        assert captured == {
            "fcs": ["a", "b"],
            "pool_size": 7,
            "tries": 3,
            "backoff": 1.5,
        }


class TestToGdf:
    """to_gdf returns a pyramids FeatureCollection built via getInfo."""

    def test_it_returns_a_feature_collection(self):
        """A point feature with a property becomes a one-row FeatureCollection."""
        payload = {
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
                    "properties": {"name": "a"},
                }
            ]
        }
        result = FeatureDownloader().to_gdf(_FakeFC(info=payload), crs=4326)
        assert isinstance(result, FeatureCollection)
        assert isinstance(result, gpd.GeoDataFrame)
        assert len(result) == 1
        assert result.iloc[0]["name"] == "a"
        assert result.crs == "EPSG:4326"

    def test_an_empty_collection_is_an_empty_feature_collection(self):
        """No features yields an empty, CRS-stamped FeatureCollection."""
        result = FeatureDownloader().to_gdf(_FakeFC(info={"features": []}))
        assert isinstance(result, FeatureCollection)
        assert len(result) == 0
