"""Tests for the C3 `RemoteProduct` dataclass + search/fetch hooks.

Covers:
- `RemoteProduct` frozen dataclass shape (defaults, equality, immutability,
  default-factory independence).
- `AbstractDataSource._search()` and `._fetch()` default `NotImplementedError`.
- `AbstractDataSource._api_via_search_fetch()` composition: empty search →
  empty list; non-empty search → forwards to `_fetch`.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from earthlens.base import (
    AbstractDataSource,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)


@pytest.mark.unit
class TestRemoteProduct:
    """The `RemoteProduct` frozen dataclass — shape, defaults, immutability."""

    def test_minimal_construction_id_only(self):
        """`RemoteProduct(id=...)` works with everything else defaulted."""
        rp = RemoteProduct(id="x")
        assert rp.id == "x"
        assert rp.href is None
        assert rp.metadata == {}

    def test_full_construction(self):
        """All three fields populate as expected."""
        rp = RemoteProduct(id="x", href="s3://b/k", metadata={"k": 1})
        assert rp.id == "x"
        assert rp.href == "s3://b/k"
        assert rp.metadata == {"k": 1}

    def test_frozen_disallows_mutation(self):
        """Assigning to a field on a frozen dataclass raises `FrozenInstanceError`."""
        rp = RemoteProduct(id="x")
        with pytest.raises(FrozenInstanceError):
            rp.id = "y"

    def test_metadata_default_factory_per_instance(self):
        """Two instances do not share the default metadata dict."""
        a = RemoteProduct(id="a")
        b = RemoteProduct(id="b")
        a.metadata["k"] = 1
        assert b.metadata == {}, (
            f"default factory leaked across instances: b.metadata={b.metadata!r}"
        )

    def test_equality_value_based(self):
        """Two `RemoteProduct`s with the same fields compare equal."""
        a = RemoteProduct(id="x", href="h", metadata={"k": 1})
        b = RemoteProduct(id="x", href="h", metadata={"k": 1})
        assert a == b

    def test_inequality_different_id(self):
        """Differing `id` yields inequality."""
        a = RemoteProduct(id="x")
        b = RemoteProduct(id="y")
        assert a != b

    def test_repr_contains_id(self):
        """`repr` includes the dataclass name and the id."""
        rp = RemoteProduct(id="abc")
        text = repr(rp)
        assert "RemoteProduct" in text and "abc" in text, f"repr drift: {text!r}"


class _MinimalSource(AbstractDataSource):
    """A barely-instantiable `AbstractDataSource` for hook tests.

    Overrides only the four abstract methods (`_initialize`,
    `_check_input_dates`, `_create_grid`, `_api`) with no-ops so test
    classes can construct it and exercise `_search` / `_fetch` /
    `_api_via_search_fetch`.
    """

    def __init__(self, tmp_path: Path):
        super().__init__(
            start="2024-01-01",
            end="2024-01-01",
            variables=[],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
        )

    def _initialize(self, *args, **kwargs):
        return None

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        return TemporalExtent(
            start_date=start, end_date=end, resolution="D", dates=[]
        )

    def _create_grid(self, lat_lim, lon_lim):
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def download(self):
        return self._api()

    def _api(self):
        return self._api_via_search_fetch()


@pytest.mark.unit
class TestSearchFetchHelpers:
    """Default `_search` / `_fetch` raise; `_api_via_search_fetch` composes them."""

    @pytest.fixture
    def src(self, tmp_path):
        """A minimal concrete `AbstractDataSource` instance for hook tests."""
        return _MinimalSource(tmp_path)

    def test_default_search_raises_not_implemented(self, src):
        """Base `_search()` raises with the subclass class name in the message."""
        with pytest.raises(NotImplementedError, match="_MinimalSource"):
            src._search()

    def test_default_fetch_raises_not_implemented(self, src):
        """Base `_fetch()` raises with the subclass class name in the message."""
        with pytest.raises(NotImplementedError, match="_MinimalSource"):
            src._fetch([])

    def test_api_via_search_fetch_empty_returns_empty_list(self, src, monkeypatch):
        """When `_search()` returns `[]`, `_api_via_search_fetch()` returns `[]` (no fetch)."""
        fetch_calls = []
        monkeypatch.setattr(src, "_search", lambda: [])
        monkeypatch.setattr(
            src, "_fetch", lambda products: fetch_calls.append(products) or []
        )
        assert src._api_via_search_fetch() == []
        assert fetch_calls == [], (
            f"_fetch should not be called when search is empty: {fetch_calls!r}"
        )

    def test_api_via_search_fetch_forwards_products(self, src, monkeypatch):
        """Non-empty `_search()` is handed to `_fetch()` verbatim."""
        products = [RemoteProduct(id="a"), RemoteProduct(id="b")]
        seen = []
        monkeypatch.setattr(src, "_search", lambda: products)
        monkeypatch.setattr(
            src, "_fetch", lambda ps: seen.append(ps) or [Path(p.id) for p in ps]
        )
        result = src._api_via_search_fetch()
        assert seen == [products], f"products not forwarded verbatim: {seen!r}"
        assert result == [Path("a"), Path("b")], f"fetch result not returned: {result!r}"

    def test_subclass_overriding_only_search_and_fetch(self, tmp_path):
        """A subclass that overrides `_search`+`_fetch` (not `_api`) works via composition."""

        class _SplitSource(_MinimalSource):
            def _search(self):
                return [RemoteProduct(id="ok")]

            def _fetch(self, products):
                return [Path(p.id) for p in products]

        src = _SplitSource(tmp_path)
        # `_MinimalSource._api` already delegates to `_api_via_search_fetch`
        result = src._api()
        assert result == [Path("ok")], f"composition broke: {result!r}"
