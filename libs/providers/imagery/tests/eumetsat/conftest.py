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

    def __init__(self, collection_id: str, store: _FakeDataStore) -> None:
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


class _FakeChain:
    """Stand-in for `eumdac.tailor_models.Chain` — records its kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        # Kept verbatim so a test can tell an omitted key from an explicit None.
        self.kwargs = dict(kwargs)
        self.product = kwargs.get("product")
        self.format = kwargs.get("format")
        # .get() collapses an omitted key and an explicit None to the same value, so
        # this cannot tell the two apart -- use .kwargs (above) for that distinction.
        self.projection = kwargs.get("projection")
        self.roi = kwargs.get("roi")
        self.filter = kwargs.get("filter")
        self.quicklook = kwargs.get("quicklook")


class _FakeRegionOfInterest:
    """Stand-in for `eumdac.tailor_models.RegionOfInterest`."""

    def __init__(self, NSWE: Any = None, **kwargs: Any) -> None:
        self.NSWE = NSWE


class _FakeFilter:
    """Stand-in for `eumdac.tailor_models.Filter`."""

    def __init__(self, bands: Any = None, **kwargs: Any) -> None:
        self.bands = bands


class _FakeCustomisation:
    """Stand-in for an `eumdac` Customisation with a scripted status sequence.

    The `statuses` list is consumed one value per `status` read until one
    remains, which then sticks — so `["QUEUED", "RUNNING", "DONE"]` drives
    a full poll loop. Records how many times `delete()` was called.
    """

    def __init__(
        self,
        statuses: list[Any] | None = None,
        outputs: list[str] | None = None,
        logfile: str = "server log tail",
        payload: bytes = b"TAILORED",
        name: str = "fake-customisation",
    ) -> None:
        self._statuses = list(statuses or ["DONE"])
        self.outputs = list(outputs if outputs is not None else ["customised.tif"])
        self._logfile = logfile
        self._payload = payload
        self.name = name
        self.deleted = 0
        self.delete_error: BaseException | None = None
        #: Optional shared ordering log, set by `_FakeDataTailor` on hand-out.
        self.events: list[tuple[str, str]] | None = None

    @property
    def status(self) -> str:
        value = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        # A scripted exception simulates a stalled / dropped poll HTTP call.
        if isinstance(value, BaseException):
            raise value
        return value

    @property
    def logfile(self) -> str:
        return self._logfile

    def stream_output(self, output: str) -> _FakeStream:
        """Yield an in-memory byte stream for `output` (name-tagged payload)."""
        return _FakeStream(BytesIO(self._payload + output.encode()))

    def delete(self) -> None:
        self.deleted += 1
        if self.events is not None:
            self.events.append(("delete", self.name))
        if self.delete_error is not None:
            raise self.delete_error
        return None

    def __str__(self) -> str:
        return self.name


class _FakeDataTailor:
    """Stand-in for `eumdac.DataTailor` — records submits, returns a scripted job.

    A test sets `.customisation` to the `_FakeCustomisation` to return (or
    seeds `.customisations` with a per-submit sequence, e.g. a FAILED job
    then a DONE one) and, optionally, `.submit_errors` with exceptions (or
    `None`) consumed one per `new_customisation` call to exercise the
    transient-submit-retry path. `.events` records the submit/delete order.
    """

    def __init__(self, token: Any) -> None:
        self.token = token
        self.submitted: list[tuple[Any, Any]] = []
        self.customisation: _FakeCustomisation | None = None
        self.customisations: list[_FakeCustomisation] = []
        self.submit_errors: list[BaseException | None] = []
        self.events: list[tuple[str, str]] = []

    def new_customisation(self, product: Any, chain: Any):
        self.submitted.append((product, chain))
        if self.submit_errors:
            error = self.submit_errors.pop(0)
            if error is not None:
                raise error
        cust = self.customisations.pop(0) if self.customisations else self.customisation
        if cust is not None:
            cust.events = self.events
            self.events.append(("submit", cust.name))
        return cust


class _FakeEumdac(types.ModuleType):
    """Fake `eumdac` module wiring the stand-ins together.

    A single `_FakeDataStore` is shared across `DataStore(...)` calls and a
    single `_FakeDataTailor` across `DataTailor(...)` calls, so a test can
    pre-load `products_for` / `tailor.customisation` and later read
    `search_calls` / `tailor.submitted`.
    """

    def __init__(self) -> None:
        super().__init__("eumdac")
        self.store = _FakeDataStore(token=None)
        self.tailor = _FakeDataTailor(token=None)
        self.tokens: list[_FakeAccessToken] = []
        self.AccessToken = self._make_token
        self.DataStore = self._make_store
        self.DataTailor = self._make_tailor
        self.tailor_models = types.SimpleNamespace(
            Chain=_FakeChain,
            RegionOfInterest=_FakeRegionOfInterest,
            Filter=_FakeFilter,
        )

    def _make_token(self, credentials, validity: int = 86400, cache: bool = True):
        token = _FakeAccessToken(credentials, validity, cache)
        self.tokens.append(token)
        return token

    def _make_store(self, token):
        self.store.token = token
        return self.store

    def _make_tailor(self, token):
        self.tailor.token = token
        return self.tailor


@pytest.fixture
def fake_eumdac(monkeypatch):
    """Install a fake `eumdac` into `sys.modules` and return it."""
    fake = _FakeEumdac()
    monkeypatch.setitem(sys.modules, "eumdac", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_eumetsat_env(request, monkeypatch):
    """Drop EUMETSAT credential env vars so unit tests start from a clean slate.

    Skipped for `e2e`-marked tests, which authenticate against the live Data
    Store and therefore need the real `EUMETSAT_CONSUMER_KEY` /
    `EUMETSAT_CONSUMER_SECRET` to survive into the test.
    """
    if request.node.get_closest_marker("e2e"):
        return
    for var in (
        "EUMETSAT_CONSUMER_KEY",
        "EUMETSAT_CONSUMER_SECRET",
        "EUMDAC_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
