"""Shared fakes for the RADKLIM backend tests (no network).

Provides `FakeHttp`, a drop-in for `earthlens.base.HttpClient` that serves a
canned operational directory listing from `get()` and writes canned granule
bytes from `download()` (raising a 404 `HTTPError` for names marked missing), so
the whole suite runs without touching the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests


class _Resp:
    """Minimal `requests.Response` stand-in carrying listing text."""

    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        """No-op — the fake always returns a 200 listing."""


class _MissingResp:
    """The `response` a 404 `HTTPError` carries."""

    status_code = 404


class FakeHttp:
    """Fake `HttpClient`: canned listing on `get`, canned bytes on `download`.

    Attributes:
        listing: The HTML returned by every `get()`.
        files: Map from granule file name to its canned bytes.
        missing: File names whose `download()` raises a 404.
        downloaded: File names downloaded so far (call order).
        got: URLs passed to `get()`.
    """

    def __init__(
        self,
        listing: str = "",
        files: dict[str, bytes] | None = None,
        missing: tuple[str, ...] = (),
    ) -> None:
        self.listing = listing
        self.files = files or {}
        self.missing = set(missing)
        self.downloaded: list[str] = []
        self.got: list[str] = []

    def get(self, url: str, **kwargs: object) -> _Resp:
        """Return the canned listing response, recording the URL."""
        self.got.append(url)
        return _Resp(self.listing)

    def download(
        self,
        url: str,
        dest: str | Path,
        *,
        progress: bool = True,
        expect_magic: bytes | tuple[bytes, ...] | None = None,
        **kwargs: object,
    ) -> Path:
        """Write the canned bytes for `url` to `dest`, or 404 for a missing name."""
        name = url.rsplit("/", 1)[-1]
        if name in self.missing:
            raise requests.HTTPError(response=_MissingResp())
        data = self.files.get(name, b"\x1f\x8bcanned")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        self.downloaded.append(name)
        return dest


#: One operational listing with two products, two formats, three yw timestamps.
OPERATIONAL_LISTING = """
<a href="raa01-yw_10000-2401011200-dwd---bin.hdf5">x</a>
<a href="raa01-yw_10000-2401011200-dwd---bin.bz2">x</a>
<a href="raa01-yw_10000-2401011205-dwd---bin.hdf5">x</a>
<a href="raa01-yw_10000-2401011210-dwd---bin.hdf5">x</a>
<a href="raa01-rw_10000-2401011200-dwd---bin.hdf5">x</a>
<a href="latest">x</a>
"""


@pytest.fixture
def operational_listing() -> str:
    """Return the canned operational directory-listing HTML."""
    return OPERATIONAL_LISTING
