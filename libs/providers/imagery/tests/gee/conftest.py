from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_gee_module_caches():
    """Reset every process-wide GEE cache between tests.

    The `ExtentDiscovery` singleton's cache (behind `earthlens.gee`'s
    `clear_extent_cache` shim) is process-shared so repeated `GEE(...)`
    constructions against the same asset don't re-issue the 2-5 s
    `reduceColumns(minMax)` round trip — see L5 in the GEE PR review.
    Without this clear, a test that mocks `ExtentDiscovery.discover` and
    counts calls would see a stale hit from a previous test. We clear
    *before* the test runs (so any caller seeing the cache sees an empty
    one).
    """
    from earthlens.gee.backend import clear_extent_cache

    clear_extent_cache()
    yield
