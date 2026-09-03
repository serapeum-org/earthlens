"""Shared `requests`-based HTTP client for the REST-style backends.

Every REST / event / air-quality / biodiversity backend repeats the
same transport chores: a pooled `requests.Session`, a sensible default
`User-Agent` and `Accept-Encoding`, a per-request timeout, a
`Retry-After`-aware retry/back-off loop for `429`/`5xx`, JSON decoding,
and a streamed `download()` with a `tqdm` progress bar. `HttpClient`
owns exactly those transport concerns so a backend keeps only the
`API`-shaped parts: endpoint paths, query params, pagination envelopes,
auth-header values, and response parsing.

The retry engine is a hand-rolled loop (not `urllib3.util.retry.Retry`
mounted on an `HTTPAdapter`) generalising the loop the REST backends
(openaq first) previously hand-rolled: `Retry-After` first, else
`backoff_factor * 2**attempt`. Both the transport
(`session=`) and the wait (`sleep=`) are injectable, so the whole client
is unit-testable with a fake transport — no live network, no real
delays.

Alongside it, `HttpRangeFile` turns a range-serving URL into a
**seekable** binary file object, so a stdlib container reader
(`zipfile`, and anything else that only needs `readinto`/`seek`) can pull
one member out of a multi-gigabyte remote archive without downloading
it. It reads through an `HttpClient`, so it inherits the same retry and
throttle policy.

`requests` and `tqdm` are already core earthlens dependencies, so this
module adds none. The public imports are
`from earthlens.base.http import HttpClient, HttpRangeFile, Timeout`
(`Timeout` is the `float | tuple[float, float]` alias every timeout
parameter accepts).
"""

from __future__ import annotations

import datetime as dt
import errno
import io
import re
import threading
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

import requests
from loguru import logger
from tqdm import tqdm

#: A `requests`-style timeout: either a single float applied to both the
#: connect and read phases, or a `(connect, read)` pair that bounds them
#: separately. A short connect budget fails a dead or blocked host in seconds
#: — a TCP handshake that will never complete is not worth the read budget —
#: while a long read budget still lets a large transfer run to completion.
Timeout = float | tuple[float, float]

#: Per-request timeout (seconds) applied when a call passes no `timeout`.
#: A `(connect, read)` pair rather than one number: a host that is down or
#: firewalled fails its TCP handshake in 10s instead of occupying a thread for
#: a full minute, while a slow-but-alive transfer keeps the 60s read budget it
#: had before. Bounding both phases with one value made the short failure and
#: the long transfer share a budget that could only suit one of them.
DEFAULT_TIMEOUT: Timeout = (10.0, 60.0)

#: Maximum retries for a retryable status before the last response's
#: error is raised.
DEFAULT_MAX_RETRIES = 5

#: Base seconds for exponential back-off when a response carries no
#: `Retry-After` header (wait = `backoff_factor * 2**attempt`).
DEFAULT_BACKOFF_FACTOR = 1.0

#: Ceiling (seconds) on any single retry wait, so a hostile or
#: misconfigured `Retry-After` cannot pin the calling thread for an
#: unbounded interval. `None` disables the cap.
DEFAULT_MAX_BACKOFF = 300.0

#: HTTP statuses that trigger a retry. `429` (rate-limited) plus the
#: transient `5xx` gateway/unavailable family.
DEFAULT_STATUS_FORCELIST: tuple[int, ...] = (429, 500, 502, 503, 504)

#: Transport-level exceptions retried by default. These are the failures that
#: never reach an HTTP status — a refused or reset connection, a DNS blip, a
#: handshake or read that timed out, a response body truncated mid-stream — so
#: `status_forcelist` cannot describe them. Leaving this empty meant a TCP reset
#: partway through a multi-gigabyte granule aborted the whole download while the
#: retry engine sat unused.
#:
#: `requests.RequestException` is deliberately NOT the entry here: it is the
#: base of `HTTPError` too, so retrying on it would re-issue requests for 4xx
#: responses that will never succeed.
DEFAULT_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

#: Methods whose retry after a *transport* failure is safe without the caller
#: saying so. A request that never completed may still have reached the server,
#: so replaying `POST` could double-submit an order, a job or a paid query;
#: replaying `GET` cannot. This mirrors urllib3's `Retry.DEFAULT_ALLOWED_METHODS`
#: and applies only to the exception path — a retryable *status* is replayed for
#: any method, as before, because the server answered and asked for it.
#:
#: A caller that knows its `POST` is safe to replay (a search endpoint, an
#: idempotent RPC) opts in with `retry_unsafe_methods=True`.
IDEMPOTENT_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"}
)

#: Streaming chunk size (bytes) for :meth:`HttpClient.download` — 1 MiB.
DEFAULT_CHUNK_SIZE = 1 << 20

#: Buffer size (bytes) :meth:`HttpRangeFile.buffered` wraps the raw
#: range reader in — 1 MiB, so a container's many small structural reads
#: coalesce into few HTTP requests.
DEFAULT_RANGE_BUFFER_SIZE = 1 << 20

#: `Content-Range: bytes <first>-<last>/<total>` — the `<total>` group is
#: the object size a `206` reply carries, used when `HEAD` is unavailable.
_CONTENT_RANGE_TOTAL = re.compile(r"bytes\s+\d+-\d+/(\d+)")


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds.

    RFC 9110 §10.2.3 allows either an integer number of seconds (e.g.
    `Retry-After: 5`) or an HTTP-date
    (`Retry-After: Fri, 31 Dec 2027 23:59:59 GMT`); both forms are
    handled. A missing, non-numeric, or **negative** numeric value yields
    `None` so the caller falls back to exponential back-off (the spec
    mandates a non-negative delay; a negative one is invalid). A past
    HTTP-date clamps to `0.0` (never sleep backwards in time).

    Args:
        value: The raw `Retry-After` header value, or `None`.

    Returns:
        The delay in seconds, or `None` when absent / unparseable /
        negative.

    Examples:
        - A numeric value parses to seconds; junk or a negative yields
          `None`, and a past HTTP-date clamps to zero:
            ```python
            >>> from earthlens.base.http import _parse_retry_after
            >>> _parse_retry_after("5")
            5.0
            >>> _parse_retry_after(None) is None
            True
            >>> _parse_retry_after("soon") is None
            True
            >>> _parse_retry_after("-1") is None
            True
            >>> _parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT")
            0.0

            ```
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        pass
    else:
        return seconds if seconds >= 0 else None
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    now = dt.datetime.now(tz=target.tzinfo)
    return max(0.0, (target - now).total_seconds())


def redact_url(url: str) -> str:
    """Return `scheme://host` for logging, hiding any secret in the URL.

    Retry warnings must never echo the full request URL: some backends
    carry a credential in the URL itself — a path segment (FIRMS
    `MAP_KEY`), a query parameter (NREL `api_key`, WDPA `?token=`), or
    userinfo (`user:pass@host`). Everything but the (non-secret) scheme
    and host is dropped, so only `scheme://host` reaches the log.

    Args:
        url: The request URL.

    Returns:
        `"scheme://host"`, or `"<url>"` when the input has no scheme/host.

    Examples:
        - The path, query, and userinfo — where secrets ride — are
          stripped:
            ```python
            >>> from earthlens.base.http import redact_url
            >>> redact_url("https://firms.example/api/SECRETKEY/area?x=1")
            'https://firms.example'
            >>> redact_url("https://user:pass@host/p")
            'https://host'

            ```
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return "<url>"
    return f"{parts.scheme}://{parts.hostname}"


def _check_magic(path: Path, magic: bytes | tuple[bytes, ...], url: str) -> None:
    """Raise unless the file at `path` starts with one of the `magic` prefixes.

    A provider that reports failure in the *body* while still returning a
    `200` (an ERDDAP error page, a portal's HTML login redirect) would
    otherwise be written out under a `.nc`/`.tif` name and only fail much
    later, when something tries to open it. Checking the leading bytes turns
    that into an immediate, readable error at the download site.

    Args:
        path: The just-written file (typically the `.part` temp).
        magic: One byte prefix, or a tuple of acceptable prefixes.
        url: The source URL, redacted before it reaches the message.

    Raises:
        ValueError: When the file starts with none of the prefixes — the
            message carries the size and the first bytes actually seen — or
            when `magic` is an empty sequence, which is a caller error.

    Examples:
        - A NetCDF-3 body passes the classic `CDF` check:
            ```python
            >>> from pathlib import Path
            >>> from tempfile import TemporaryDirectory
            >>> from earthlens.base.http import _check_magic
            >>> with TemporaryDirectory() as tmp:
            ...     nc = Path(tmp) / "grid.nc"
            ...     _ = nc.write_bytes(b"CDF\\x01rest-of-the-file")
            ...     _check_magic(nc, (b"CDF", b"\\x89HDF"), "https://host/grid.nc")

            ```
        - An HTML error page served as a `.nc` is rejected, and the message
          names the host and shows what arrived:
            ```python
            >>> from pathlib import Path
            >>> from tempfile import TemporaryDirectory
            >>> from earthlens.base.http import _check_magic
            >>> with TemporaryDirectory() as tmp:
            ...     nc = Path(tmp) / "grid.nc"
            ...     _ = nc.write_bytes(b"<html>error</html>")
            ...     _check_magic(nc, b"CDF", "https://host/grid.nc?token=secret")
            Traceback (most recent call last):
                ...
            ValueError: https://host returned a body that does not start with b'CDF' (18 bytes, starts b'<html>error</html>'). The server likely returned an error page or a redirect instead of the file.

            ```
    """
    options = (magic,) if isinstance(magic, bytes) else tuple(magic)
    if not options:
        # No prefixes to check against is a caller bug, not a bad download —
        # say which rather than dying inside `max()` on an empty sequence.
        raise ValueError(
            "expect_magic was an empty sequence; pass at least one byte "
            "prefix, or None to skip the check entirely."
        )
    with open(path, "rb") as handle:
        head = handle.read(max(max(len(m) for m in options), 24))
    if any(head.startswith(m) for m in options):
        return
    size = path.stat().st_size
    raise ValueError(
        f"{redact_url(url)} returned a body that does not start with "
        f"{magic!r} ({size} bytes, starts {head[:24]!r}). The server likely "
        f"returned an error page or a redirect instead of the file."
    )


def _progress_total(headers: Any) -> int | None:
    """Return the download progress-bar total from response headers.

    Uses `Content-Length` only for an untransformed body: it reports the
    *compressed* size, but `iter_content` yields *decompressed* bytes, so
    a `Content-Encoding` (e.g. gzip) response would overshoot the bar. In
    that case (or when the length is absent / non-numeric) the total is
    `None` and the bar runs unbounded.

    Args:
        headers: The response headers mapping.

    Returns:
        The byte total, or `None` when it cannot be trusted.
    """
    encoding = (headers.get("Content-Encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        return None
    raw_length = headers.get("Content-Length")
    if raw_length is not None and raw_length.isdigit():
        return int(raw_length)
    return None


def _default_user_agent() -> str:
    """Return the default `User-Agent`, `earthlens/{version}`.

    The version is read lazily from the installed package metadata so
    importing this module never triggers a circular import against
    the `earthlens.core` package surface. The string is deliberately **non-Mozilla**:
    the DIGITAL.CSIC Anubis anti-bot wall (SPEIbase) blocks browser-like
    `User-Agent`s, and several upstreams throttle the bare python-requests
    default.

    Returns:
        The default agent string, e.g. `"earthlens/0.10.0"` (or
        `"earthlens/unknown"` when the package metadata is unavailable).
    """
    from earthlens.core import __version__

    return f"earthlens/{__version__}"


def prefer_ipv4() -> None:
    """Make every urllib3-based request in this process skip AAAA records.

    urllib3 asks `getaddrinfo` for `AF_UNSPEC` — both IPv4 (A) and IPv6 (AAAA)
    addresses — only while `urllib3.util.connection.HAS_IPV6` is true. Setting
    it false narrows resolution to `AF_INET`, so a connection only ever tries
    an IPv4 address.

    This exists for a host reached over a network with no IPv6 egress, where a
    resolved AAAA connects into a dead route and raises
    `OSError: [Errno 101] Network is unreachable` (`ENETUNREACH`) with no IPv4
    fallback — the failure mode of GitHub-hosted runners against the dual-stack
    Earthdata Login host `urs.earthdata.nasa.gov`. Forcing IPv4 drops the AAAA
    from consideration, so the dead route is never dialled.

    The switch is a `urllib3` module global, so it is **process-wide**: it
    affects every `requests` / `urllib3` connection made afterwards, not only
    the caller's. It is idempotent — calling it again is a no-op — and only
    ever removes IPv6, never restores it. Reach for it in one shared place (an
    auth path against a known dual-stack host), not per request.
    """
    import urllib3.util.connection as connection

    connection.HAS_IPV6 = False


_LoginResult = TypeVar("_LoginResult")


def is_network_unreachable(exc: BaseException | None) -> bool:
    """Return whether `exc`'s cause/context chain holds an `ENETUNREACH` error.

    A dead IPv6 route surfaces as `OSError: [Errno 101] Network is unreachable`
    wrapped several layers deep — `requests.ConnectionError` around urllib3's
    `NewConnectionError` around the `OSError`. This walks the `__cause__` /
    `__context__` chain (guarding against cycles) and reports whether any link
    is an `ENETUNREACH` `OSError`.

    The `isinstance` / `errno` check is the precise signal and is
    platform-correct (`errno.ENETUNREACH` resolves to the local value). It is
    backed by a text fallback for the common case where urllib3 embeds the
    errno in a `NewConnectionError` message rather than chaining the `OSError`;
    that fallback matches the platform errno tag (`[Errno 101]` on the Linux CI
    runners #926 targets) so an unrelated message that merely mentions an
    unreachable network cannot trip the process-wide, irreversible IPv4 flip
    downstream. The tag form is Unix-shaped and does not match a Windows
    `WinError`, but a bare Windows `OSError` is still caught by the errno check.

    Args:
        exc: The exception to inspect, or `None`.

    Returns:
        Whether an `ENETUNREACH` appears anywhere in the exception chain.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and exc.errno == errno.ENETUNREACH:
            return True
        if f"[Errno {errno.ENETUNREACH}]" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def retry_login_forcing_ipv4(login: Callable[[], _LoginResult]) -> _LoginResult:
    """Run `login`, and on a dead IPv6 route force IPv4 and retry it once.

    `login` is called as-is first. If it fails with an `ENETUNREACH` — the
    dual-stack host resolved a AAAA that connects into a dead route, the #926
    failure on hosts with no IPv6 egress — `prefer_ipv4()` narrows the process
    to IPv4 and `login` is retried exactly once. Any other failure, and the
    retry's own failure, propagate unchanged.

    Forcing IPv4 only on an observed `ENETUNREACH` leaves IPv6 untouched on
    healthy dual-stack and IPv6-only networks, where the first call succeeds and
    the switch never fires.

    Args:
        login: A zero-argument callable that performs the login / dial and
            returns its result (e.g. an `earthaccess` auth handle or an
            authenticated session).

    Returns:
        Whatever `login` returns, from the first successful call or the retry.
    """
    try:
        return login()
    except Exception as exc:  # noqa: BLE001
        # Catch broadly so any ENETUNREACH shape is retried; all else re-raised.
        if not is_network_unreachable(exc):
            raise
        logger.warning(
            "Login hit a dead IPv6 route (ENETUNREACH); forcing IPv4 and "
            "retrying the dial once."
        )
        prefer_ipv4()
        return login()


def new_session() -> requests.Session:
    """Return the pooled transport :class:`HttpClient` uses by default.

    A single indirection so the default transport is decided in one place
    rather than at 21 construction sites. A `requests.Session` keeps the
    TCP+TLS connection open across calls, which is the difference between one
    handshake per batch and one per request — and every backend that fetches
    many small files from one host pays that difference.

    Returns:
        requests.Session: A fresh pooled session.
    """
    import requests

    return requests.Session()


#: Per-thread session cache behind :func:`thread_local_session`. A plain dict
#: on a `threading.local` so each thread gets its own sessions and never shares
#: one, keyed by caller so two providers do not end up on the same cookie jar.
_THREAD_SESSIONS = threading.local()


def thread_local_session(key: str) -> requests.Session:
    """Return this thread's pooled session for `key`, creating it on first use.

    For the download helpers that are *called* per item — one tile, one file,
    one REST page — rather than holding a client. Constructing a session inside
    such a helper pools nothing: each call gets a new connection, which is the
    handshake-per-request cost all over again. Caching one per thread fixes
    that without sharing a session between threads, which `requests` does not
    guarantee is safe. GHSL, for instance, pulls its tiles through
    `joblib.Parallel(prefer="threads")`, so each worker reuses its own
    connection across the tiles it handles.

    Args:
        key: Names the caller (e.g. `"ghsl"`). Sessions are cached per key so
            unrelated providers keep separate cookie jars and headers.

    Returns:
        requests.Session: The session for this thread and key.
    """
    cache = getattr(_THREAD_SESSIONS, "cache", None)
    if cache is None:
        cache = {}
        _THREAD_SESSIONS.cache = cache
    session = cache.get(key)
    if session is None:
        session = new_session()
        cache[key] = session
    return session


def reset_thread_local_sessions() -> None:
    """Drop this thread's cached sessions.

    The cache outlives any one request, so a caller that has swapped the
    transport underneath it needs a way to make the next
    :func:`thread_local_session` call rebuild, rather than hand back a session
    built against the previous one.
    """
    _THREAD_SESSIONS.cache = {}


class RequestsGet:
    """Session-like adapter routing every call through the `requests` module.

    Passed as `HttpClient(session=RequestsGet())` by the backends that want a
    fresh connection per call (no pooled `requests.Session`) rather than
    connection reuse. It re-imports `requests` on every call so it dispatches
    through whatever `requests` is current — a test that monkeypatches
    `requests.get` (under any import alias; they all reference the one module)
    or injects a fake `requests` module via `sys.modules` still drives the
    transport. One shared class instead of the shim re-declared verbatim in a
    dozen backends.

    Each call fills in a default `timeout` (`DEFAULT_TIMEOUT`) when the caller
    omits one, so a bare `RequestsGet().get(url)` cannot hang indefinitely; an
    explicit `timeout=` (including the `None` that `HttpClient` passes for "no
    timeout") is left untouched.
    """

    def get(self, url: str, **kwargs: Any) -> Any:
        """Issue a `GET` via the current `requests.get`."""
        import requests

        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return requests.get(url, **kwargs)  # nosec B113 - default timeout applied above

    def post(self, url: str, **kwargs: Any) -> Any:
        """Issue a `POST` via the current `requests.post`."""
        import requests

        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return requests.post(url, **kwargs)  # nosec B113 - default timeout applied above


class HttpClient:
    """Reusable HTTP transport: session, headers, timeout, retry, download.

    Wraps a :class:`requests.Session`, attaching default headers (a
    non-Mozilla `User-Agent` and `Accept-Encoding: gzip, deflate`) to
    every request and applying a shared timeout. The verbs
    (:meth:`get`, :meth:`post`, :meth:`request`) route through a
    `Retry-After`-aware back-off loop; :meth:`get_json` decodes the JSON
    body and :meth:`download` streams a response to disk. Both the
    session and the sleep function are injectable so the client is fully
    unit-testable without a live network.

    Default headers set at construction are merged with (and overridden
    by) any per-request `headers=`, so a backend expresses its quirks —
    openaq's `X-API-Key`, a descriptive osm contact `User-Agent` — without
    subclassing.

    Attributes:
        timeout: Per-request timeout in seconds — a single float, or a
            `(connect, read)` pair bounding the two phases separately.
        max_retries: Maximum retries on a retryable status before raising.
        backoff_factor: Base seconds for exponential back-off when no
            `Retry-After` header is present.
        status_forcelist: HTTP statuses that trigger a retry.
        max_backoff: Ceiling in seconds on any single retry wait.
        retry_on_exceptions: Transport exception types that trigger a retry.
        raise_for_status: Whether the final response is `raise_for_status`-ed.
        min_interval: Minimum seconds between consecutive requests.

    Examples:
        - The default agent is non-Mozilla and version-stamped:
            ```python
            >>> from earthlens.base.http import HttpClient
            >>> HttpClient().default_headers["User-Agent"].startswith("earthlens/")
            True

            ```
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        status_forcelist: tuple[int, ...] = DEFAULT_STATUS_FORCELIST,
        max_backoff: float | None = DEFAULT_MAX_BACKOFF,
        retry_on_exceptions: tuple[type[BaseException], ...] = (
            DEFAULT_RETRY_EXCEPTIONS
        ),
        retry_unsafe_methods: bool = False,
        retry_predicate: Callable[[requests.Response], bool] | None = None,
        raise_for_status: bool = True,
        min_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build a client with default headers, timeout, and retry policy.

        Args:
            session: An existing :class:`requests.Session` to reuse.
                Defaults to a fresh session. Injectable so tests can
                supply a fake transport.
            user_agent: The default `User-Agent` header value. Defaults
                to `earthlens/{version}` (non-Mozilla by design; see
                :func:`_default_user_agent`).
            headers: Extra default headers merged onto every request
                (e.g. `{"X-API-Key": ...}`). Override per call with a
                request-level `headers=`.
            timeout: Per-request timeout in seconds — a single float bounds
                both the connect and read phases, or pass a `(connect, read)`
                pair to bound them separately (a short connect budget fails a
                dead host fast without shortening the read budget).
            max_retries: Maximum retries on a retryable status before the
                last response's error is raised.
            backoff_factor: Base seconds for exponential back-off when a
                response carries no `Retry-After` header.
            status_forcelist: HTTP statuses that trigger a retry.
            max_backoff: Ceiling in seconds on any single retry wait, so a
                large `Retry-After` cannot pin the thread indefinitely.
                `None` disables the cap.
            retry_on_exceptions: Exception types that also trigger a retry
                when raised by the transport (e.g.
                `(requests.ConnectionError, requests.Timeout)`). Empty
                (the default) retries on status only, never on a raised
                exception.
            retry_predicate: An optional callback `(response) -> bool`
                that, when it returns `True`, marks a response retryable
                even if its status is not in `status_forcelist` (e.g. a
                `200` whose body signals a rate-limit).
            raise_for_status: Whether to call `raise_for_status` on the
                final response. `False` returns the response unraised so
                the caller can inspect the status itself (e.g. to redact a
                secret-bearing URL from the error, or to branch on a
                `4xx`). Overridable per request.
            min_interval: Minimum seconds between consecutive requests
                (a proactive client-side rate limit). `0.0` (default)
                disables throttling.
            clock: Monotonic clock used for the `min_interval` throttle.
                Injectable so tests drive it deterministically.
            sleep: The sleep function used between retries and for the
                throttle. Defaults to :func:`time.sleep`; injectable so
                tests run without real delays.
        """
        self._session = session if session is not None else new_session()
        self._user_agent = user_agent or _default_user_agent()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.status_forcelist = tuple(status_forcelist)
        self.max_backoff = max_backoff
        self.retry_on_exceptions = tuple(retry_on_exceptions)
        self.retry_unsafe_methods = retry_unsafe_methods
        self._retry_predicate = retry_predicate
        self.raise_for_status = raise_for_status
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None
        self._throttle_lock = threading.Lock()
        self._default_headers: dict[str, str] = {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        if headers:
            self._default_headers.update(headers)

    @property
    def default_headers(self) -> dict[str, str]:
        """Return a copy of the headers merged onto every request."""
        return dict(self._default_headers)

    @property
    def session(self) -> requests.Session:
        """Return the underlying :class:`requests.Session`."""
        return self._session

    def _merge_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        """Merge per-request `headers` over the client's defaults."""
        merged = dict(self._default_headers)
        if headers:
            merged.update(headers)
        return merged

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: Timeout | None = None,
        raise_for_status: bool | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Send one request with the default headers, timeout, and retry.

        Args:
            method: HTTP verb (`"GET"`, `"POST"`, ...).
            url: Absolute request URL.
            headers: Per-request headers merged over the client defaults.
            timeout: Per-request timeout override (seconds), as a single
                float or a `(connect, read)` pair. Defaults to the client's
                `timeout`.
            raise_for_status: Per-request override of the client's
                `raise_for_status` policy. `None` (default) uses the
                client setting.
            **kwargs: Extra keyword arguments forwarded to `requests`
                (`params`, `data`, `json`, `stream`, ...).

        Returns:
            requests.Response: The response (after `raise_for_status`
                unless it is disabled).

        Raises:
            requests.HTTPError: On a non-retryable error status, or after
                the retryable status is exhausted (when `raise_for_status`
                is on).
        """
        merged = self._merge_headers(headers)
        effective_timeout = self.timeout if timeout is None else timeout
        return self._request_with_retry(
            method,
            url,
            headers=merged,
            timeout=effective_timeout,
            raise_for_status=raise_for_status,
            **kwargs,
        )

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a `GET` request. See :meth:`request` for arguments."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a `POST` request. See :meth:`request` for arguments."""
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """Send a `GET` request and decode the JSON response body.

        Convenience over :meth:`get` for the REST endpoints that return
        JSON envelopes.

        Args:
            url: Absolute request URL.
            **kwargs: Keyword arguments forwarded to :meth:`get`
                (`params`, `headers`, `timeout`, ...).

        Returns:
            The parsed JSON body (typically a `dict` or `list`).

        Raises:
            requests.HTTPError: On a non-retryable error status, or after
                the retryable status is exhausted.
        """
        return self.get(url, **kwargs).json()

    def stream(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a streaming `GET` (`stream=True`), retry-wrapped.

        Returns the open response without consuming its body, so the
        caller can iterate `iter_content`. Retries follow the same
        `Retry-After`/back-off policy as the other verbs; the retry
        decision reads only the status line, never the body.

        Args:
            url: Absolute request URL.
            **kwargs: Keyword arguments forwarded to :meth:`get`.

        Returns:
            requests.Response: The open streaming response.
        """
        return self.get(url, stream=True, **kwargs)

    def _stream_to_file(
        self,
        response: requests.Response,
        dest: Path,
        *,
        chunk: int,
        progress: bool,
        desc: str,
    ) -> None:
        """Write a streaming response's body to `dest` with a `tqdm` bar.

        Args:
            response: The open streaming response.
            dest: The file to write (typically a temp `.part` path).
            chunk: Streaming block size in bytes.
            progress: Whether to show the progress bar.
            desc: The bar label (the final file name).
        """
        total = _progress_total(response.headers)
        bar = tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            disable=not progress,
            desc=desc,
        )
        try:
            with open(dest, "wb") as handle:
                for block in response.iter_content(chunk_size=chunk):
                    if block:
                        handle.write(block)
                        bar.update(len(block))
        finally:
            bar.close()

    def download(
        self,
        url: str,
        dest: str | Path,
        *,
        chunk: int = DEFAULT_CHUNK_SIZE,
        progress: bool = True,
        atomic: bool = True,
        expect_magic: bytes | tuple[bytes, ...] | None = None,
        headers: dict[str, str] | None = None,
        timeout: Timeout | None = None,
        **kwargs: Any,
    ) -> Path:
        """Stream `url` to `dest` atomically, optionally showing a `tqdm` bar.

        Absorbs the chunk-loop the streamed-download backends each
        re-implement: streams with `stream=True`, sizes a progress bar
        from `Content-Length` when present, and writes 1 MiB blocks. When
        `atomic` (the default) it writes to a sibling `<dest>.part` and
        renames on success, and it removes the temp on any failure — so a
        crashed or interrupted download never leaves a truncated `dest`.
        The whole download is retry-wrapped: a status in `status_forcelist`
        or an exception in `retry_on_exceptions` retries the attempt (after
        cleaning the temp), honouring the `Retry-After`/back-off policy.

        Args:
            url: Absolute request URL.
            dest: Output file path. Parent directories are created.
            chunk: Streaming block size in bytes (default 1 MiB).
            progress: Show a `tqdm` progress bar. `False` (or a
                non-interactive / test context) suppresses it.
            atomic: Write to `<dest>.part` then rename on success, cleaning up
                the temp on failure — so a crashed download never leaves a
                truncated `dest` and never removes an existing one. Keep this on
                (the default) whenever an existing `dest` must survive a failed
                attempt. `False` streams straight into `dest`, which is opened
                `"wb"` and therefore **truncated up front**: a mid-stream failure
                leaves `dest` short, and any previous contents are gone. The
                failure path does not additionally delete it, but that is damage
                limitation, not a guarantee — `atomic=False` is only appropriate
                when `dest` is known to be disposable.
            expect_magic: One or more byte prefixes the body must start with
                (e.g. `b"CDF"` / `b"\\x89HDF"` for NetCDF). A body that starts
                with none of them raises `ValueError` and the partial write is
                discarded, so an HTML error page served with a 200 status never
                lands as a `.nc`. `None` (the default) skips the check.
            headers: Per-request headers merged over the client defaults.
            timeout: Per-request timeout override (seconds), as a single
                float or a `(connect, read)` pair — the latter fails a dead
                host on the short connect budget without shortening the long
                read budget a large download needs.
            **kwargs: Extra keyword arguments forwarded to `requests`.

        Returns:
            Path: The `dest` path the bytes were written to.

        Raises:
            ValueError: When `expect_magic` is given and the body does not
                start with any of the supplied prefixes.
            requests.HTTPError: On an error status — `download` always
                calls `raise_for_status` (the client's `raise_for_status`
                flag governs the verb methods, not `download`; a file
                fetch never keeps an error body). Note the resulting
                `HTTPError` is itself subject to `retry_on_exceptions`:
                a client whose `retry_on_exceptions` includes a supertype
                of `requests.HTTPError` (e.g. `requests.RequestException`,
                as ghsl/glaciers pass) will **retry** an error status
                before raising it, mirroring their old download loops.
                Also the last transport exception after the retry budget
                is exhausted.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # `expect_magic` promises a rejected body is discarded, which is only
        # possible if `dest` has not been written yet — so a magic check forces
        # staging even when the caller passed `atomic=False`. Otherwise the
        # validation would run on a file that had already replaced a good one.
        staged = atomic or expect_magic is not None
        tmp = dest.with_name(dest.name + ".part") if staged else dest

        def discard_partial() -> None:
            """Remove the partial write, but never a caller-owned `dest`.

            When the write was staged (`atomic`, or a magic check forcing it)
            the partial lives at a private `<dest>.part`, so removing it is
            always safe. Otherwise the stream wrote straight to `dest`, which
            `_stream_to_file` has already truncated by opening it `"wb"` —
            deleting it as well would only turn a truncated file into a missing
            one, and would destroy a file this call never owned.
            """
            if staged:
                tmp.unlink(missing_ok=True)

        merged = self._merge_headers(headers)
        effective_timeout = self.timeout if timeout is None else timeout
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._send(
                    "GET",
                    url,
                    stream=True,
                    headers=merged,
                    timeout=effective_timeout,
                    **kwargs,
                )
                try:
                    retryable = response.status_code in self.status_forcelist or (
                        self._retry_predicate is not None
                        and self._retry_predicate(response)
                    )
                    if retryable and attempt < self.max_retries:
                        retry_after = _parse_retry_after(
                            response.headers.get("Retry-After")
                        )
                        wait = self._backoff_wait(retry_after, attempt)
                        logger.warning(
                            f"HTTP {response.status_code} on {redact_url(url)}; retry "
                            f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                        )
                        self._sleep(wait)
                        attempt += 1
                        continue
                    response.raise_for_status()
                    self._stream_to_file(
                        response, tmp, chunk=chunk, progress=progress, desc=dest.name
                    )
                    if expect_magic is not None:
                        _check_magic(tmp, expect_magic, url)
                finally:
                    response.close()
            except self.retry_on_exceptions as exc:
                discard_partial()
                if attempt >= self.max_retries:
                    raise
                wait = self._backoff_wait(None, attempt)
                logger.warning(
                    f"{type(exc).__name__} on {redact_url(url)}; retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                self._sleep(wait)
                attempt += 1
                continue
            except BaseException:
                discard_partial()
                raise
            if staged:
                # Guard the rename too, so the "removes the temp on any
                # failure" promise holds if the final replace fails.
                try:
                    tmp.replace(dest)
                except BaseException:
                    discard_partial()
                    raise
            return dest

    def _throttle(self) -> None:
        """Sleep so consecutive requests are >= `min_interval` apart.

        A no-op when `min_interval` is `0`. Uses the injected monotonic
        `clock`, records the send time, and sleeps via the injected
        `sleep` so tests drive the rate limit deterministically.

        Held under a lock for the whole read-sleep-write sequence, so
        `min_interval` bounds the *aggregate* request rate rather than the
        rate per thread. Read-then-write without it is a race: every thread
        sees the same `_last_request`, each concludes the interval has
        elapsed, and they all fire together — the burst the limit exists to
        prevent.

        No backend shares one client across threads *today*: the three that
        thread (worldpop, ghsl, hdx, all `prefer="threads"`) build a fresh
        client inside each worker, and `_run_items` is a sequential loop. The
        lock is cheap and makes a shared client safe whenever one appears,
        rather than leaving a latent race for that change to trip over.
        """
        if self.min_interval <= 0:
            return
        with self._throttle_lock:
            if self._last_request is not None:
                remaining = self.min_interval - (self._clock() - self._last_request)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request = self._clock()

    def _backoff_wait(self, retry_after: float | None, attempt: int) -> float:
        """Compute one retry wait: `Retry-After` else exponential back-off.

        Applies the `max_backoff` ceiling and a non-negative floor.

        Args:
            retry_after: Parsed `Retry-After` seconds, or `None`.
            attempt: The zero-based attempt index.

        Returns:
            The clamped wait in seconds.
        """
        wait = (
            retry_after
            if retry_after is not None
            else self.backoff_factor * (2**attempt)
        )
        if self.max_backoff is not None:
            wait = min(wait, self.max_backoff)
        return max(0.0, wait)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        raise_for_status: bool | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Send one request, retrying statuses/exceptions with back-off.

        Retries while the attempt budget holds and either the response
        status is in `status_forcelist`, the `retry_predicate` marks the
        response retryable, or the transport raised one of
        `retry_on_exceptions`. Waits `Retry-After` seconds when present
        and numeric, otherwise `backoff_factor * 2**attempt` (capped by
        `max_backoff`). Honours the `min_interval` throttle before every
        send. Once non-retryable or exhausted, the response is
        `raise_for_status`-ed unless that is disabled, then returned.

        Args:
            method: HTTP verb.
            url: Absolute request URL.
            raise_for_status: Per-request override; `None` uses the
                client policy.
            **kwargs: Keyword arguments forwarded to the session.

        Returns:
            requests.Response: The final response.

        Raises:
            requests.HTTPError: On a non-retryable error status when
                `raise_for_status` is on.
            BaseException: The last transport exception, re-raised after
                the `retry_on_exceptions` budget is exhausted.
        """
        effective_raise = (
            self.raise_for_status if raise_for_status is None else raise_for_status
        )
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._send(method, url, **kwargs)
            except self.retry_on_exceptions as exc:
                if attempt >= self.max_retries:
                    raise
                # A transport failure gives no proof the server did not receive
                # and act on the request, so a non-idempotent method is replayed
                # only when the caller says it is safe.
                if not (
                    self.retry_unsafe_methods or method.upper() in IDEMPOTENT_METHODS
                ):
                    raise
                wait = self._backoff_wait(None, attempt)
                logger.warning(
                    f"{type(exc).__name__} on {redact_url(url)}; retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                self._sleep(wait)
                attempt += 1
                continue
            retryable = response.status_code in self.status_forcelist or (
                self._retry_predicate is not None and self._retry_predicate(response)
            )
            if retryable and attempt < self.max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = self._backoff_wait(retry_after, attempt)
                logger.warning(
                    f"HTTP {response.status_code} on {redact_url(url)}; retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                # Release the (possibly streamed) connection before retrying;
                # a stream=True body is otherwise never consumed and its socket
                # leaks out of the pool.
                response.close()
                self._sleep(wait)
                attempt += 1
                continue
            if effective_raise:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    # Close the final errored response too — for a streamed
                    # request the caller never receives it, so nothing else would.
                    response.close()
                    raise
            return response

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Dispatch to the session's verb method, falling back to `request`.

        Routing `GET` to `session.get` (rather than a generic
        `session.request`) keeps drop-in fake transports that implement
        only `get()` working — the shape the migrated backends' tests use.

        Args:
            method: HTTP verb.
            url: Absolute request URL.
            **kwargs: Keyword arguments forwarded to the session call.

        Returns:
            requests.Response: The raw response (no status check).
        """
        verb = getattr(self._session, method.lower(), None)
        if callable(verb):
            return cast("requests.Response", verb(url, **kwargs))
        return self._session.request(method, url, **kwargs)


class RangeReadError(Exception):
    """A ranged read failed at the transport level.

    Deliberately **not** an `OSError`. Container readers probe a file with
    `except OSError` around their structural reads, so a `requests` error -
    which does derive from `OSError` - would be swallowed and reported as a
    malformed container rather than as the HTTP failure it is.
    """


class HttpRangeFile(io.RawIOBase):
    """A seekable, read-only binary file over an HTTP URL, backed by `Range`.

    Implements just enough of the binary file protocol — `readinto`,
    `seek`, `tell`, `seekable`, `readable` — that the stdlib container
    readers accept it as a file object. That turns any range-serving URL
    into random-access storage, so a member can be pulled out of a
    multi-gigabyte remote archive without downloading the archive.

    The motivating case is a ZIP, which stores its central directory at
    the tail: `zipfile.ZipFile(HttpRangeFile(url).buffered())` reads the
    directory from the last few kilobytes and then inflates only the
    members asked for. Measured against an 8.84 GB Zenodo archive, the
    full member index costs 4 requests / 0.81 MB and one member a further
    2 requests / 2.1 MB — against 8.84 GB for the download-it-all
    alternative.

    Every read goes through the supplied :class:`HttpClient`, so the
    retry / back-off / throttle / `User-Agent` policy is the one the rest
    of earthlens uses, and tests inject a fake session instead of a
    network.

    Two correctness guards matter and are enforced:

    * A server that **ignores** `Range` answers `200` with the whole
      body. Silently accepting that would mis-seek every later read (and
      quietly transfer the entire object), so a non-`206` reply raises.
    * A **compressed** transfer would return a byte count unrelated to
      the requested range, so `Accept-Encoding: identity` is sent on
      every read, overriding the client's default `gzip, deflate`.

    Attributes:
        url: The resolved (post-redirect) URL bytes are fetched from.
        size: Total object size in bytes.
        request_count: Range requests issued so far — for cost logging.
        bytes_read: Total bytes transferred so far — for cost logging.

    Examples:
        - Random access over a fake transport, no network:
            ```python
            >>> import io
            >>> import requests
            >>> from earthlens.base.http import HttpClient, HttpRangeFile
            >>> body = bytes(range(256))
            >>> class FakeSession:
            ...     headers = {}
            ...     def head(self, url, **kwargs):
            ...         r = requests.Response()
            ...         r.status_code = 200
            ...         r.url = url
            ...         r.headers["Content-Length"] = str(len(body))
            ...         return r
            ...     def get(self, url, **kwargs):
            ...         first, last = kwargs["headers"]["Range"][6:].split("-")
            ...         r = requests.Response()
            ...         r.status_code = 206
            ...         r._content = body[int(first) : int(last) + 1]
            ...         return r
            >>> handle = HttpRangeFile(
            ...     "https://example.org/blob", client=HttpClient(session=FakeSession())
            ... )
            >>> handle.size
            256
            >>> handle.seek(-4, io.SEEK_END)
            252
            >>> handle.read(4)
            b'\\xfc\\xfd\\xfe\\xff'
            >>> handle.request_count  # the HEAD that sized it, plus the read
            2

            ```
    """

    def __init__(
        self,
        url: str,
        *,
        client: HttpClient | None = None,
        size: int | None = None,
        timeout: Timeout | None = None,
    ) -> None:
        """Open the remote object and resolve its length.

        Args:
            url: The object URL. Redirects are followed once here and the
                final URL reused for every read, so a redirecting host is
                not re-negotiated on each range request.
            client: Transport to read through. Defaults to a fresh
                :class:`HttpClient`. Inject one to share a session, carry
                auth headers, or supply a fake transport in tests.
            size: Total object size when the caller already knows it (e.g.
                from a catalog row). Skips the size probe entirely — one
                fewer round trip.
            timeout: Per-request timeout override in seconds — a single float
                or a `(connect, read)` pair — applied to the size probe and
                every read. The pair fails a dead host on the short connect
                budget without shortening the read budget a range read needs.

        Raises:
            ValueError: When the object's size cannot be determined —
                neither a `HEAD` `Content-Length` nor a `Content-Range`
                on a one-byte probe yielded a length, so no offset
                arithmetic is possible.
        """
        self._owns_client = client is None
        self._client = client if client is not None else HttpClient()
        self._timeout = timeout
        self._pos = 0
        self.request_count = 0
        self.bytes_read = 0
        self.url = url
        self.size = size if size is not None else self._probe_size(url)

    def _probe_size(self, url: str) -> int:
        """Resolve the object length, preferring `HEAD` over a byte probe.

        `HEAD` is one cheap round trip and is what a well-behaved static
        host answers. Some hosts reject it (`405`) or omit
        `Content-Length`, so the fallback is a `Range: bytes=0-0` `GET`
        whose `206` carries `Content-Range: bytes 0-0/<total>`. The
        fallback doubles as a range-support check: a host that cannot
        satisfy a one-byte range cannot back this file object at all.

        Args:
            url: The object URL, before redirects are followed.

        Returns:
            int: The total object size in bytes.

        Raises:
            ValueError: When neither route yields a length.
        """
        try:
            response = self._client.request(
                "HEAD",
                url,
                allow_redirects=True,
                # Same reason the reads send it: a host that negotiates gzip
                # would report the *compressed* length, and every range offset
                # would then be computed against the wrong total.
                headers={"Accept-Encoding": "identity"},
                timeout=self._timeout,
                raise_for_status=False,
            )
        except requests.RequestException:
            # A HEAD that fails still cost a round trip; count it so the stats
            # do not depend on whether the server answered.
            self.request_count += 1
            response = None
        else:
            self.request_count += 1
        if response is not None and response.ok:
            # Trust the redirect chain HEAD walked, so the reads skip it.
            self.url = response.url or url
            length = response.headers.get("Content-Length")
            if length is not None and length.isdigit():
                return int(length)

        probe = self._client.get(
            url,
            headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
            timeout=self._timeout,
        )
        self.request_count += 1
        self.bytes_read += len(probe.content)
        self.url = probe.url or url
        match = _CONTENT_RANGE_TOTAL.search(probe.headers.get("Content-Range", ""))
        if match is None:
            raise ValueError(
                f"cannot determine the size of {redact_url(url)}: the server "
                f"returned neither a HEAD Content-Length nor a Content-Range "
                f"on a byte probe, so it cannot be read by range."
            )
        return int(match.group(1))

    def readable(self) -> bool:
        """Return `True` — the object is read-only but always readable."""
        return True

    def seekable(self) -> bool:
        """Return `True` — random access is the point of this file object."""
        return True

    def writable(self) -> bool:
        """Return `False` — a remote object is never written through this."""
        return False

    def tell(self) -> int:
        """Return the current byte offset (no request is issued)."""
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the read cursor without touching the network.

        Args:
            offset: Byte offset relative to `whence`.
            whence: `io.SEEK_SET` (0, from the start), `io.SEEK_CUR` (1,
                from the current position), or `io.SEEK_END` (2, from the
                end — a negative `offset` is the usual way to reach a
                container's trailing index).

        Returns:
            int: The new absolute offset. Seeking past the end is allowed (as
                for a local file); the next read simply returns nothing.

        Raises:
            ValueError: On an unknown `whence`.
            OSError: When the result would be a negative offset. That is caller
                arithmetic gone wrong, and silently flooring it to `0` would
                hand back the wrong bytes. `OSError` specifically, because
                container readers guard their structural probes with
                `except OSError` and a `ValueError` would escape that guard.
        """
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self.size + offset
        else:
            raise ValueError(f"invalid whence {whence!r}; expected 0, 1 or 2.")
        if target < 0:
            # `OSError`, not `ValueError`: `zipfile` probes a small archive by
            # seeking back further than its length and guards that with
            # `except OSError`. A `ValueError` escapes the guard and surfaces as
            # a confusing seek error instead of "this is not a ZIP".
            raise OSError(
                errno.EINVAL,
                f"cannot seek to a negative offset ({target}); "
                f"whence={whence} offset={offset} against size {self.size}.",
            )
        self._pos = target
        return self._pos

    def readinto(self, buffer: Any) -> int:
        """Fill `buffer` from the current offset with a single ranged `GET`.

        The read is clamped to the object's end, so a caller asking for
        more than remains gets a short read rather than an error — the
        contract `io.BufferedReader` expects.

        Args:
            buffer: A writable buffer (`bytearray` / `memoryview`) to
                fill. Its length is the number of bytes requested.

        Returns:
            int: Bytes written into `buffer`; `0` at (or past) the end of
                the object.

        Raises:
            ValueError: When the server answers something other than
                `206 Partial Content` — it ignored the `Range` header, so
                the returned bytes are not the requested window and every
                subsequent offset would be wrong.
            RangeReadError: When the transport itself fails. Deliberately
                not an `OSError`, so a container reader's `except OSError`
                probe cannot swallow a live HTTP failure and report it as a
                malformed archive.
        """
        want = len(buffer)
        if want <= 0 or self._pos >= self.size:
            return 0
        last = min(self._pos + want, self.size) - 1
        try:
            response = self._client.get(
                self.url,
                headers={
                    "Range": f"bytes={self._pos}-{last}",
                    # The client's default `gzip, deflate` would make the body
                    # length unrelated to the requested window.
                    "Accept-Encoding": "identity",
                },
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            # `requests` errors derive from `OSError`, and container readers
            # such as `zipfile` swallow `OSError` while probing - which turns a
            # live 404 or 503 into "this file is not a ZIP". Re-raised as a
            # non-`OSError` so the real status reaches the caller.
            raise RangeReadError(
                f"range read of {redact_url(self.url)} failed: {exc}"
            ) from exc
        if response.status_code != 206:
            raise ValueError(
                f"{redact_url(self.url)} ignored the Range header "
                f"(status {response.status_code}, expected 206); it cannot be "
                f"read by range."
            )
        # Trust the offsets, not the reply length. A server that answers `206`
        # with more bytes than the range asked for would otherwise grow a
        # `bytearray` caller's buffer (assigning past its length is legal) and
        # advance `_pos` by the wrong amount, desyncing every later read.
        received = response.content
        data = received[:want]
        buffer[: len(data)] = data
        self.request_count += 1
        # The bytes that crossed the wire, not the window kept: an over-sending
        # server costs the full body and the stats should say so.
        self.bytes_read += len(received)
        self._pos += len(data)
        return len(data)

    def close(self) -> None:
        """Release the underlying transport.

        Only closes the session when this object created it; an injected
        client is the caller's to manage and may be shared with other readers.
        """
        if self._owns_client:
            # A fake transport injected by a test need not implement `close`.
            closer = getattr(self._client.session, "close", None)
            if callable(closer):
                closer()
        super().close()

    def buffered(
        self, buffer_size: int = DEFAULT_RANGE_BUFFER_SIZE
    ) -> io.BufferedReader:
        """Wrap this file in an :class:`io.BufferedReader`.

        A container reader walks its index in many small reads, and one
        HTTP request per read would be pathological. Buffering coalesces
        them: reading a whole ZIP central directory costs a handful of
        requests instead of hundreds. Always read through this rather
        than the raw object.

        Args:
            buffer_size: Read-ahead buffer in bytes (default 1 MiB).

        Returns:
            io.BufferedReader: The buffered view over this file.

        Examples:
            - The buffered handle is what a container reader is given:
                ```python
                >>> import zipfile
                >>> from earthlens.base.http import HttpRangeFile
                >>> open_remote_zip = lambda url: zipfile.ZipFile(
                ...     HttpRangeFile(url).buffered()
                ... )
                >>> callable(open_remote_zip)
                True

                ```
        """
        return io.BufferedReader(self, buffer_size=buffer_size)
