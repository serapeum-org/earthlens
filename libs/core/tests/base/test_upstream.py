"""Unit tests for the shared upstream-availability error and status helpers."""

from __future__ import annotations

import urllib.error

import pytest
import requests

from earthlens.base import (
    UpstreamUnavailableError,
    exception_chain,
    http_status,
    is_http_status,
    response_status,
    status_in_message,
)


class _WithResponse(Exception):
    """An exception carrying a `response.status_code`, like `requests.HTTPError`."""

    def __init__(self, message: str, status: object) -> None:
        super().__init__(message)
        self.response = type("_Resp", (), {"status_code": status})()


class TestUpstreamUnavailableError:
    """The shared availability error."""

    def test_carries_message_and_status(self):
        """It stores the message on the exception and the status on `.status_code`."""
        err = UpstreamUnavailableError("service down", status_code=503)
        assert str(err) == "service down", f"unexpected message: {err}"
        assert err.status_code == 503, f"unexpected status: {err.status_code}"

    def test_status_defaults_to_none(self):
        """A transport failure with no status leaves `.status_code` None."""
        assert UpstreamUnavailableError("dropped").status_code is None

    def test_is_a_runtimeerror(self):
        """It subclasses RuntimeError, so a broad transport `except` catches it."""
        assert isinstance(UpstreamUnavailableError("x"), RuntimeError)


class TestIsHttpStatus:
    """The bool-is-int guard."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (503, True),
            (200, True),
            (0, True),
            (True, False),
            (False, False),
            (None, False),
            ("503", False),
            (503.0, False),
        ],
    )
    def test_only_a_real_int_passes(self, value: object, expected: bool):
        """An int-and-not-bool passes; a bool, None, str, or float does not."""
        assert is_http_status(value) is expected, f"{value!r} -> {expected}"


class TestExceptionChain:
    """The cause/context walk."""

    def test_lone_exception_yields_only_itself(self):
        """An exception with no cause or context yields just itself."""
        exc = ValueError("solo")
        assert list(exception_chain(exc)) == [exc]

    def test_follows_explicit_cause(self):
        """`raise … from cause` is followed via `__cause__`."""
        try:
            raise RuntimeError("wrapper") from ValueError("root")
        except RuntimeError as exc:
            assert [str(link) for link in exception_chain(exc)] == ["wrapper", "root"]

    def test_follows_implicit_context(self):
        """An unsuppressed implicit `__context__` is followed."""
        try:
            try:
                raise ValueError("inner")
            except ValueError:
                raise RuntimeError("outer")  # noqa: B904 - context is the point
        except RuntimeError as exc:
            assert [str(link) for link in exception_chain(exc)] == ["outer", "inner"]

    def test_suppressed_context_is_not_followed(self):
        """`raise … from None` hides the implicit context."""
        try:
            try:
                raise ValueError("inner")
            except ValueError:
                raise RuntimeError("outer") from None
        except RuntimeError as exc:
            assert [str(link) for link in exception_chain(exc)] == ["outer"]

    def test_cycle_terminates_yielding_each_once(self):
        """A self-referential chain terminates instead of looping forever."""
        first = ValueError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__cause__ = first
        assert list(exception_chain(first)) == [first, second]


class TestResponseStatus:
    """The structural single-exception status read."""

    def test_reads_urllib_httperror_code(self):
        """A `urllib.error.HTTPError` exposes its status on `.code`."""
        exc = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
        assert response_status(exc) == 404

    def test_reads_requests_style_response(self):
        """A `.response.status_code` is read from the response object."""
        assert response_status(_WithResponse("boom", 503)) == 503

    def test_rejects_bool_status(self):
        """A `bool` on `.response.status_code` is not a real status."""
        assert response_status(_WithResponse("boom", True)) is None

    def test_rejects_non_int_status(self):
        """A non-int `.response.status_code` yields None."""
        assert response_status(_WithResponse("boom", "503")) is None

    def test_no_structure_yields_none(self):
        """An exception with neither shape yields None."""
        assert response_status(ValueError("no status here")) is None


class TestStatusInMessage:
    """The `NNN Server/Client Error` text fallback."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("500 Server Error: boom", 500),
            ("404 Client Error: Not Found", 404),
            ("500 SERVER ERROR", 500),
            ("boom", None),
            ("", None),
        ],
    )
    def test_default_search_anywhere(self, text: str, expected: int | None):
        """Unanchored, a status is matched wherever it appears (or not at all)."""
        assert status_in_message(text) == expected, f"{text!r} -> {expected}"

    def test_buried_status_needs_unanchored(self):
        """A mid-string status is found unanchored but rejected when anchored."""
        assert status_in_message("failed: 502 Server Error") == 502
        assert status_in_message("failed: 502 Server Error", anchored=True) is None

    def test_leading_status_matches_either_way(self):
        """A leading status is found whether anchored or not."""
        assert status_in_message("503 Server Error: x") == 503
        assert status_in_message("503 Server Error: x", anchored=True) == 503


class TestHttpStatus:
    """The walking best-effort extractor."""

    def test_reads_structural_status_first(self):
        """A `.response.status_code` on the outermost link is returned."""
        assert http_status(_WithResponse("boom", 503)) == 503

    def test_parses_message_when_no_response(self):
        """A `raise_for_status` message with no response yields its status."""
        assert http_status(requests.HTTPError("400 Client Error: Bad Request")) == 400

    def test_walks_chain_to_a_wrapped_status(self):
        """A status buried in a `__cause__` is recovered."""
        try:
            raise RuntimeError("wrapper") from _WithResponse("gateway", 502)
        except RuntimeError as exc:
            assert http_status(exc) == 502

    def test_unanchored_message_in_a_link(self):
        """A status mid-message is found (the extractor is unanchored)."""
        assert http_status(RuntimeError("failed: 503 Server Error")) == 503

    def test_transport_error_has_no_status(self):
        """A bare connection error carries no status."""
        assert http_status(requests.ConnectionError("boom")) is None

    def test_cycle_terminates(self):
        """A self-referential chain with no status terminates and returns None."""
        first = ValueError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__cause__ = first
        assert http_status(first) is None
