"""`gportal` protocol branch — search + SFTP download via the `gportal` SDK.

Imports `gportal` lazily so the wider `earthlens.jaxa` surface stays
importable without the `[jaxa]` extra. The branch translates the
`SpatialExtent` + `TemporalExtent` into a `gportal.search(...)` call,
materialises the resulting `Search.products()` iterator, and downloads
each `Product` over SFTP via `gportal.download(...)`.

`gportal.download` is sequential upstream (a list comprehension in
`gportal/sftp.py`); the function has no `processes=` argument and adding
concurrency at this layer is a follow-on. Credentials are passed as
explicit kwargs to `gportal.download(...)` rather than via the SDK's
module-level `gportal.username` / `.password` globals — the auth object
holds the resolved values privately so no module-level mutation leaks
between requests.
"""

from __future__ import annotations

import socket
from pathlib import Path

import requests

from earthlens.base import UpstreamUnavailableError, http_status
from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
from earthlens.jaxa.auth import AuthenticationError, JaxaAuth
from earthlens.jaxa.catalog import Dataset


class GportalUnavailableError(UpstreamUnavailableError):
    """G-Portal was unreachable or answered with something other than JSON.

    JAXA's G-Portal serves an HTML error page rather than a JSON body when it
    is down or throttling, which surfaces from the `gportal` SDK as a bare
    `requests.HTTPError` (on a `5xx`, from `response.raise_for_status`) or a
    `requests.exceptions.JSONDecodeError` — which subclasses
    `requests.RequestException` — when a non-error status carries an HTML body
    and `response.json` fails. The later SFTP download goes over `paramiko`, so
    the same outage mid-transfer surfaces as a `paramiko.SSHException` or a
    connection-level error (a dropped / reset / timed-out socket, or a DNS
    failure); those are translated here too. A `paramiko.AuthenticationException`
    is not (a credential rejection is a login problem, not an outage), and
    neither is a local filesystem error while writing the download (disk full /
    permission denied) — both propagate raw. Raised in its place so a caller — a
    live `e2e` test especially — can tell a transient JAXA outage from a genuine
    request error and skip rather than fail. Carries the originating HTTP
    `status_code` when one is discernible from the chained exception (the SFTP
    path has none, so it is `None` there).

    Examples:
        - The typed error carries the status a caller branches on:
            ```python
            >>> from earthlens.jaxa import GportalUnavailableError
            >>> err = GportalUnavailableError("G-Portal unavailable", status_code=503)
            >>> err.status_code
            503
            >>> str(err)
            'G-Portal unavailable'

            ```
        - A non-JSON body with no readable status carries `None`:
            ```python
            >>> from earthlens.jaxa import GportalUnavailableError
            >>> GportalUnavailableError("HTML error page").status_code is None
            True

            ```
    """


def fetch_gportal(
    *,
    dataset: Dataset,
    space: SpatialExtent,
    time: TemporalExtent,
    auth: JaxaAuth,
    out_dir: Path,
) -> list[Path]:
    """Search G-Portal for a dataset and SFTP-download the matching products.

    Args:
        dataset: The resolved catalog row (its `short_name` is the
            G-Portal numeric dataset id passed to `gportal.search`).
        space: The validated WGS84 bbox.
        time: The validated date window.
        auth: A configured `JaxaAuth`; its
            `username` and
            `password` are passed straight
            to ``gportal.download(username=, password=)`` so the SDK's
            module-level credential globals stay untouched.
        out_dir: Output directory (created if missing).

    Returns:
        list[Path]: One written path per product the search returned.
            Empty list when the search matches no products (treated as a
            normal empty AOI/time response, not an error).

    Raises:
        ImportError: If the `gportal` SDK is not installed.
        ValueError: If `dataset.short_name` is missing — bad catalog row.
        AuthenticationError: When `auth` has not been configured (i.e.
            its `username` / `password` are still `None`).
        GportalUnavailableError: When the catalogue search or the SFTP
            download fails because the service is down or throttling — an HTTP
            `5xx` or a non-JSON (HTML) error page on the search
            (`requests.HTTPError` / `requests.exceptions.JSONDecodeError`), or a
            dropped/throttled SFTP transfer on the download (`paramiko.SSHException`
            or a reset / timed-out socket / DNS failure).
        requests.HTTPError: A `4xx` client error on the catalogue search
            propagates unchanged — G-Portal rejected the request, which is a
            request problem to fix, not an outage to retry.
        paramiko.AuthenticationException: Propagated unchanged when G-Portal
            rejects the SFTP credentials — a login problem, not an outage.
        OSError: A local filesystem failure while writing the download (disk
            full, permission denied) propagates unchanged — not a JAXA outage.
    """
    try:
        import gportal  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "the 'gportal' SDK is required for the gportal protocol. "
            "Install it via the [jaxa] extra: pip install 'earthlens[jaxa]'."
        ) from exc

    if not dataset.short_name:
        raise ValueError(
            f"dataset {dataset.key!r} has no short_name — bad catalog row."
        )

    if auth.username is None or auth.password is None:
        raise AuthenticationError(
            "G-Portal credentials are not resolved; call auth.configure() first."
        )

    search = gportal.search(
        dataset_ids=[dataset.short_name],
        start_time=time.start_date.isoformat(),
        end_time=time.end_date.isoformat(),
        bbox=[space.west, space.south, space.east, space.north],
    )
    try:
        matched = search.matched() or 0
        if matched == 0:
            return []
        products = list(search.products())
    except requests.RequestException as exc:
        status = http_status(exc)
        if status is not None and 400 <= status < 500:
            # A 4xx is a client/request error, not an outage — G-Portal
            # answered, it just rejected the request. Surface it as-is so a
            # caller fixes the request rather than being told to retry later.
            raise
        # Any other failure from the SDK's requests layer means G-Portal did
        # not usefully answer the catalogue search, so surface it as
        # unavailable: a connection drop / timeout, a 5xx from
        # raise_for_status, or the JSONDecodeError that response.json() raises
        # on an HTML error page (requests.exceptions.JSONDecodeError subclasses
        # RequestException). The request-construction subclasses (MissingSchema
        # / InvalidURL) cannot arise here because the SDK builds its own URLs.
        raise GportalUnavailableError(
            "JAXA G-Portal did not return a usable response for the catalogue "
            "search; the service is likely down or throttling. Retry later "
            "rather than changing the request.",
            status_code=status,
        ) from exc
    if not products:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    import paramiko

    try:
        paths = gportal.download(
            products,
            local_dir=str(out_dir),
            username=auth.username,
            password=auth.password.get_secret_value(),
        )
    except paramiko.AuthenticationException:
        # A credential rejection is not an outage — surface it as-is so the
        # user fixes their G-Portal login rather than "retrying later".
        raise
    except (
        paramiko.SSHException,
        paramiko.ssh_exception.NoValidConnectionsError,
        ConnectionError,
        TimeoutError,
        socket.gaierror,
    ) as exc:
        # The SFTP transfer goes over paramiko, so a dropped / throttled
        # download raises an SSHException (protocol error) or a connection-level
        # failure (NoValidConnectionsError, reset, timeout, DNS failure). Only
        # those transport shapes are translated — a LOCAL filesystem OSError
        # from writing the file (disk full, permission denied, size mismatch,
        # all raised inside paramiko's SFTPClient.get) is the caller's
        # environment, not a JAXA outage, and is deliberately left to propagate
        # raw so "retry later" is not offered as wrong remediation.
        raise GportalUnavailableError(
            "JAXA G-Portal dropped the SFTP connection during download; the "
            "service is likely down or throttling. Retry later rather than "
            "changing the request."
        ) from exc
    if isinstance(paths, str):
        paths = [paths]
    return [Path(p) for p in paths]
