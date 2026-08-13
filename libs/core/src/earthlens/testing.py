"""Shared pytest fixtures for testing earthlens and its backends.

Imported by each member distribution's `tests/conftest.py`. It lives in the
installed package rather than in a test directory because pytest's rootdir
becomes the *member* directory when a member is run on its own — which is how CI
runs them — so a repo-root `conftest.py` silently would not load and a module
under `libs/core/tests/` would not be importable. An installed module is
reachable from any rootdir.

The `numpy.testing` / `pandas.testing` precedent: shipping test support beside
the code it tests, so downstream code testing its own backend gets the same
seams rather than reinventing them.

Requires pytest, which is why it is behind the `test` extra
(`pip install earthlens-core[test]`) — importing this module without it raises
`ImportError`. Nothing in the runtime package imports it.
"""

from __future__ import annotations

import re
import urllib.error
from collections.abc import Generator, Iterator

import pytest


@pytest.fixture(autouse=True)
def unpooled_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `HttpClient`'s default transport at the `requests`-module adapter.

    Production pools: `HttpClient()` builds a `requests.Session`, so a backend
    pulling many small files from one host pays one TCP+TLS handshake instead of
    one per file. The suite, though, drives HTTP by patching `requests.get` /
    `requests.head` on the module object, and `session.get` never consults
    `requests.get` — 161 tests would fall through to the real network.

    So for the duration of a test the default becomes
    :class:`~earthlens.base.http.RequestsGet`, which re-resolves `requests` per
    call. Every module-level fake keeps driving the transport while production
    keeps its pooled session. A test that needs the shipped default asks for
    :func:`real_pooled_session`, which switches it back.

    Args:
        monkeypatch: pytest's patcher, so the seam is undone per test.
    """
    from earthlens.base import http

    monkeypatch.setattr(http, "new_session", http.RequestsGet)
    # The per-thread cache outlives a test, so a session built against the
    # previous test's transport would otherwise be handed to this one.
    http.reset_thread_local_sessions()


@pytest.fixture
def real_pooled_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the production default so a test can observe real pooling.

    Args:
        monkeypatch: pytest's patcher, so the seam is undone per test.
    """
    import requests

    from earthlens.base import http

    monkeypatch.setattr(http, "new_session", requests.Session)
    # Same reason as the unpooled fixture: the per-thread cache outlives a
    # test, so without this the pooled session under test would be whichever
    # one the previous test's transport built.
    http.reset_thread_local_sessions()


# HTTP statuses that mean "the service, not the request, is the problem":
# 408 request timeout, 425 too early, 429 rate limited, and every 5xx. 403 is
# deliberately excluded: it is ambiguous (a real auth/permission regression vs a
# public endpoint throttling a CI runner), and masking an auth failure in a gate
# is worse than an occasional throttle false-red — a throttled endpoint should
# return 429 or be handled by that test.
_TRANSIENT_HTTP_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

# Substrings (matched case-insensitively) that mark an availability failure in a
# wrapped or third-party exception whose type/status we cannot read directly
# (GDAL/pyogrio CURL errors, object-store "Access Denied", DNS failures).
_TRANSIENT_SIGNATURES: tuple[str, ...] = (
    "empty reply from server",
    "gateway time-out",
    "gateway timeout",
    "service unavailable",
    "temporarily unavailable",
    "temporary failure in name resolution",
    "name or service not known",
    "failed to establish a new connection",
    "max retries exceeded",
    "connection reset",
    "connection aborted",
    "remote end closed connection",
    "read timed out",
)

try:  # requests is a core dependency; the guard only keeps a bare import working
    import requests as _requests

    _NETWORK_EXC: tuple[type[BaseException], ...] = (
        _requests.exceptions.ConnectionError,
        _requests.exceptions.Timeout,
        ConnectionError,
        TimeoutError,
        urllib.error.URLError,
    )
except ImportError:  # pragma: no cover - requests is always present under test
    _NETWORK_EXC = (ConnectionError, TimeoutError, urllib.error.URLError)


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield `exc` then each linked `__cause__` / `__context__`, cycle-safe.

    Args:
        exc: The exception to walk.

    Yields:
        Each exception in the chain, most recent first.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        # Honour `raise … from None`: an explicit cause wins; otherwise follow
        # the implicit context unless the author suppressed it (matching
        # Python's own traceback display), so a deliberately surfaced failure is
        # not reclassified via a context it asked to hide.
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__


def _http_status(exc: BaseException) -> int | None:
    """Return the HTTP status `exc` carries, from its type, a `response`, or text.

    Handles the two stdlib/SDK shapes: `urllib.error.HTTPError` carries the status
    on `.code`, `requests.HTTPError` on `.response.status_code`. Falls back to
    parsing a leading `NNN Server/Client Error` out of the message.

    Args:
        exc: The exception to inspect.

    Returns:
        The status code, or `None` if none is discernible.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    match = re.match(r"\s*(\d{3})\s+(?:server|client)\s+error", str(exc), re.IGNORECASE)
    return int(match.group(1)) if match else None


def is_upstream_unavailable(exc: BaseException) -> str | None:
    """Classify `exc` as an external-service availability failure, or not.

    Walks the exception's cause/context chain and returns a short reason when the
    failure looks like the upstream service being unreachable or rate-limiting the
    caller — a connection/timeout error, an HTTP `408` / `425` / `429` / `5xx`, or
    a known transient message such as `Empty reply from server`. It returns `None`
    for anything else, so an assertion on the returned data, a `400`/`403`/`404`
    (including a `urllib.error.HTTPError`, read via `.code`), or a request-shaping
    error (for example a CDS constraint mismatch) stays a real failure.

    Args:
        exc: The exception raised by a live call.

    Returns:
        A human-readable skip reason, or `None` when `exc` is not an availability
        problem.
    """
    for link in _exception_chain(exc):
        # Status first: a `urllib.error.HTTPError` is also a `URLError` (in
        # `_NETWORK_EXC`), so classifying it by type would skip a real 4xx as
        # "unreachable". Read the status, and let a definite non-transient status
        # (400 / 403 / 404 / ...) stay a failure instead of falling through.
        status = _http_status(link)
        if status is not None:
            if status in _TRANSIENT_HTTP_STATUS:
                return f"upstream returned HTTP {status}"
            continue
        if isinstance(link, _NETWORK_EXC):
            return f"upstream unreachable ({type(link).__name__})"
        # Message-sniff only third-party/wrapped exceptions. An AssertionError
        # (pytest rewrites its message to include response text) or a ValueError
        # (a real request/logic error, e.g. a CRS or CDS-constraint failure)
        # could echo a service phrase and be skipped by mistake.
        if isinstance(link, (AssertionError, ValueError)):
            continue
        message = str(link).lower()
        for signature in _TRANSIENT_SIGNATURES:
            if signature in message:
                return f"upstream unavailable ({signature})"
    return None


#: Prefix stamped on every availability-skip reason, so the session-finish guard
#: can tell a hook-induced skip from an ordinary `pytest.skip` (missing creds, …).
_LIVE_SKIP_PREFIX = "live e2e skipped — "


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Turn an upstream-availability failure in a live `e2e` test into a skip.

    A live end-to-end test asserts against a real external service; when that
    service is down, throttling, or refusing the CI runner the test should report
    `skipped`, not `failed`. This wrapper inspects the exception a `e2e`-marked
    test raises and, when `is_upstream_unavailable` recognises it, re-raises it as
    a skip. Every other failure — and every non-`e2e` test — propagates unchanged.

    Args:
        item: The test item pytest is about to run.

    Returns:
        The wrapped hook's result for a passing or non-availability outcome.
    """
    try:
        return (yield)
    except Exception as exc:  # noqa: BLE001 - inspect any test failure, then re-raise
        if item.get_closest_marker("e2e") is not None:
            reason = is_upstream_unavailable(exc)
            if reason is not None:
                pytest.skip(f"{_LIVE_SKIP_PREFIX}{reason}")
        raise


def _skip_reason(report: pytest.TestReport) -> str:
    """Return a skip report's reason text (`longrepr` is a `(path, line, reason)`)."""
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr or "")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a lane whose every e2e test was availability-skipped (masked-run guard).

    The per-test hook turns a flaky-upstream failure into a skip, which is the
    right call when *some* of a lane's tests still ran. But if a lane collected
    tests, none passed, and at least one was skipped for upstream unavailability,
    the whole lane is masked — a green report that exercised nothing. That is the
    worst case the skip behaviour can produce, so convert an otherwise-passing
    session into a failure. Ordinary skips (missing credentials, marker gates) do
    not carry the availability prefix, so an all-skipped-for-other-reasons run
    stays green.

    Args:
        session: The finished pytest session.
        exitstatus: The exit code pytest computed for the run.
    """
    if exitstatus != pytest.ExitCode.OK:
        return
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is None or reporter.stats.get("passed"):
        return
    masked = sum(
        1
        for report in reporter.stats.get("skipped", [])
        if _LIVE_SKIP_PREFIX in _skip_reason(report)
    )
    if masked:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter.write_line(
            f"ERROR: all e2e tests were skipped for upstream availability "
            f"({masked} skip(s)) and none passed — failing the lane so a wholly "
            "masked run does not report green.",
            red=True,
        )
