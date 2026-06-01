"""Unit tests for the GHSL catalog refresh tool's offline URL sampling."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOL = (
    Path(__file__).resolve().parents[2] / "tools" / "ghsl" / "refresh_ghsl_catalog.py"
)
_spec = importlib.util.spec_from_file_location("ghsl_refresh_tool", _TOOL)
refresh_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh_tool)


@pytest.mark.ghsl
class TestSampleUrls:
    """`_sample_urls` builds one HEAD-checkable URL per availability block."""

    def test_multi_block_product_yields_one_url_per_block(self):
        """A 2-block product (GHS_BUILT_S R2023A) yields 2 sample URLs."""
        from earthlens.ghsl.catalog import Catalog

        urls = refresh_tool._sample_urls(Catalog(), "GHS_BUILT_S", "R2023A")
        assert len(urls) == 2, f"expected one URL per block, got {urls}"
        assert any("_54009_10_" in u for u in urls), "the 2018 10 m block must appear"

    def test_single_block_product_yields_one_url(self):
        """A single-block product (GHS_POP R2023A) yields one sample URL."""
        from earthlens.ghsl.catalog import Catalog

        urls = refresh_tool._sample_urls(Catalog(), "GHS_POP", "R2023A")
        assert len(urls) == 1, f"expected one URL, got {urls}"
