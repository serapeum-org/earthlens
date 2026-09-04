"""Unit tests for `earthlens.base.http.HttpRangeFile` (no network)."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
import requests

from earthlens.base.http import (
    HttpClient,
    HttpRangeFile,
    RangeReadError,
    redact_url,
)


class _RangeResp:
    """Canned response carrying a status, headers, and a body slice."""

    def __init__(
        self,
        *,
        status: int = 206,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
        url: str = "",
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.content = content
        self.url = url
        self.ok = status < 400

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        pass


class _RangeSession:
    """Serves `Range` requests from an in-memory blob, recording every call."""

    def __init__(
        self,
        blob: bytes,
        *,
        head_status: int = 200,
        send_content_length: bool = True,
        send_content_range: bool = True,
        ignore_range: bool = False,
        redirect_to: str | None = None,
    ) -> None:
        self.blob = blob
        self.head_status = head_status
        self.send_content_length = send_content_length
        self.send_content_range = send_content_range
        self.ignore_range = ignore_range
        self.redirect_to = redirect_to
        self.head_calls: list[str] = []
        self.head_kwargs: list[dict[str, Any]] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def head(self, url: str, **kwargs: Any) -> _RangeResp:
        self.head_calls.append(url)
        self.head_kwargs.append(kwargs)
        headers: dict[str, str] = {}
        if self.send_content_length and self.head_status < 400:
            headers["Content-Length"] = str(len(self.blob))
        return _RangeResp(
            status=self.head_status, headers=headers, url=self.redirect_to or url
        )

    def get(self, url: str, **kwargs: Any) -> _RangeResp:
        self.get_calls.append((url, kwargs))
        if self.ignore_range:
            return _RangeResp(status=200, content=self.blob, url=url)
        first, last = kwargs["headers"]["Range"].removeprefix("bytes=").split("-")
        chunk = self.blob[int(first) : int(last) + 1]
        headers: dict[str, str] = {}
        if self.send_content_range:
            headers["Content-Range"] = f"bytes {first}-{last}/{len(self.blob)}"
        return _RangeResp(status=206, headers=headers, content=chunk, url=url)


def _range_file(session: _RangeSession, **kwargs: Any) -> HttpRangeFile:
    """Build an `HttpRangeFile` reading through a fake session."""
    return HttpRangeFile(
        "https://example.org/blob", client=HttpClient(session=session), **kwargs
    )


def _zip_blob() -> bytes:
    """Return a small in-memory ZIP holding two members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "timeseries/csv/dk/dk_1.csv", "date,streamflow\n2020-01-01,1.5\n"
        )
        archive.writestr("attributes/dk/attributes_other_dk.csv", "gauge_id\ndk_1\n")
    return buffer.getvalue()


class TestSize:
    """Resolving the remote object's length."""

    def test_size_comes_from_the_head_content_length(self):
        """One `HEAD` suffices when the host reports a length."""
        session = _RangeSession(b"0123456789")

        handle = _range_file(session)

        assert handle.size == 10
        assert session.head_calls == ["https://example.org/blob"]
        assert session.get_calls == []

    def test_head_without_a_length_falls_back_to_a_byte_probe(self):
        """A missing `Content-Length` is recovered from `Content-Range`."""
        session = _RangeSession(b"0123456789", send_content_length=False)

        handle = _range_file(session)

        assert handle.size == 10
        assert len(session.get_calls) == 1
        assert session.get_calls[0][1]["headers"]["Range"] == "bytes=0-0"

    def test_a_rejected_head_falls_back_to_a_byte_probe(self):
        """A host answering `405` to `HEAD` is still readable."""
        session = _RangeSession(b"0123456789", head_status=405)

        assert _range_file(session).size == 10

    def test_no_length_anywhere_raises(self):
        """Without a length no offset arithmetic is possible."""
        session = _RangeSession(
            b"0123456789", send_content_length=False, send_content_range=False
        )

        with pytest.raises(ValueError, match="cannot determine the size"):
            _range_file(session)

    def test_a_known_size_skips_the_probe(self):
        """A caller-supplied size costs no round trip."""
        session = _RangeSession(b"0123456789")

        handle = _range_file(session, size=10)

        assert handle.size == 10
        assert session.head_calls == []
        assert session.get_calls == []

    def test_reads_follow_the_redirect_head_resolved(self):
        """The post-redirect URL is reused so each read skips the hop."""
        session = _RangeSession(b"0123456789", redirect_to="https://cdn.example.org/x")

        handle = _range_file(session)
        handle.read(4)

        assert handle.url == "https://cdn.example.org/x"
        assert session.get_calls[0][0] == "https://cdn.example.org/x"


class TestSeek:
    """Cursor arithmetic, which never touches the network."""

    def test_seek_from_start_current_and_end(self):
        """All three `whence` modes land on the expected offset."""
        handle = _range_file(_RangeSession(b"0123456789"))

        assert handle.seek(3) == 3
        assert handle.seek(2, io.SEEK_CUR) == 5
        assert handle.seek(-2, io.SEEK_END) == 8
        assert handle.tell() == 8

    def test_an_unknown_whence_raises(self):
        """Only 0, 1 and 2 are valid."""
        handle = _range_file(_RangeSession(b"0123456789"))

        with pytest.raises(ValueError, match="invalid whence"):
            handle.seek(0, 99)

    def test_seeking_issues_no_request(self):
        """Moving the cursor is local state only."""
        session = _RangeSession(b"0123456789")
        handle = _range_file(session)

        handle.seek(5)

        assert session.get_calls == []


class TestRead:
    """Reading windows out of the remote object."""

    def test_reads_the_requested_window(self):
        """The bytes returned are the ones the offset names."""
        handle = _range_file(_RangeSession(b"0123456789"))

        handle.seek(2)

        assert handle.read(3) == b"234"
        assert handle.tell() == 5

    def test_a_read_past_the_end_is_clamped(self):
        """Asking for more than remains gives a short read, not an error."""
        handle = _range_file(_RangeSession(b"0123456789"))

        handle.seek(8)

        assert handle.read(100) == b"89"

    def test_reading_at_the_end_returns_nothing(self):
        """A cursor at or past EOF yields empty bytes and no request."""
        session = _RangeSession(b"0123456789")
        handle = _range_file(session)

        handle.seek(10)

        assert handle.read(4) == b""
        assert session.get_calls == []

    def test_an_ignored_range_header_raises(self):
        """A `200` means the whole body came back, so later offsets would be wrong."""
        session = _RangeSession(b"0123456789", ignore_range=True)
        handle = _range_file(session, size=10)

        with pytest.raises(ValueError, match="ignored the Range header"):
            handle.read(4)

    def test_reads_disable_content_encoding(self):
        """A compressed body would not match the requested byte window."""
        session = _RangeSession(b"0123456789")
        handle = _range_file(session)

        handle.read(4)

        assert session.get_calls[0][1]["headers"]["Accept-Encoding"] == "identity"

    def test_split_timeout_reaches_the_probe_and_reads(self):
        """A (connect, read) timeout is forwarded to the size probe and the read."""
        session = _RangeSession(b"0123456789")
        handle = _range_file(session, timeout=(5.0, 30.0))

        handle.read(4)

        assert session.head_kwargs[0]["timeout"] == (5.0, 30.0)
        assert session.get_calls[0][1]["timeout"] == (5.0, 30.0)

    def test_counters_track_requests_and_bytes(self):
        """The cost of a range-read session is observable, probe included."""
        handle = _range_file(_RangeSession(b"0123456789"))

        handle.read(4)
        handle.read(3)

        # One HEAD to size the object, then the two ranged reads. The HEAD
        # carries no body, so it costs a request but no bytes.
        assert handle.request_count == 3
        assert handle.bytes_read == 7

    def test_the_file_is_readable_and_seekable_but_not_writable(self):
        """The declared capabilities are what container readers check."""
        handle = _range_file(_RangeSession(b"0123456789"))

        assert handle.readable()
        assert handle.seekable()
        assert not handle.writable()


class TestBuffered:
    """The buffered wrapper and the container-reader case it exists for."""

    def test_buffering_coalesces_many_small_reads(self):
        """Ten one-byte reads cost one request through the buffer."""
        session = _RangeSession(bytes(range(64)))
        buffered = _range_file(session, size=64).buffered(buffer_size=64)

        for _ in range(10):
            buffered.read(1)

        assert len(session.get_calls) == 1

    def test_zipfile_reads_a_member_over_the_range_reader(self):
        """The motivating case: one member out of a remote ZIP, no download."""
        blob = _zip_blob()
        session = _RangeSession(blob)
        handle = _range_file(session)

        archive = zipfile.ZipFile(handle.buffered())

        assert "timeseries/csv/dk/dk_1.csv" in archive.namelist()
        assert archive.read("timeseries/csv/dk/dk_1.csv").decode() == (
            "date,streamflow\n2020-01-01,1.5\n"
        )


class TestHostileServer:
    """Guards against a server that answers a range request badly."""

    def test_an_overlong_reply_is_truncated_to_the_window(self):
        """More bytes than asked for must not grow the buffer or desync `_pos`."""
        session = _RangeSession(bytes(range(64)))
        original_get = session.get

        def _overlong(url: str, **kwargs: Any) -> _RangeResp:
            response = original_get(url, **kwargs)
            response.content = response.content + b"\xff" * 40
            return response

        session.get = _overlong  # type: ignore[method-assign]
        handle = _range_file(session, size=64)
        buffer = bytearray(10)

        written = handle.readinto(buffer)

        assert written == 10
        assert len(buffer) == 10
        assert handle.tell() == 10

    def test_the_head_probe_disables_content_encoding(self):
        """A gzip-negotiating host would report the compressed length as size."""
        session = _RangeSession(b"0123456789")

        _range_file(session)

        assert session.head_kwargs[0]["headers"]["Accept-Encoding"] == "identity"

    def test_the_size_probe_is_counted_in_the_transfer_stats(self):
        """A budget assertion that ignores the probe under-reports the cost."""
        session = _RangeSession(b"0123456789", send_content_length=False)

        handle = _range_file(session)

        # The HEAD that returned no length, plus the one-byte Content-Range
        # probe that recovered it.
        assert handle.request_count == 2
        assert handle.bytes_read == 1


class TestSeekBounds:
    """Seeking outside the object."""

    def test_a_negative_absolute_offset_raises_oserror(self):
        """`zipfile` probes small archives with `except OSError`, so it must be one."""
        handle = _range_file(_RangeSession(b"0123456789"))

        with pytest.raises(OSError, match="negative offset"):
            handle.seek(-100, io.SEEK_END)

    def test_seeking_past_the_end_is_allowed(self):
        """As for a local file; the next read simply returns nothing."""
        handle = _range_file(_RangeSession(b"0123456789"))

        assert handle.seek(500) == 500
        assert handle.read(4) == b""


class TestClose:
    """Releasing the transport."""

    def test_closing_releases_a_client_it_created(self):
        """A reader that built its own session must not leak it."""
        closed: list[bool] = []
        handle = HttpRangeFile("https://example.org/blob", size=10)
        handle._client._session = _ClosableSession(closed)  # type: ignore[assignment]

        handle.close()

        assert handle.closed
        assert closed == [True]

    def test_closing_leaves_an_injected_client_alone(self):
        """An injected client may be shared, so it is the caller's to close."""
        closed: list[bool] = []
        handle = _range_file(_RangeSession(b"0123456789"), size=10)
        handle._client._session = _ClosableSession(closed)  # type: ignore[assignment]

        handle.close()

        assert handle.closed
        assert closed == [], "an injected client must not be closed for the caller"


class _ClosableSession:
    """Records whether the reader closed it."""

    def __init__(self, log: list[bool]) -> None:
        self._log = log

    def close(self) -> None:
        self._log.append(True)


class _FailingRangeSession(_RangeSession):
    """Answers the size probe, then fails every ranged read."""

    def __init__(self, blob: bytes, exc: BaseException) -> None:
        super().__init__(blob)
        self._exc = exc

    def get(self, url: str, **kwargs: Any) -> _RangeResp:
        """Record the attempt and raise the injected transport error."""
        self.get_calls.append((url, kwargs))
        raise self._exc


class TestRangeReadErrors:
    """A live HTTP failure must reach the caller, not look like a bad archive."""

    def test_a_transport_failure_raises_range_read_error(self):
        """The `requests` error is translated at the boundary, not leaked."""
        session = _FailingRangeSession(b"x" * 64, requests.ConnectionError("boom"))
        reader = _range_file(session)
        with pytest.raises(RangeReadError) as excinfo:
            reader.read(8)
        # Compared against the reader's own redacted URL rather than a
        # hostname literal: the point is that the message names the object
        # that failed, whatever `redact_url` leaves of it.
        assert redact_url(reader.url) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, requests.ConnectionError)

    def test_it_is_not_an_oserror(self):
        """Container readers probe with `except OSError`, which must not match."""
        assert not issubclass(RangeReadError, OSError)

    def test_a_zipfile_probe_cannot_swallow_a_live_failure(self):
        """The whole point: a 503 must not be reported as a malformed ZIP."""
        session = _FailingRangeSession(
            _zip_blob(), requests.HTTPError("503 Service Unavailable")
        )
        reader = _range_file(session)
        with pytest.raises(RangeReadError):
            zipfile.ZipFile(reader.buffered())

    def test_an_http_error_status_still_reaches_the_caller(self):
        """`raise_for_status` inside the client is a `RequestException` too."""
        session = _FailingRangeSession(b"x" * 64, requests.HTTPError("404"))
        reader = _range_file(session)
        with pytest.raises(RangeReadError, match="404"):
            reader.read(8)

    def test_a_server_ignoring_the_range_is_a_value_error_not_a_short_read(self):
        """A `200` carries the whole object, so every later offset would be wrong."""
        session = _RangeSession(b"y" * 64, ignore_range=True)
        reader = _range_file(session)
        with pytest.raises(ValueError, match="ignored the Range header"):
            reader.read(8)
