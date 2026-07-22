"""Unit tests for the Sentinel Hub Catalog API scene search (C11)."""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.sentinel_hub.backend import SentinelHub

from .conftest import FakeSentinelHubCatalog

pytestmark = pytest.mark.sentinel_hub


def _backend(output_dir, variables=None, **kwargs) -> SentinelHub:
    """A small backend for catalog searches."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-03",
        variables=variables or {"sentinel-2-l2a-ndvi": []},
        lat_lim=[40.0, 40.1],
        lon_lim=[14.0, 14.1],
        path=output_dir,
        client_id="a",
        client_secret="b",
        **kwargs,
    )


class TestCatalogSearch:
    """`search()` enumerates catalog items intersecting the request."""

    def test_returns_one_product_per_item(self, fake_sh, output_dir: Path):
        """Each catalog item becomes a RemoteProduct with id + datetime."""
        products = _backend(output_dir).search()
        assert [p.id for p in products] == ["S2_TILE_A", "S2_TILE_B"]
        assert products[0].metadata["datetime"] == "2020-06-01T10:00:00Z"
        assert products[0].metadata["collection"] == "SENTINEL2_L2A"

    def test_search_passes_bbox_and_time(self, fake_sh, output_dir: Path):
        """The search is issued with the request bbox + window + limit."""
        FakeSentinelHubCatalog.searches = []
        _backend(output_dir).search(limit=50)
        assert len(FakeSentinelHubCatalog.searches) == 1
        call = FakeSentinelHubCatalog.searches[0]
        assert call["time"] == ("2020-06-01", "2020-06-03")
        assert call["limit"] == 50
        assert call["bbox"] is not None

    def test_empty_result_is_empty_list(self, fake_sh, output_dir: Path, monkeypatch):
        """An empty catalog response yields an empty list, not an error."""
        monkeypatch.setattr(FakeSentinelHubCatalog, "items", [])
        assert _backend(output_dir).search() == []

    def test_deduplicates_collections(self, fake_sh, output_dir: Path):
        """Two recipe keys on the same collection search it once."""
        backend = _backend(
            output_dir,
            variables={"sentinel-2-l2a-ndvi": [], "sentinel-2-l2a-ndwi": []},
        )
        products = backend.search()
        # both keys share SENTINEL2_L2A → one search → two items (not four)
        assert len(products) == 2
