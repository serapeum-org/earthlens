"""Tests for the live-e2e upstream-availability skip classifier and hook."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import urllib.error
from pathlib import Path

import pytest

from earthlens.base import UpstreamUnavailableError
from earthlens.testing import (
    _LIVE_SKIP_PREFIX,
    is_upstream_unavailable,
    pytest_runtest_call,
    skip_live_unavailable,
)


class _WithResponse(Exception):
    """An exception carrying a `response.status_code`, like `requests.HTTPError`."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.response = type("_Resp", (), {"status_code": status})()


class _ProviderUnavailable(UpstreamUnavailableError):
    """A stand-in provider subclass, to prove recognition is by the base type."""


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
    # 403 / AccessDenied are treated as real failures (auth-safe), not skips.
    (_WithResponse("forbidden", 403), False),
    (ConnectionError("Failed to establish a new connection"), True),
    (TimeoutError("read timed out"), True),
    (RuntimeError("AccessDenied: Access Denied"), False),
    (Exception("CURL error: Empty reply from server"), True),
    (Exception("429 Client Error: Too Many Requests"), True),
    (ValueError("Request for 'eac4' does not match any constraint entry"), False),
    (AssertionError("expected at least one feature"), False),
    (Exception("400 Client Error: Bad Request for url: https://x"), False),
    (_WithResponse("nope", 404), False),
    # A real assertion/ValueError whose text echoes a service phrase must fail,
    # not skip (message-sniffing is limited to third-party/wrapped exceptions).
    (AssertionError("body was 'Service Unavailable', expected features"), False),
    (ValueError("WCS returned a non-XML body; the service may be unavailable"), False),
    # urllib.error.HTTPError is a URLError subclass — a real 4xx must NOT skip,
    # a 5xx must; read the status from `.code`, not the network type.
    (urllib.error.HTTPError("http://x", 404, "Not Found", {}, None), False),
    (urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None), False),
    (urllib.error.HTTPError("http://x", 503, "Unavailable", {}, None), True),
]


@pytest.mark.parametrize(
    "exc, needle",
    [
        (_WithResponse("x", 503), "HTTP 503"),
        (urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, None), "HTTP 502"),
        (ConnectionError("boom"), "unreachable"),
        (Exception("CURL error: Empty reply from server"), "empty reply from server"),
    ],
)
def test_reason_text_identifies_the_signal(exc: Exception, needle: str) -> None:
    """The skip reason names the concrete availability signal it matched."""
    reason = is_upstream_unavailable(exc)
    assert reason is not None
    assert needle in reason


@pytest.mark.parametrize("exc, expect_skip", _CASES)
def test_classifies_availability_versus_real(exc: Exception, expect_skip: bool) -> None:
    """Availability failures return a reason; real failures return None."""
    reason = is_upstream_unavailable(exc)
    assert (reason is not None) is expect_skip, reason


@pytest.mark.parametrize(
    "exc",
    [
        UpstreamUnavailableError("service down after retries"),
        UpstreamUnavailableError("throttled", status_code=429),
        _ProviderUnavailable("provider down", 503),
    ],
)
def test_shared_typed_error_is_recognised(exc: UpstreamUnavailableError) -> None:
    """The base type (and any subclass) classifies as a skip, unenumerated."""
    reason = is_upstream_unavailable(exc)
    assert reason is not None
    assert type(exc).__name__ in reason


def test_shared_typed_error_recognised_when_wrapped() -> None:
    """A typed availability error reached through a cause chain is still a skip."""
    try:
        try:
            raise _ProviderUnavailable("provider down", 503)
        except UpstreamUnavailableError as cause:
            raise RuntimeError("wrapper") from cause
    except RuntimeError as exc:
        assert is_upstream_unavailable(exc) is not None


def test_walks_the_cause_chain() -> None:
    """A real error chained from a transient one is still classified transient."""
    try:
        try:
            raise _WithResponse("gateway", 502)
        except Exception as cause:
            raise ValueError("wrapped") from cause
    except ValueError as exc:
        assert is_upstream_unavailable(exc) is not None


def test_non_transient_status_is_authoritative() -> None:
    """A real 404 chained from a transient error is a failure, not a skip."""
    try:
        try:
            raise ConnectionError("earlier retry failed to connect")
        except Exception as cause:
            raise _WithResponse("not found", 404) from cause
    except Exception as exc:
        assert is_upstream_unavailable(exc) is None


def test_suppressed_context_is_not_followed() -> None:
    """`raise … from None` hides a transient context, so it is not reclassified."""
    try:
        try:
            raise _WithResponse("gateway", 503)
        except Exception:
            raise AssertionError("deliberately surfaced") from None
    except AssertionError as exc:
        assert is_upstream_unavailable(exc) is None


def _drive_hook(item: _Item, exc: Exception) -> None:
    """Advance the wrapper to its yield, then inject `exc` as the test outcome."""
    generator = pytest_runtest_call(item)
    next(generator)
    generator.throw(exc)


def test_hook_skips_e2e_on_upstream_error() -> None:
    """The hook converts an availability failure in an e2e test into a skip."""
    item = _Item(e2e=True)
    exc = Exception("503 Server Error: Service Unavailable")
    with pytest.raises(pytest.skip.Exception):
        _drive_hook(item, exc)


def test_hook_reraises_e2e_real_failure() -> None:
    """The hook lets a genuine failure in an e2e test propagate unchanged."""
    item = _Item(e2e=True)
    exc = AssertionError("real bug")
    with pytest.raises(AssertionError):
        _drive_hook(item, exc)


def test_hook_ignores_non_e2e_tests() -> None:
    """The hook never rewrites the outcome of a non-e2e test."""
    item = _Item(e2e=False)
    exc = Exception("503 Server Error: Service Unavailable")
    with pytest.raises(Exception, match="503"):
        _drive_hook(item, exc)


def test_hook_passes_through_a_passing_test() -> None:
    """The hook returns the wrapped result untouched when the test does not raise."""
    generator = pytest_runtest_call(_Item(e2e=True))
    next(generator)
    with pytest.raises(StopIteration) as raised:
        generator.send("wrapped-result")
    assert raised.value.value == "wrapped-result"


def _run_guard_lane(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run a throwaway lane wired to the hooks in a subprocess; return the result."""
    (tmp_path / "conftest.py").write_text(
        "from earthlens.testing import (  # noqa: F401\n"
        "    pytest_runtest_call,\n"
        "    pytest_sessionfinish,\n"
        ")\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    e2e: live end-to-end test\n", encoding="utf-8"
    )
    (tmp_path / "test_lane.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-p", "no:cacheprovider", "-q"],
        capture_output=True,
        text=True,
    )


@pytest.mark.slow
def test_guard_fails_a_wholly_masked_lane(tmp_path: Path) -> None:
    """A lane whose only e2e tests all availability-skip fails, not passes green."""
    result = _run_guard_lane(
        tmp_path,
        """
        import pytest

        @pytest.mark.e2e
        def test_a():
            raise Exception("503 Server Error: Service Unavailable")

        @pytest.mark.e2e
        def test_b():
            raise Exception("504 Server Error: Gateway Timeout")
        """,
    )
    assert result.returncode != 0, result.stdout
    assert "wholly masked" in result.stdout


@pytest.mark.slow
def test_guard_allows_a_lane_with_a_pass(tmp_path: Path) -> None:
    """A lane with at least one passing e2e test stays green despite a skip."""
    result = _run_guard_lane(
        tmp_path,
        """
        import pytest

        @pytest.mark.e2e
        def test_ok():
            assert True

        @pytest.mark.e2e
        def test_down():
            raise Exception("503 Server Error: Service Unavailable")
        """,
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.slow
def test_guard_allows_a_credential_skipped_lane(tmp_path: Path) -> None:
    """A lane all-skipped for missing creds (no availability skip) stays green."""
    result = _run_guard_lane(
        tmp_path,
        """
        import pytest

        @pytest.mark.e2e
        def test_no_creds():
            pytest.skip("credentials not configured")

        @pytest.mark.e2e
        @pytest.mark.skipif(True, reason="gated off")
        def test_gated():
            assert False
        """,
    )
    assert result.returncode == 0, result.stdout
    assert "masked" not in result.stdout


@pytest.mark.slow
def test_guard_fails_a_lane_mixing_creds_skip_and_availability_skip(
    tmp_path: Path,
) -> None:
    """One availability skip with zero passes fails, even beside a creds skip."""
    result = _run_guard_lane(
        tmp_path,
        """
        import pytest

        @pytest.mark.e2e
        def test_no_creds():
            pytest.skip("credentials not configured")

        @pytest.mark.e2e
        def test_down():
            raise Exception("503 Server Error: Service Unavailable")
        """,
    )
    assert result.returncode != 0, result.stdout
    assert "wholly masked" in result.stdout


def test_skip_live_unavailable_stamps_the_guard_prefix() -> None:
    """The helper skips with the shared prefix so the masked-lane guard counts it."""
    with pytest.raises(pytest.skip.Exception) as excinfo:
        skip_live_unavailable("GDACS SEARCH unavailable: boom")
    assert str(excinfo.value).startswith(_LIVE_SKIP_PREFIX), (
        f"skip reason must carry the guard prefix, got {excinfo.value!r}"
    )
    assert "GDACS SEARCH unavailable: boom" in str(excinfo.value)
