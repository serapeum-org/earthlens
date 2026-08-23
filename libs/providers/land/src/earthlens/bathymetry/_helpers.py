"""Pure, stateless helpers for the bathymetry backend.

No SDK and no network — every helper here is unit-testable in isolation. Two
groups: the ERDDAP `griddap` subset-URL builders the backend GETs (the exact
shape `…/griddap/<id>.nc?<var>[(lat_lo):1:(lat_hi)][(lon_lo):1:(lon_hi)]`, no
time axis — the DEMs are static — was pinned live in the A1 gate captures),
and the WCS service-failure classification the backend uses to tell a transient
upstream outage from a real request error: `WcsServiceUnavailableError` and
`is_wcs_service_failure`.
"""

from __future__ import annotations

import errno
import re
import socket
import urllib.error
from typing import TYPE_CHECKING

import requests

from earthlens.base import UpstreamUnavailableError, exception_chain, response_status

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent

#: Default sampling stride for a griddap axis range (`1` = full resolution).
_DEFAULT_STEP = 1

#: Parses a `"<value> arc-(second|minute)"` native-resolution label.
_RESOLUTION_RE = re.compile(r"\s*([\d.]+)\s*arc-(second|minute)", re.IGNORECASE)

#: HTTP statuses (besides the whole 5xx range, handled separately) that mean the
#: service — not the request — is at fault: request timeout, too-early, and
#: rate-limit. `400` / `403` / `404` are deliberately excluded: they are real
#: answers to the request and must stay a `ValueError`.
_TRANSIENT_STATUS: frozenset[int] = frozenset({408, 425, 429})

#: Exception types that always mean the transport, not the request, failed.
#: `socket.gaierror` covers DNS resolution; the bare-`OSError` network errnos
#: below cover unreachable-host / no-route cases that are not `ConnectionError`
#: subclasses.
_TRANSPORT_EXC: tuple[type[Exception], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    ConnectionError,
    TimeoutError,
    urllib.error.URLError,
    socket.gaierror,
)

#: `OSError.errno` values that mark a network-path failure (not a bad request).
_NETWORK_ERRNOS: frozenset[int] = frozenset(
    e
    for e in (
        getattr(errno, name, None)
        for name in (
            "ENETUNREACH",
            "EHOSTUNREACH",
            "ECONNREFUSED",
            "ECONNRESET",
            "ECONNABORTED",
            "ETIMEDOUT",
            "ENETDOWN",
            "EHOSTDOWN",
            "ENETRESET",
        )
    )
    if e is not None
)

#: Lower-cased substrings in a WCS/GDAL failure that mark the OGC service — not
#: the request — as the problem: a degraded server answering `GetCapabilities`
#: with an HTML error page (`non-XML body`), a 5xx / gateway error, or a dropped
#: connection. A genuine request error (a coverage id that does not exist, an
#: empty subset intersection) carries none of these, so it stays a `ValueError`.
#: Note: `getcapabilities` alone is intentionally absent — servers name it when
#: reporting an unknown coverage too, so the `non-xml` signature (plus the status
#: checks) catches the real bad-body case without masking a request error.
_SERVICE_SIGNATURES: tuple[str, ...] = (
    "non-xml",
    "non xml",
    "empty reply from server",
    "internal server error",
    "bad gateway",
    "gateway time",
    "service unavailable",
    "temporarily unavailable",
    "too many requests",
    "max retries",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "timed out",
    "failed to establish",
    "name resolution",
    "name or service not known",
    "network is unreachable",
    "no route to host",
)

#: Matches a transient status (`408` / `429` / any `5xx`) in text only where it is
#: unambiguously an HTTP-status token: adjacent to a status keyword
#: (`status` / `code` / `http` / `http/1.1`), or in the `NNN … Error` form. It
#: deliberately does NOT match a bare leading integer or a keyword like
#: `returned`, so a request / size message (`512 x 512 grid`, `500 records
#: returned`, `coverage returned 512 rows`) is never mistaken for a status. A
#: bare status in free text without such a token (a raw CDN `522 …` string) is
#: instead recognised structurally by `_http_status` when the exception carries a
#: `.response`; the text path is only a fallback for wrapped GDAL / CURL strings.
_STATUS_IN_TEXT_RE = re.compile(
    r"(?:\bhttp\b|\bhttp/\d(?:\.\d)?\b|\bstatus\b|\bcode\b)"
    r"[^0-9A-Za-z]{0,4}(?:408|429|5\d\d)\b"
    r"|\b(?:408|429|5\d\d)\s+(?:server|client|internal server) error\b",
    re.I,
)


class WcsServiceUnavailableError(UpstreamUnavailableError):
    """A WCS coverage read failed because the OGC service was unavailable.

    Raised by the bathymetry backend's WCS path when `pyramids.Dataset.from_wcs`
    fails for a **transport / service** reason — the endpoint dropped the
    connection, returned a 5xx / gateway error, or answered `GetCapabilities`
    with a non-XML error page — rather than a request error (a bad bbox or an
    unknown coverage id, which stay a `ValueError`). It is a distinct type so a
    caller — notably a live `e2e` test — can skip on a flaky upstream instead of
    failing, the way the OSM backend's `OhsomeUnavailableError` does.

    Examples:
        - It is a `RuntimeError`, so a broad transport-failure `except` catches it:
            ```python
            >>> from earthlens.bathymetry import WcsServiceUnavailableError
            >>> try:
            ...     raise WcsServiceUnavailableError("the WCS service is unavailable")
            ... except RuntimeError as exc:
            ...     print(exc)
            the WCS service is unavailable

            ```
    """


def is_wcs_service_failure(exc: Exception) -> bool:
    """Return whether `exc` marks the WCS service (not the request) as at fault.

    Walks the exception's cause/context chain and reports `True` when a link is a
    transport error (`requests` / `urllib` connection-timeout types, a
    DNS `gaierror`, or a network-errno `OSError`), carries a **transient HTTP
    status** (`408` / `425` / `429` or any `5xx`, read structurally from a
    `response` / `HTTPError` when present, else from a status token in the text),
    or matches a service signature — a `non-XML body`, an `Empty reply from
    server`, a dropped connection, or a DNS failure. A **definite non-transient
    status** (`400` / `403` / `404`) is authoritative: the request reached the
    service and got a real answer, so it stays `False`. Everything else — an
    unknown coverage, an empty subset intersection — is also `False`, so a
    genuine bug stays a hard failure rather than being masked as "service down".

    Args:
        exc: The exception `pyramids.Dataset.from_wcs` raised.

    Returns:
        `True` when the failure looks like an unavailable / degraded service.

    Examples:
        - A non-XML `GetCapabilities` body is a service failure:
            ```python
            >>> from earthlens.bathymetry._helpers import is_wcs_service_failure
            >>> is_wcs_service_failure(
            ...     RuntimeError("WCS GetCapabilities returned a non-XML body")
            ... )
            True

            ```
        - A dropped connection is a service failure, whatever its message:
            ```python
            >>> import requests
            >>> from earthlens.bathymetry._helpers import is_wcs_service_failure
            >>> is_wcs_service_failure(requests.exceptions.ConnectionError("boom"))
            True

            ```
        - An unknown coverage id is a request error, not a service failure:
            ```python
            >>> from earthlens.bathymetry._helpers import is_wcs_service_failure
            >>> is_wcs_service_failure(RuntimeError("Could not find coverage 'x'"))
            False

            ```
    """
    for link in exception_chain(exc):
        verdict = _link_verdict(link)  # NOSONAR
        if verdict is not None:
            return verdict
    return False


def _link_verdict(link: BaseException) -> bool | None:
    """Classify one exception-chain link as service / request / undecided.

    Args:
        link: One exception from the cause/context chain.

    Returns:
        `True` when the link marks a service/transport failure, `False` when it
        is an authoritative request answer (a definite non-transient HTTP
        status), or `None` when this link alone does not decide it (defer to the
        rest of the chain).
    """
    status = response_status(link)
    if status is not None:
        return status in _TRANSIENT_STATUS or 500 <= status <= 599
    if isinstance(link, _TRANSPORT_EXC):
        return True
    if isinstance(link, OSError) and link.errno in _NETWORK_ERRNOS:
        return True
    message = str(link).lower()
    if any(signature in message for signature in _SERVICE_SIGNATURES):
        return True
    if _STATUS_IN_TEXT_RE.search(message):
        return True
    return None


def resolution_degrees(native_resolution: str) -> float | None:
    """Convert a `"<n> arc-second"` / `"arc-minute"` label to degrees.

    Args:
        native_resolution: A catalog row's `native_resolution` label
            (`"15 arc-second"`, `"1 arc-minute"`).

    Returns:
        float | None: The cell size in degrees, or `None` when the label
            is not a recognised arc-second / arc-minute string.

    Examples:
        - Arc-seconds and arc-minutes convert to degrees:
            ```python
            >>> from earthlens.bathymetry._helpers import resolution_degrees
            >>> round(resolution_degrees("15 arc-second"), 6)
            0.004167
            >>> resolution_degrees("1 arc-minute")
            0.016666666666666666
            >>> resolution_degrees("native") is None
            True

            ```
    """
    match = _RESOLUTION_RE.match(native_resolution or "")
    if match is None:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value / 3600.0 if unit == "second" else value / 60.0


def estimate_grid_pixels(
    bbox: tuple[float, float, float, float], native_resolution: str
) -> tuple[int, int] | None:
    """Estimate the `(width, height)` pixel dimensions of a bbox subset.

    Args:
        bbox: `(west, south, east, north)` in degrees.
        native_resolution: The DEM's `native_resolution` label.

    Returns:
        tuple[int, int] | None: `(width_px, height_px)`, each at least 1, or
            `None` when the resolution label is not parseable.
    """
    degrees = resolution_degrees(native_resolution)
    if degrees is None or degrees <= 0:
        return None
    west, south, east, north = bbox
    width = max(1, round(abs(east - west) / degrees))
    height = max(1, round(abs(north - south) / degrees))
    return width, height


def _normalise_lon(lon: float, lon_convention: str) -> float:
    """Map a `-180..180` longitude onto the server's convention.

    Args:
        lon: A longitude in the user's `-180..180` frame.
        lon_convention: The server's frame — `"-180..180"` (pass through)
            or `"0..360"` (wrap negatives, e.g. `-18 -> 342`).

    Returns:
        float: The longitude in the server's frame.
    """
    if lon_convention == "0..360":
        return lon % 360.0
    return float(lon)


def bbox_from_extent(space: SpatialExtent) -> tuple[float, float, float, float]:
    """Return the `(west, south, east, north)` bbox of a spatial extent.

    Args:
        space: A :class:`~earthlens.base.SpatialExtent` (the backend's
            `self.space`).

    Returns:
        tuple[float, float, float, float]: `(west, south, east, north)` in
            degrees.
    """
    return (space.west, space.south, space.east, space.north)


def griddap_subset_url(
    endpoint: str,
    dataset_id: str,
    variable: str,
    bbox: tuple[float, float, float, float],
    lon_convention: str = "-180..180",
    step: int = _DEFAULT_STEP,
) -> str:
    """Build the ERDDAP `griddap` `.nc` subset URL for a static DEM bbox.

    The DEMs have no time axis, so the URL carries exactly two coordinate
    ranges — latitude then longitude, matching the grid's `[latitude]
    [longitude]` dimension order. The request bbox (`-180..180`) is
    normalised to the server's `lon_convention` first.

    Args:
        endpoint: ERDDAP base URL (a trailing slash is tolerated).
        dataset_id: The griddap coverage id on that server.
        variable: The elevation band name (`"elevation"` / `"z"`).
        bbox: `(west, south, east, north)` in `-180..180` degrees.
        lon_convention: The server's longitude frame — `"-180..180"` or
            `"0..360"`.
        step: Sampling stride per axis (`1` = native resolution).

    Returns:
        str: The full `…/griddap/<id>.nc?<var>[(s):step:(n)][(w):step:(e)]`
            download URL.

    Raises:
        ValueError: If, after normalisation, the western longitude exceeds
            the eastern one (an antimeridian-crossing bbox the single-URL
            form cannot express — split it into two requests).

    Examples:
        - A `-180..180` row passes the bbox straight through:
            ```python
            >>> from earthlens.bathymetry._helpers import griddap_subset_url
            >>> griddap_subset_url(
            ...     "https://coastwatch.pfeg.noaa.gov/erddap",
            ...     "GEBCO_2020",
            ...     "elevation",
            ...     (-18.0, 25.0, -17.0, 26.0),
            ... )
            'https://coastwatch.pfeg.noaa.gov/erddap/griddap/GEBCO_2020.nc?elevation[(25.0):1:(26.0)][(-18.0):1:(-17.0)]'

            ```
        - A `0..360` row wraps negative longitudes:
            ```python
            >>> griddap_subset_url(
            ...     "https://example.org/erddap",
            ...     "DEM360",
            ...     "z",
            ...     (-18.0, 25.0, -17.0, 26.0),
            ...     lon_convention="0..360",
            ... )
            'https://example.org/erddap/griddap/DEM360.nc?z[(25.0):1:(26.0)][(342.0):1:(343.0)]'

            ```
    """
    west, south, east, north = bbox
    west_n = _normalise_lon(west, lon_convention)
    east_n = _normalise_lon(east, lon_convention)
    if west_n > east_n:
        raise ValueError(
            f"bbox is inverted or crosses the antimeridian in the server's "
            f"{lon_convention!r} frame (west {west_n} > east {east_n}): pass "
            "west < east for a contiguous box, or split an "
            "antimeridian-crossing request into two."
        )
    base = f"{endpoint.rstrip('/')}/griddap/{dataset_id}.nc?"
    lat_range = f"[({south}):{step}:({north})]"
    lon_range = f"[({west_n}):{step}:({east_n})]"
    return f"{base}{variable}{lat_range}{lon_range}"
