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

import hashlib
import re
import urllib.error
from collections.abc import Generator
from typing import NoReturn

import pytest

from earthlens.base import (
    UpstreamUnavailableError,
    exception_chain,
    response_status,
    status_in_message,
)
from earthlens.config import set_cache_dir, set_output_dir


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
    for link in exception_chain(exc):
        # A backend that already judged its own failure an availability problem
        # raised the shared typed error; honour that verdict directly rather than
        # re-deriving it from a status or message.
        if isinstance(link, UpstreamUnavailableError):
            return f"upstream unavailable ({type(link).__name__})"
        # Status next: a `urllib.error.HTTPError` is also a `URLError` (in
        # `_NETWORK_EXC`), so classifying it by type would skip a real 4xx as
        # "unreachable". A definite non-transient status (400 / 403 / 404 / ...)
        # is authoritative: the request reached the service and got a real
        # answer, so it stays a failure and a deeper transient link (e.g. an
        # earlier retry's connection error) does not override it. The message
        # parse is anchored, so a response body echoed by a pytest-rewritten
        # `AssertionError` cannot spoof a status mid-string.
        status = response_status(link)
        if status is None:
            status = status_in_message(str(link), anchored=True)
        if status is not None:
            if status in _TRANSIENT_HTTP_STATUS:
                return f"upstream returned HTTP {status}"
            return None
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


def skip_live_unavailable(reason: str) -> NoReturn:
    """Skip the current live `e2e` test as an upstream-availability failure.

    Stamps the same `_LIVE_SKIP_PREFIX` the automatic :func:`pytest_runtest_call`
    hook uses, so the masked-lane guard in :func:`pytest_sessionfinish` still
    counts the skip. A backend that raises a typed availability error which its
    own e2e test catches — rather than letting the shared hook classify the raw
    exception — calls this instead of a bare `pytest.skip`, so the guard stays
    authoritative and a wholly-masked lane is never reported green.

    Args:
        reason: Human-readable reason, appended after the shared prefix.

    Raises:
        Skipped: Always — this is pytest's skip signal (never returns).
    """
    pytest.skip(f"{_LIVE_SKIP_PREFIX}{reason}")


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


#: Marks that the masked-lane guard already ran this session, so importing the
#: hook into every member conftest cannot fire it more than once.
_GUARD_RAN = pytest.StashKey[bool]()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a lane whose every e2e test was availability-skipped (masked-run guard).

    The per-test hook turns a flaky-upstream failure into a skip, which is the
    right call when *some* of a lane's tests still ran. But if a lane collected
    tests, nothing succeeded, and at least one test was skipped for upstream
    unavailability, the whole lane is masked — a green report that exercised
    nothing. That is the worst case the skip behaviour can produce, so convert an
    otherwise-passing session into a failure. Ordinary skips (missing credentials,
    marker gates) do not carry the availability prefix, so an
    all-skipped-for-other-reasons run stays green.

    Scope: this is a *session*-level hook, so its "did anything succeed?" check
    reads session-global stats. That is exact for the shipped CI model, where each
    lane runs exactly one member per session (`pytest libs/providers/<theme>` /
    `pytest libs/core`). In a combined repo-root run across all members a pass in
    one member would hide another's masking — acceptable because the guard is a
    per-lane CI safety net, not a local-run gate. It relies on the terminal
    reporter (always present under CI's `-rs -v`); a `-p no:terminal` run disables
    it, which is fine for an advisory net. A session stash key keeps it single-shot
    even though every member conftest imports it.

    Args:
        session: The finished pytest session.
        exitstatus: The exit code pytest computed for the run.
    """
    if exitstatus != pytest.ExitCode.OK or session.stash.get(_GUARD_RAN, False):
        return
    session.stash[_GUARD_RAN] = True
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is None:
        return
    # xpassed / xfailed also mean a test ran to a non-skip outcome, so they count
    # as "something succeeded" and must not be treated as a masked lane.
    if any(reporter.stats.get(kind) for kind in ("passed", "xpassed", "xfailed")):
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


@pytest.fixture(scope="session", autouse=True)
def _earthlens_dirs_scratch(tmp_path_factory):
    """Point the output and cache directories at a scratch root for the session.

    Session-scoped and autouse so the redirect is in place before *any* other
    fixture runs. A function-scoped fixture would be too late: pytest builds
    session-, package-, module- and class-scoped fixtures first, so a
    module-scoped fixture that constructs a backend without `path=` would
    resolve the developer's real `~/.earthlens/data` instead.
    """
    root = tmp_path_factory.mktemp("earthlens-dirs")
    set_output_dir(root / "data")
    set_cache_dir(root / "cache")
    yield root
    set_output_dir(None)
    set_cache_dir(None)


@pytest.fixture(autouse=True)
def isolate_earthlens_dirs(_earthlens_dirs_scratch, request):
    """Give each test its own slot beneath the session scratch root.

    A backend built without `path=` resolves to the configured output directory,
    and each backend hangs its intermediates cache off the configured cache
    directory. Left alone those are the developer's own `~/.earthlens/data` and
    per-platform user cache, so any test that downloads would write there.

    Per test, so a cache one test populates is not visible to the next — several
    tests assert on an empty or a pre-seeded cache. The slot is keyed on a hash
    of the whole node id: truncating the id would drop the discriminating path
    prefix, and long ids that end alike would share a directory. A readable
    prefix is kept so a leftover directory can still be traced back to its test.
    Resolving a directory never creates it, so an unused slot costs nothing.

    Autouse, so a member's tests get the isolation by importing this module's
    fixtures the same way they already import the HTTP transport seam.
    """
    nodeid = request.node.nodeid
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", nodeid)[-60:]
    digest = hashlib.blake2b(nodeid.encode("utf-8"), digest_size=8).hexdigest()
    root = _earthlens_dirs_scratch / f"{label}-{digest}"
    set_output_dir(root / "data")
    set_cache_dir(root / "cache")
    yield
    # Back to the session root rather than None, so anything still resolving
    # after this test — a wider-scoped teardown — stays isolated too.
    set_output_dir(_earthlens_dirs_scratch / "data")
    set_cache_dir(_earthlens_dirs_scratch / "cache")
