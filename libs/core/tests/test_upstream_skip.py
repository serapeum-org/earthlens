"""Tests for the live-e2e upstream-availability skip classifier and hook."""

from __future__ import annotations

import pytest

from earthlens.testing import is_upstream_unavailable, pytest_runtest_call


class _WithResponse(Exception):
    """An exception carrying a `response.status_code`, like `requests.HTTPError`."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.response = type("_Resp", (), {"status_code": status})()


class _Item:
    """A minimal stand-in for a pytest item exposing only the e2e marker."""

    def __init__(self, *, e2e: bool) -> None:
        self._e2e = e2e

    def get_closest_marker(self, name: str) -> object | None:
        """Return a truthy marker for `e2e` on an e2e item, else None."""
        return object() if (self._e2e and name == "e2e") else None


# (exception, expect_skip) — availability failures skip; real failures do not.
_CASES = [
    (Exception("504 Server Error: Gateway Timeout for url: https://x"), True),
    (_WithResponse("boom", 503), True),
    (_WithResponse("forbidden", 403), True),
    (ConnectionError("Failed to establish a new connection"), True),
    (TimeoutError("read timed out"), True),
    (RuntimeError("AccessDenied: Access Denied"), True),
    (Exception("CURL error: Empty reply from server"), True),
    (Exception("429 Client Error: Too Many Requests"), True),
    (ValueError("Request for 'eac4' does not match any constraint entry"), False),
    (AssertionError("expected at least one feature"), False),
    (Exception("400 Client Error: Bad Request for url: https://x"), False),
    (_WithResponse("nope", 404), False),
]


@pytest.mark.parametrize("exc, expect_skip", _CASES)
def test_classifies_availability_versus_real(exc: Exception, expect_skip: bool) -> None:
    """Availability failures return a reason; real failures return None."""
    reason = is_upstream_unavailable(exc)
    assert (reason is not None) is expect_skip, reason


def test_walks_the_cause_chain() -> None:
    """A real error chained from a transient one is still classified transient."""
    try:
        try:
            raise _WithResponse("gateway", 502)
        except Exception as cause:
            raise ValueError("wrapped") from cause
    except ValueError as exc:
        assert is_upstream_unavailable(exc) is not None


def _drive_hook(item: _Item, exc: Exception) -> None:
    """Advance the wrapper to its yield, then inject `exc` as the test outcome."""
    generator = pytest_runtest_call(item)
    next(generator)
    generator.throw(exc)


def test_hook_skips_e2e_on_upstream_error() -> None:
    """The hook converts an availability failure in an e2e test into a skip."""
    with pytest.raises(pytest.skip.Exception):
        _drive_hook(_Item(e2e=True), Exception("503 Server Error: Service Unavailable"))


def test_hook_reraises_e2e_real_failure() -> None:
    """The hook lets a genuine failure in an e2e test propagate unchanged."""
    with pytest.raises(AssertionError):
        _drive_hook(_Item(e2e=True), AssertionError("real bug"))


def test_hook_ignores_non_e2e_tests() -> None:
    """The hook never rewrites the outcome of a non-e2e test."""
    with pytest.raises(Exception, match="503"):
        _drive_hook(
            _Item(e2e=False), Exception("503 Server Error: Service Unavailable")
        )
