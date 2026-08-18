"""The live Overture release axis: what a release id is, and which exist.

Overture publishes a dated release roughly monthly (`yyyy-mm-dd.x`) and
keeps only the newest one or two on `s3://overturemaps-us-west-2`, pruning
the rest. Every consumer in this package therefore needs the same two
things — a way to recognise a release id, and the ids Overture publishes
*now* — so both live here rather than being re-derived per module.

The live reads go through `earthlens.base.HttpClient`, not the
`overturemaps` SDK. The SDK's own lookups call `urlopen(STAC_CATALOG_URL)`
with no `timeout=`, which inherits the global socket default of `None`: on
a route that drops rather than refuses — a firewall that allows the public
S3 bucket but not `stac.overturemaps.org` — a download would block
indefinitely, and the offline fallback that exists for exactly that
situation would never be reached. `HttpClient` bounds the request and
applies the repo's shared retry and user-agent policy. Only the SDK's
`STAC_CATALOG_URL` constant is reused, so the endpoint stays in one place.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from earthlens.base import HttpClient

#: Shape of an Overture release id: a release date plus an ordinal
#: (`2026-07-22.0`). Matched with `fullmatch`, so no trailing newline slips
#: through. This is a *shape* check, not an existence or validity one —
#: `9999-99-99.0` matches — so it guards against garbage (the `https:`
#: fragments the SDK's own parser emits) and typos, not against an id
#: Overture has pruned.
RELEASE_ID_RE = re.compile(r"\d{4}-\d{2}-\d{2}\.\d+")

#: Bounds the STAC read: `(connect, read)` seconds. Short enough that a
#: black-holed route degrades to the bundled index promptly rather than
#: stalling a download.
STAC_TIMEOUT = (5.0, 15.0)


class ReleaseLookupError(RuntimeError):
    """Raised when Overture's STAC catalog cannot be read or parsed."""


def is_release_id(value: object) -> bool:
    """Return whether `value` is shaped like an Overture release id.

    Args:
        value: Any object. A non-string is never a release id, which keeps
            callers from having to pre-check the type.

    Returns:
        bool: `True` when `value` is a string of the form
            `yyyy-mm-dd.<ordinal>`.

    Examples:
        - A published release id is accepted:
            ```python
            >>> from earthlens.overture.releases import is_release_id
            >>> is_release_id("2026-07-22.0")
            True

            ```
        - The `https:` fragment the SDK's parser emits is not:
            ```python
            >>> from earthlens.overture.releases import is_release_id
            >>> [is_release_id(v) for v in ("https:", "2026-7-22.0", None)]
            [False, False, False]

            ```
    """
    return isinstance(value, str) and RELEASE_ID_RE.fullmatch(value) is not None


def stac_catalog(client: HttpClient | None = None) -> dict[str, Any]:
    """Fetch Overture's STAC root catalog, bounded and retry-wrapped.

    Args:
        client: Transport to read through. Defaults to a `HttpClient`
            carrying `STAC_TIMEOUT`.

    Returns:
        dict[str, Any]: The decoded catalog document.

    Raises:
        ReleaseLookupError: If the catalog cannot be fetched, is not JSON,
            or is not a JSON object.
    """
    from overturemaps.core import STAC_CATALOG_URL

    transport = client or HttpClient(timeout=STAC_TIMEOUT)
    try:
        document = transport.get_json(STAC_CATALOG_URL)
    # Transport, status, and decode failures all mean the same thing to every
    # caller — the catalog could not be read — so they are reported as one.
    except Exception as exc:  # noqa: BLE001
        raise ReleaseLookupError(
            f"could not read Overture's STAC catalog at {STAC_CATALOG_URL} ({exc})"
        ) from exc
    if not isinstance(document, dict):
        raise ReleaseLookupError(
            f"Overture's STAC catalog at {STAC_CATALOG_URL} is not a JSON object"
        )
    return document


def latest_release(client: HttpClient | None = None) -> str:
    """Return the release id Overture currently publishes.

    Reads the catalog's dedicated `latest` key — the same value the SDK's
    `get_latest_release()` returns, but over a bounded transport.

    Args:
        client: Transport to read through (see `stac_catalog`).

    Returns:
        str: The published release id.

    Raises:
        ReleaseLookupError: If the catalog cannot be read, or its `latest`
            is missing or not shaped like a release id.
    """
    latest = stac_catalog(client).get("latest")
    if not is_release_id(latest):
        raise ReleaseLookupError(
            f"Overture's STAC catalog reported latest={latest!r}, which is not "
            "a release id"
        )
    return str(latest)


def child_release_ids(client: HttpClient | None = None) -> list[str]:
    """Return every release id the STAC catalog links to as a child.

    Each child link points at `<base>/<release>/catalog.json`, so the
    release is the second-to-last segment of the URL *path*. Read directly
    rather than through the SDK, whose `get_available_releases()` derives
    the ids by splitting the whole href on `/` after stripping `./` —
    which yields `https:` now that the catalog serves absolute hrefs.
    Parsing the path rather than the raw href is what keeps the scheme and
    host out of the result.

    Args:
        client: Transport to read through (see `stac_catalog`).

    Returns:
        list[str]: The linked release ids, in catalog order, skipping any
            href too short to carry one.

    Raises:
        ReleaseLookupError: If the catalog cannot be read or parsed.
    """
    ids: list[str] = []
    links = stac_catalog(client).get("links")
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict) or link.get("rel") != "child":
            continue
        path = urlparse(str(link.get("href", ""))).path
        segments = [part for part in path.split("/") if part]
        if len(segments) >= 2:
            ids.append(segments[-2])
    return ids


__all__ = [
    "RELEASE_ID_RE",
    "STAC_TIMEOUT",
    "ReleaseLookupError",
    "child_release_ids",
    "is_release_id",
    "latest_release",
    "stac_catalog",
]
