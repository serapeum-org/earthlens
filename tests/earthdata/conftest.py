"""Shared fakes and fixtures for the Earthdata backend tests.

The whole suite runs without `earthaccess` installed or any network:
:class:`_FakeEarthaccess` is injected into `sys.modules` so the lazy
`import earthaccess` inside the backend / auth resolves to the fake.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest


class _FakeAuth:
    """Stand-in for the `earthaccess.Auth` handle a real login returns."""

    def __init__(self, authenticated: bool = True) -> None:
        self.authenticated = authenticated
        self.s3_calls: list[dict[str, Any]] = []

    def get_s3_credentials(self, daac: str | None = None, provider: str | None = None):
        """Record the call and return a dummy credentials dict."""
        self.s3_calls.append({"daac": daac, "provider": provider})
        return {"accessKeyId": "AK", "secretAccessKey": "SK", "sessionToken": "TK"}


class _FakeFile:
    """Stand-in for an fsspec file handle returned by `earthaccess.open`."""

    def __init__(self, path: str) -> None:
        self.path = path


class _FakeEarthaccess(types.ModuleType):
    """Fake `earthaccess` module recording every login / search / fetch call."""

    def __init__(self) -> None:
        super().__init__("earthaccess")
        self.login_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []
        self.open_calls: list[list[Any]] = []
        self.authenticated = True
        self.login_raises: BaseException | None = None
        self._auth = _FakeAuth()
        self.granules: list[dict[str, Any]] = [
            {"meta": {"concept-id": "G1-PROV"}},
            {"meta": {"concept-id": "G2-PROV"}},
        ]

    def login(self, strategy: str = "all", persist: bool = False, **kwargs: Any):
        """Record the login and return the fake auth handle."""
        self.login_calls.append({"strategy": strategy, "persist": persist})
        if self.login_raises is not None:
            raise self.login_raises
        self._auth.authenticated = self.authenticated
        return self._auth

    def search_data(self, count: int = -1, **kwargs: Any):
        """Record the search kwargs and return the canned granule list."""
        self.search_calls.append({"count": count, **kwargs})
        return list(self.granules)

    def download(self, granules, local_path=None, **kwargs: Any):
        """Record the download and return one fabricated path per granule."""
        self.download_calls.append(
            {
                "n": len(granules),
                "local_path": local_path,
                "show_progress": kwargs.get("show_progress"),
            }
        )
        return [str(Path(local_path) / f"g{i}.nc4") for i, _ in enumerate(granules)]

    def open(self, granules, **kwargs: Any):
        """Record the open and return one fake S3 file handle per granule."""
        self.open_calls.append(
            {"granules": list(granules), "show_progress": kwargs.get("show_progress")}
        )
        return [_FakeFile(f"/vsis3/bucket/g{i}.nc4") for i, _ in enumerate(granules)]


@pytest.fixture
def fake_earthaccess(monkeypatch: pytest.MonkeyPatch) -> _FakeEarthaccess:
    """Inject a fake `earthaccess` module so no SDK / network is touched."""
    fake = _FakeEarthaccess()
    monkeypatch.setitem(sys.modules, "earthaccess", fake)
    return fake


@pytest.fixture
def edl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set EDL env credentials and clear any AWS region hints."""
    monkeypatch.setenv("EARTHDATA_USERNAME", "user")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "pass")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
