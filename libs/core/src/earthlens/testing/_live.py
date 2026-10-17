"""Live-test gating: the availability classifier, the skip helpers and the guards.

One idiom for "do not run this live test right now", so both masked-lane guards
can tell an upstream outage from an unconfigured backend:

- `require_reachable` (and the `requires_host` marker that calls it) —
  probe a host once per session and skip, prefixed, when it is unreachable.
- `skip_on_upstream` — classify an exception a test caught itself and
  either skip, prefixed, or re-raise it unchanged.
- `skip_live_unavailable` — the raw prefixed skip the two above build on.

Split out of the former single-module `earthlens.testing`; import from
`earthlens.testing`, not from here.
"""

from __future__ import annotations

import re
import socket
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
from earthlens.testing._tagging import tag_items_by_directory

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

    Examples:
        - A dropped connection is an availability failure, named by its type:
            ```python
            >>> import requests
            >>> from earthlens.testing import is_upstream_unavailable
            >>> is_upstream_unavailable(requests.ConnectionError("boom"))
            'upstream unreachable (ConnectionError)'

            ```
        - A request error is not, so it stays a real failure:
            ```python
            >>> from earthlens.testing import is_upstream_unavailable
            >>> is_upstream_unavailable(ValueError("does not match any constraint")) is None
            True

            ```
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

    Stamps the same `_LIVE_SKIP_PREFIX` the automatic `pytest_runtest_call`
    hook uses, so the masked-lane guard in `pytest_sessionfinish` still
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


def skip_on_upstream(exc: BaseException) -> NoReturn:
    """Re-raise `exc` unless it is an availability failure, which skips, prefixed.

    The counterpart to `require_reachable` for the case a test catches its
    own exception: the host answered, but the call failed. Replaces the
    `pytest.skip(f"… unavailable: {exc}")` idiom, which emits an *unprefixed*
    skip that both masked-lane guards read as "this backend was never
    configured" — so a transient outage is reported as a missing credential and,
    beside passing neighbours, reddens the whole lane.

    Args:
        exc: The exception the test caught.

    Raises:
        Skipped: When `is_upstream_unavailable` recognises `exc`.
        BaseException: `exc` itself, unchanged, when it does not.

    Examples:
        ```python
        >>> import requests
        >>> from earthlens.testing import skip_on_upstream
        >>> try:
        ...     skip_on_upstream(ValueError("bad bbox"))
        ... except ValueError as caught:
        ...     print(caught)
        bad bbox

        ```
    """
    reason = is_upstream_unavailable(exc)
    if reason is None:
        raise exc
    skip_live_unavailable(reason)


def skip_on_public_endpoint_block(exc: BaseException) -> NoReturn:
    """Skip a *keyless* public endpoint's block, including a bare `403`.

    `skip_on_upstream` forgives `408` / `425` / `429` / `5xx` but deliberately
    not `403`: against an authenticated service a `403` is a real credential or
    permission regression, and forgiving it everywhere would hide that. Against
    an endpoint that takes *no* credentials it cannot be — there is nothing to
    authenticate — so a `403` there is the service throttling or blacklisting
    the caller (NOAA ERDDAP rate-limiting a CI runner, #1168; ohsome's front
    proxy, #1025). A test that knows its endpoint is keyless calls this, so that
    case skips with the guard prefix instead of reddening the lane.

    Only the `403` is widened. A `404`, a `400`, or a content assertion is not
    a block, so it falls through to `skip_on_upstream` and still fails; a `429`
    or `5xx` is forgiven there exactly as before.

    Args:
        exc: The exception the test caught.

    Raises:
        Skipped: When the first HTTP status in `exc`'s chain is `403`, or when
            `skip_on_upstream` recognises an availability failure.
        BaseException: `exc` unchanged, when it is a real failure.

    Examples:
        - A keyless endpoint answering `403` is a throttle, so it skips:
            ```python
            >>> import requests
            >>> from earthlens.testing import skip_on_public_endpoint_block
            >>> resp = requests.Response()
            >>> resp.status_code = 403
            >>> err = requests.HTTPError("forbidden", response=resp)
            >>> try:
            ...     skip_on_public_endpoint_block(err)
            ... except BaseException as caught:
            ...     print(type(caught).__name__)
            Skipped

            ```
        - A `404` is a real answer, not a block, so it re-raises:
            ```python
            >>> import requests
            >>> from earthlens.testing import skip_on_public_endpoint_block
            >>> resp = requests.Response()
            >>> resp.status_code = 404
            >>> err = requests.HTTPError("missing", response=resp)
            >>> try:
            ...     skip_on_public_endpoint_block(err)
            ... except requests.HTTPError as caught:
            ...     print(caught)
            missing

            ```
    """
    for link in exception_chain(exc):
        status = response_status(link)
        if status is None:
            status = status_in_message(str(link), anchored=True)
        if status is not None:
            if status == 403:
                skip_live_unavailable(f"public endpoint returned HTTP {status}")
            break
    skip_on_upstream(exc)


#: Reachability verdicts keyed on the probed target: `None` when the host
#: answered, otherwise the reason it did not. Cached for the life of the
#: process so N tests against one host cost one probe, which is the whole
#: reason the per-module `skipif` idiom was tolerable in the first place.
_PROBE_CACHE: dict[str, str | None] = {}

#: Default probe timeout. Deliberately longer than the 5s several of the
#: hand-rolled helpers used: a 5s budget turns a merely slow public host into a
#: skipped test, which is the false-negative direction that hides regressions.
_PROBE_TIMEOUT = 10.0


#: pytest-socket raises `SocketBlockedError` for a blocked `socket()` and
#: `SocketConnectBlockedError` for a blocked `connect()` under `--allow-hosts`.
#: The two share no base beyond `RuntimeError`, and importing them would make
#: the plugin a hard dependency of this module, so they are matched by name.
_SOCKET_BLOCK_ERRORS = frozenset({"SocketBlockedError", "SocketConnectBlockedError"})


def _is_socket_blocked(exc: BaseException) -> bool:
    """Return whether `exc` is pytest-socket refusing the connection.

    Args:
        exc: The exception to classify.

    Returns:
        `True` when the lane's socket block raised it.
    """
    return type(exc).__name__ in _SOCKET_BLOCK_ERRORS


def _probe(target: str, timeout: float) -> str | None:
    """Return `None` when `target` answers, else the reason it did not.

    A URL is probed with a `HEAD` and *any* HTTP answer counts as reachable —
    even a 500. Whether the service works is the test's business, and the
    `pytest_runtest_call` hook already classifies that; this only answers
    "is the host there at all". A bare host (optionally `host:port`) is probed
    with a TCP connect.
    """
    try:
        if "://" in target:
            import requests

            requests.head(target, timeout=timeout, allow_redirects=True)
        else:
            host, _, port = target.partition(":")
            socket.create_connection((host, int(port or 443)), timeout=timeout).close()
    except Exception as exc:  # noqa: BLE001 - any probe failure is a verdict
        # pytest-socket blocking the probe means the test is in the wrong lane,
        # not that the host is down. Let that surface as the error it is.
        if _is_socket_blocked(exc):
            raise
        return f"{target} unreachable ({type(exc).__name__})"
    return None


def require_reachable(target: str, *, timeout: float = _PROBE_TIMEOUT) -> None:
    """Skip the current live test, prefixed, when `target` cannot be reached.

    The single replacement for the ~30 hand-rolled `_network_ok` / `_host_ok` /
    `_online` probes that used four transports, three timeouts and — because
    they fed a module-level `pytest.mark.skipif` — ran at *import* time, so the
    `-m "not e2e"` lane opened real sockets during collection and the resulting
    skip carried no prefix.

    Called from inside a test (or, preferably, via the `requires_host` marker)
    it runs only for selected tests, probes each target once per session, and
    stamps the guard prefix so a lane masked by an outage is diagnosed as an
    outage.

    Args:
        target: A URL (`https://host/path`, probed with `HEAD`), a bare host
            (probed with a TCP connect to 443), or `host:port`.
        timeout: Seconds to wait for the probe.

    Raises:
        Skipped: When the target does not answer.

    Examples:
        ```python
        >>> import pytest
        >>> from earthlens.testing import require_reachable, reset_reachability_cache
        >>> reset_reachability_cache({"down.invalid": "down.invalid unreachable"})
        >>> with pytest.raises(BaseException, match="live e2e skipped"):
        ...     require_reachable("down.invalid")
        >>> reset_reachability_cache()

        ```
    """
    if target not in _PROBE_CACHE:
        _PROBE_CACHE[target] = _probe(target, timeout)
    reason = _PROBE_CACHE[target]
    if reason is not None:
        skip_live_unavailable(reason)


def reset_reachability_cache(seed: dict[str, str | None] | None = None) -> None:
    """Clear (or seed) the per-session reachability verdicts.

    Args:
        seed: Verdicts to install instead — `None` for a reachable host, a
            reason string for an unreachable one. Omit to simply clear.
    """
    _PROBE_CACHE.clear()
    if seed:
        _PROBE_CACHE.update(seed)


@pytest.fixture(autouse=True)
def require_marked_hosts(request: pytest.FixtureRequest) -> None:
    """Apply every `requires_host` marker on the test before it runs.

    Lets a live test keep the one-line decorator ergonomics of the `skipif`
    idiom it replaces — `@pytest.mark.requires_host("wgms.ch")` — while the
    probe happens at run time, for selected tests only, and skips with the
    guard prefix.

    Args:
        request: pytest's request object, for the item's markers.
    """
    for marker in request.node.iter_markers("requires_host"):
        timeout = marker.kwargs.get("timeout", _PROBE_TIMEOUT)
        for target in marker.args:
            require_reachable(target, timeout=timeout)


#: What to tell someone whose non-e2e test just tried to open a socket. A bare
#: `SocketBlockedError` names the symptom; this names the decision they have to
#: make. Borrowed from Airflow, whose DB-access block fails with a sentence
#: saying which marker to add rather than with a connection error.
_SOCKET_BLOCKED_HELP = (
    "{nodeid} opened a network socket, and this lane runs with "
    "`--disable-socket` because it must not touch the network.\n"
    "Either fake the transport (`earthlens.testing.fake_http` / `FakeResponse`, "
    "or an emulator on 127.0.0.1), or mark the test `@pytest.mark.e2e` so it "
    "runs in a live lane instead.\n"
    "If it is a *reachability probe*, it belongs in "
    "`@pytest.mark.requires_host(...)`, which runs only for selected tests."
)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Turn an upstream-availability failure in a live `e2e` test into a skip.

    A live end-to-end test asserts against a real external service; when that
    service is down, throttling, or refusing the CI runner the test should report
    `skipped`, not `failed`. This wrapper inspects the exception a `e2e`-marked
    test raises and, when `is_upstream_unavailable` recognises it, re-raises it as
    a skip. Every other failure — and every non-`e2e` test — propagates unchanged.

    It also rewrites pytest-socket's `SocketBlockedError` into a message that
    says what to do about it, since that error arrives in the lane where the
    author is least likely to know the lane blocks sockets at all.

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
        elif _is_socket_blocked(exc):
            raise RuntimeError(_SOCKET_BLOCKED_HELP.format(nodeid=item.nodeid)) from exc
        raise


#: Reason stamped on a quarantined test's skip, so a reader of the log knows the
#: test was held back deliberately rather than failing to select.
_QUARANTINE_REASON = (
    "quarantined — chronically unreliable upstream; run it with `-m quarantined`"
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Tag each item by its directory, then hold back the quarantined ones.

    Two jobs in one hook because pytest allows a conftest only one
    `pytest_collection_modifyitems`, and both belong to every member:

    1. `tag_items_by_directory` gives each test its
       backend marker, so `-m <backend>` selects that backend's whole subtree —
       including the `test_cli.py` that used to carry only `cli`.
    2. A `quarantined` test is skipped unless the `-m` expression names the
       marker. The third option the suite lacked: a live backend whose host is
       down for weeks could previously only be deleted (losing the coverage) or
       left in a shared lane (reddening its neighbours).

    Args:
        config: The session config, read for the `-m` expression.
        items: The collected items, mutated in place.
    """
    tag_items_by_directory(items)
    if re.search(r"\bquarantined\b", config.getoption("-m", default="") or ""):
        return
    skip = pytest.mark.skip(reason=_QUARANTINE_REASON)
    for item in items:
        if item.get_closest_marker("quarantined") is not None:
            item.add_marker(skip)


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
