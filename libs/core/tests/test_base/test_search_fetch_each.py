"""Tests for the shared `_search_fetch_each` / `_fetch_one` C3 helpers.

These factor the per-product progress-aware composition that FIRMS and
OpenAQ previously duplicated (M3 in
the provider-API consistency alignment).
"""

from __future__ import annotations

import pytest

from earthlens.base import AbstractDataSource, RemoteProduct


class _Dummy(AbstractDataSource):
    """Minimal concrete backend exercising the per-product C3 hooks."""

    def _initialize(self):
        return None

    def _create_grid(self, lat_lim, lon_lim):
        return None

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        return None

    def download(self):
        return None

    def _api(self):
        return None

    def _search(self):
        return [RemoteProduct(id="a"), RemoteProduct(id="b")]

    def _fetch_one(self, product):
        return product.id.upper()


def _bare(search):
    """Build a _Dummy without running __init__, with a patched _search."""
    obj = _Dummy.__new__(_Dummy)
    obj._search = search  # type: ignore[method-assign]
    return obj


def test_search_fetch_each_maps_fetch_one():
    """_search_fetch_each maps _fetch_one over the searched products in order."""
    obj = _bare(lambda: [RemoteProduct(id="a"), RemoteProduct(id="b")])
    assert obj._search_fetch_each(progress_bar=False) == ["A", "B"]


def test_search_fetch_each_empty_search_short_circuits():
    """An empty _search yields [] without calling _fetch_one."""
    obj = _bare(lambda: [])
    assert obj._search_fetch_each(progress_bar=True, desc="x", unit="y") == []


def test_fetch_one_default_raises():
    """The base _fetch_one stub raises NotImplementedError naming the class."""
    obj = AbstractDataSource.__new__(_Dummy)
    AbstractDataSource._fetch_one  # ensure attribute exists
    with pytest.raises(NotImplementedError, match="_fetch_one"):
        AbstractDataSource._fetch_one(obj, RemoteProduct(id="a"))
