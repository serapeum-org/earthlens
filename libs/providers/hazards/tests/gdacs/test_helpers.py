"""Unit tests for the GDACS availability classifier (`earthlens.gdacs._helpers`)."""

from __future__ import annotations

import requests

from earthlens.gdacs._helpers import (
    GDACS_RETRY_STATUSES,
    GdacsUnavailableError,
    gdacs_http_status,
    service_failure_reason,
)


class TestGdacsHttpStatus:
    """`gdacs_http_status` recovers the status from either exception shape."""

    def test_status_from_message(self):
        """A bare raise_for_status message yields its leading status code."""
        assert (
            gdacs_http_status(requests.HTTPError("400 Client Error: Bad Request"))
            == 400
        )
        assert gdacs_http_status(requests.HTTPError("503 Server Error")) == 503

    def test_status_from_response(self):
        """A status carried on `.response.status_code` is preferred."""
        response = requests.Response()
        response.status_code = 429
        err = requests.HTTPError("rate limited", response=response)
        assert gdacs_http_status(err) == 429

    def test_no_status_for_transport_error(self):
        """A connection/timeout error carries no status."""
        assert gdacs_http_status(requests.ConnectionError("boom")) is None
        assert gdacs_http_status(requests.ReadTimeout("read timed out")) is None

    def test_walks_exception_chain(self):
        """An explicit `raise ... from` cause still yields the underlying status."""
        try:
            try:
                raise requests.HTTPError("500 Server Error")
            except requests.HTTPError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            assert gdacs_http_status(outer) == 500

    def test_follows_implicit_context(self):
        """A bare re-raise inside `except` exposes the status via `__context__`."""
        try:
            try:
                raise requests.HTTPError("503 Server Error")
            except requests.HTTPError:
                raise RuntimeError("wrapped")
        except RuntimeError as outer:
            assert gdacs_http_status(outer) == 503

    def test_suppressed_context_hides_the_status(self):
        """`raise ... from None` suppresses the context, so no status is recovered."""
        try:
            try:
                raise requests.HTTPError("500 Server Error")
            except requests.HTTPError:
                raise RuntimeError("wrapped") from None
        except RuntimeError as outer:
            assert gdacs_http_status(outer) is None


class TestServiceFailureReason:
    """`service_failure_reason` separates availability failures from real errors."""

    def test_service_statuses_are_failures(self):
        """Every retry-worthy status (incl. the spurious 400) is an availability failure."""
        for status in GDACS_RETRY_STATUSES:
            err = requests.HTTPError(f"{status} Server Error")
            assert service_failure_reason(err) == f"HTTP {status}"

    def test_transport_errors_are_failures(self):
        """A connection or timeout error is an availability failure."""
        assert "ConnectionError" in (
            service_failure_reason(requests.ConnectionError("down")) or ""
        )
        assert "ReadTimeout" in (
            service_failure_reason(requests.ReadTimeout("read timed out")) or ""
        )

    def test_authoritative_statuses_are_not_failures(self):
        """A 403 / 404 is a genuine error, not an availability one."""
        assert service_failure_reason(requests.HTTPError("403 Client Error")) is None
        assert service_failure_reason(requests.HTTPError("404 Client Error")) is None

    def test_unrelated_error_is_not_a_failure(self):
        """A non-transport, non-HTTP error is not classified as unavailable."""
        assert service_failure_reason(ValueError("bad json")) is None


def test_unavailable_error_carries_status():
    """The typed error stores the originating status for callers to branch on."""
    err = GdacsUnavailableError("unavailable", status_code=400)
    assert err.status_code == 400
    assert isinstance(err, RuntimeError)
