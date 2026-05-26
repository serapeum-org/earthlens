"""Shared fakes and fixtures for the EUMETSAT backend tests.

The whole suite runs without `eumdac` installed and without any network:
`_FakeEumdac` is injected into `sys.modules` so the lazy `import eumdac`
inside the backend / auth resolves to the fake. The fake records every
search call and streams in-memory bytes for each product.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import pytest


class _FakeAccessToken:
    """Stand-in for `eumdac.AccessToken` — records creds, never expires."""

    def __init__(self, credentials: Any, validity: int = 86400, cache: bool = True):
        self.credentials = tuple(credentials)
        self.validity = validity
        # Far-future expiry so `EumetsatAuth.is_authenticated` reports live.
        self.expiration = datetime.now() + timedelta(hours=1)


class _FakeProduct:
    """Stand-in for an `eumdac` product: streams in-memory bytes."""

    def __init__(self, product_id: str, payload: bytes = b"DATA") -> None:
        self._id = product_id
        self._payload = payload

    def __str__(self) -> str:
        return self._id

    def open(self, entry=None, chunk=None, custom_headers=None):
        """Return a context manager yielding a readable byte stream."""
        return _FakeStream(BytesIO(self._payload))


class _FakeStream:
    """Minimal context manager wrapping a `BytesIO` (what `open()` yields)."""

    def __init__(self, buffer: BytesIO) -> None:
        self._buffer = buffer

    def __enter__(self) -> BytesIO:
        return self._buffer

    def __exit__(self, *exc) -> None:
        self._buffer.close()


class _FakeCollection:
    """Stand-in for an `eumdac` collection — yields products on search."""

    def __init__(self, collection_id: str, store: "_FakeDataStore") -> None:
        self.collection_id = collection_id
        self._store = store

    def search(self, **query: Any):
        """Record the query and yield the products configured for this id."""
        self._store.search_calls.append({"collection": self.collection_id, **query})
        return iter(self._store.products_for.get(self.collection_id, []))


class _FakeDataStore:
    """Stand-in for `eumdac.DataStore` — hands out fake collections."""

    def __init__(self, token: Any) -> None:
        self.token = token
        self.search_calls: list[dict[str, Any]] = []
        # collection_id -> list[_FakeProduct]
        self.products_for: dict[str, list[_FakeProduct]] = {}

    def get_collection(self, collection_id: str) -> _FakeCollection:
        return _FakeCollection(collection_id, self)


class _FakeDataTailor:
    """Stand-in for `eumdac.DataTailor` (only needed for the deferred H4)."""

    def __init__(self, token: Any) -> None:
        self.token = token


class _FakeEumdac(types.ModuleType):
    """Fake `eumdac` module wiring the stand-ins together.

    A single `_FakeDataStore` is shared across `DataStore(...)` calls so a
    test can pre-load `products_for` and later read `search_calls`.
    """

    def __init__(self) -> None:
        super().__init__("eumdac")
        self.store = _FakeDataStore(token=None)
        self.tokens: list[_FakeAccessToken] = []
        self.AccessToken = self._make_token
        self.DataStore = self._make_store
        self.DataTailor = _FakeDataTailor

    def _make_token(self, credentials, validity: int = 86400, cache: bool = True):
        token = _FakeAccessToken(credentials, validity, cache)
        self.tokens.append(token)
        return token

    def _make_store(self, token):
        self.store.token = token
        return self.store


@pytest.fixture
def fake_eumdac(monkeypatch):
    """Install a fake `eumdac` into `sys.modules` and return it."""
    fake = _FakeEumdac()
    monkeypatch.setitem(sys.modules, "eumdac", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_eumetsat_env(monkeypatch):
    """Drop EUMETSAT credential env vars so tests start from a clean slate."""
    for var in (
        "EUMETSAT_CONSUMER_KEY",
        "EUMETSAT_CONSUMER_SECRET",
        "EUMDAC_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
