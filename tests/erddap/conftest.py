"""Shared fixtures for the ERDDAP backend tests (faked erddapy + HTTP)."""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest


class FakeErddapClient:
    """Records ctor kwargs + set attributes; realises tabledap via `to_pandas`.

    Deliberately exposes **no** `to_xarray` / `get_download_url`, so any
    accidental use by the backend raises `AttributeError` and the
    xarray-free contract is enforced by construction.
    """

    #: The most recently constructed instance (one request per test).
    last: FakeErddapClient | None = None
    #: Frame `to_pandas` returns; set per test.
    frame: pd.DataFrame | None = None
    #: Exception `to_pandas` raises instead of returning; set per test.
    error: Exception | None = None

    def __init__(
        self, server: str, protocol: str | None = None, response: str = "html"
    ):
        self.server = server
        self.protocol = protocol
        self.dataset_id: str | None = None
        self.variables: list[str] | None = None
        self.constraints: dict | None = None
        type(self).last = self

    def to_pandas(self) -> pd.DataFrame:
        """Return the configured frame, or raise the configured error."""
        if type(self).error is not None:
            raise type(self).error
        return type(self).frame.copy()


@pytest.fixture
def fake_erddapy(monkeypatch):
    """Install a fake `erddapy` module exposing :class:`FakeErddapClient`."""
    module = types.ModuleType("erddapy")
    module.ERDDAP = FakeErddapClient
    FakeErddapClient.last = None
    FakeErddapClient.frame = None
    FakeErddapClient.error = None
    monkeypatch.setitem(sys.modules, "erddapy", module)
    return FakeErddapClient


#: Canned griddap body — valid NetCDF-3 magic (`CDF\x01`) so the backend's
#: magic-byte guard accepts it as real data.
FAKE_NETCDF_BYTES = b"CDF\x01earthlens-fake-netcdf-body"


class FakeResponse:
    """Minimal `requests` response carrying canned NetCDF bytes."""

    def __init__(
        self, content: bytes = FAKE_NETCDF_BYTES, error: Exception | None = None
    ):
        self.content = content
        self._error = error

    def raise_for_status(self) -> None:
        """Raise the configured error, if any."""
        if self._error is not None:
            raise self._error


@pytest.fixture
def fake_nc_get(monkeypatch):
    """Stub `backend.requests.get`; returns the recorded URL list."""
    calls: list[str] = []

    def _get(url: str, timeout: float | None = None) -> FakeResponse:
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr("earthlens.erddap.backend.requests.get", _get)
    return calls
