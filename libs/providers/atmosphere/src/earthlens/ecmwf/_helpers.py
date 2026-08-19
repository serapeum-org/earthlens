"""Private, stateless helpers for the ECMWF / CADS backend.

No SDK client is constructed here and no state is held: these classify what a
failed `cdsapi` retrieve *was* and drive the retry policy around it, so the
backend keeps only the `AbstractDataSource` implementation. Mirrors the typed
availability error every sibling backend that faces a flaky upstream ships in
its own `_helpers` (`gdacs`, `osm`, `bathymetry`).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from loguru import logger

from earthlens.ecmwf.endpoints import endpoint_url


def _looks_like_licence_not_accepted(exc: BaseException) -> bool:
    """Heuristic: does this exception come from an unaccepted CDS licence?

    CDS returns HTTP 403 with a body that mentions "Required licences
    not accepted" (or "licence" depending on locale) when the user has
    a valid Personal Access Token but has not ticked the licence on
    the dataset's download page. cdsapi raises this through to the
    caller as a generic exception; we detect it by message scan so we
    can rewrite into a :class:`PermissionError` that names the
    dataset URL.

    Args:
        exc: The exception raised by `client.retrieve(...)`.

    Returns:
        True if the message looks like a licence-acceptance failure;
        False otherwise.
    """
    message = str(exc).lower()
    return (
        "licence" in message
        or "license" in message
        or "403" in message
        and ("accept" in message or "term" in message)
    )


class CadsUnavailableError(RuntimeError):
    """A CADS store refused the retrieve after the backend's retries.

    Raised when a store rejects a request for a reason that is the *service*,
    not the request: the per-dataset queue limit every CADS instance enforces
    per account. The store accepts the job and then rejects it with
    `Number queued requests for this dataset is temporarily limited`, which is
    transient — a different day, or a quieter account, and the identical request
    succeeds. Carries the originating HTTP `status_code` when one is
    discernible, so a caller — a live e2e test especially — can tell a throttled
    store apart from a genuine request error and skip rather than fail.

    Examples:
        - The typed error carries the status a caller branches on:
            ```python
            >>> from earthlens.ecmwf import CadsUnavailableError
            >>> err = CadsUnavailableError("ECDS is throttling", status_code=400)
            >>> err.status_code
            400
            >>> str(err)
            'ECDS is throttling'

            ```
        - A refusal with no discernible status carries `None`:
            ```python
            >>> from earthlens.ecmwf import CadsUnavailableError
            >>> CadsUnavailableError("queue limited").status_code is None
            True

            ```
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Store the actionable message and the originating HTTP status.

        Args:
            message: What the store refused and why, in the caller's terms.
            status_code: The HTTP status when one could be read off the
                failure, else `None`.
        """
        super().__init__(message)
        self.status_code = status_code


def _looks_like_throttled(exc: BaseException) -> bool:
    """Is this a CADS queue-limit rejection rather than a bad request?

    A throttled retrieve fails with a `400`, the same status a malformed
    request gets, so the status alone cannot separate them — the wording does.
    The store states the reason plainly (`the job has been rejected`,
    `temporarily limited`), and the request itself was already validated
    offline against `constraints.json` before submission.

    Args:
        exc: The exception raised by `client.retrieve(...)`.

    Returns:
        True when the message looks like a queue-limit refusal; False for a
        genuine request error that must not be retried.
    """
    # Message sniffing is only safe on the transport errors cdsapi raises. A
    # ValueError or AssertionError reaching here is our own bug or a test's
    # assertion, and misreading one as a throttle would burn three attempts and
    # then let a live test *skip* over a real failure.
    if isinstance(exc, ValueError | AssertionError):
        return False
    message = str(exc).lower()
    return "temporarily limited" in message or "queued requests" in message


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield `exc` then each linked `__cause__` / `__context__`, cycle-safe.

    Args:
        exc: The exception to walk.

    Yields:
        BaseException: Each exception in the chain, most recent first.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        # Honour `raise ... from None`: an explicit cause wins, otherwise follow
        # the implicit context unless the author suppressed it (matching
        # Python's own traceback display), so a deliberately surfaced failure is
        # not reclassified through a context it asked to hide.
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__


def _status_of(exc: BaseException) -> int | None:
    """Return the HTTP status behind a failed retrieve, when discernible.

    Walks the exception chain, because `cdsapi` wraps the `requests` error that
    carries the `response` — reading only the outermost exception would answer
    `None` for most real refusals.

    Args:
        exc: The exception raised by `client.retrieve(...)`.

    Returns:
        int | None: The HTTP status, from a `response` object when one is
            reachable and otherwise parsed out of the message; `None` when
            neither yields one, as a transport drop carries no status.
    """
    for link in _exception_chain(exc):
        status = getattr(getattr(link, "response", None), "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
        # The status is often only in the text of the wrapped error, so scan
        # each link rather than the outermost message alone.
        parsed = _status_in_message(str(link))
        if parsed is not None:
            return parsed
    return None


def _status_in_message(text: str) -> int | None:
    """Return the `NNN Client/Server Error` status in `text`, when present."""
    match = re.search(r"\b(\d{3})\s+(?:server|client)\s+error", text, re.I)
    return int(match.group(1)) if match else None


#: Attempts a throttled retrieve gets before the store is declared unavailable.
CADS_MAX_ATTEMPTS = 3

#: Base seconds for the exponential wait between throttled attempts
#: (`CADS_BACKOFF_SECONDS * 2**attempt`).
CADS_BACKOFF_SECONDS = 2.0


def _retrieve_with_retry(
    client: Any,
    dataset: str,
    request: dict[str, Any],
    target: Path,
    endpoint: str,
) -> None:
    """Retrieve `dataset` into `target`, retrying while the store is throttling.

    Wraps the one `cdsapi` call every retrieve path makes, so the two failure
    modes a CADS store has for a well-formed request are handled in one place:

    * **licence not accepted** - permanent until the user ticks the licence, so
      it is rewritten into a :class:`PermissionError` naming the dataset page.
    * **queue limit** - transient, so it is retried with an exponential wait and,
      if it outlives the attempts, raised as :class:`CadsUnavailableError` so a
      caller can skip rather than fail.

    Anything else propagates untouched: a malformed request must fail fast, not
    be retried into the same error three times.

    Args:
        client: The `cdsapi` client for `endpoint`.
        dataset: Upstream dataset id to retrieve.
        request: The request body, already validated against `constraints.json`.
        target: Destination path for the retrieved bytes.
        endpoint: CADS instance slug, used for the messages.

    Returns:
        None: The bytes are written to `target`.

    Raises:
        PermissionError: The dataset's licence has not been accepted.
        CadsUnavailableError: The store kept refusing the job on its queue limit.
    """
    # Retrieve into a sidecar and move it into place, per the repo's atomic
    # download contract: writing straight to `target` truncates a pre-existing
    # good file on the first attempt, and an exhausted retry would leave the
    # stub behind as though it were the data.
    part = target.with_name(target.name + ".part")
    last: BaseException | None = None
    for attempt in range(CADS_MAX_ATTEMPTS):
        try:
            client.retrieve(dataset, request, str(part))
        except Exception as exc:  # noqa: BLE001 - cdsapi raises many types; classified here
            part.unlink(missing_ok=True)
            if _looks_like_licence_not_accepted(exc):
                base = endpoint_url(endpoint).rsplit("/api", 1)[0]
                raise PermissionError(
                    f"{endpoint.upper()} rejected the request for {dataset!r}: "
                    f"licence not accepted. Open the dataset page at "
                    f"{base}/datasets/{dataset} and tick the licence at the "
                    "bottom of the 'Download' tab. The acceptance is permanent "
                    "and tied to your Copernicus account."
                ) from exc
            if not _looks_like_throttled(exc):
                raise
            last = exc
            if attempt + 1 < CADS_MAX_ATTEMPTS:
                wait = CADS_BACKOFF_SECONDS * 2**attempt
                logger.warning(
                    f"{endpoint.upper()} is throttling {dataset!r} "
                    f"(attempt {attempt + 1}/{CADS_MAX_ATTEMPTS}); "
                    f"retrying in {wait:.0f}s"
                )
                time.sleep(wait)
        else:
            # Outside the `except`: a failure moving the file is not a failed
            # retrieve, and running it in the handler's scope would unlink the
            # bytes just downloaded. A retrieve that wrote nothing is a real
            # failure — reporting success would hand back whatever stale file
            # happened to be at `target`.
            if not part.exists():
                raise CadsUnavailableError(
                    f"{endpoint.upper()} returned no data for {dataset!r}: the "
                    "retrieve reported success but wrote no file.",
                    status_code=None,
                )
            os.replace(part, target)
            return
    raise CadsUnavailableError(
        f"{endpoint.upper()} refused {dataset!r} after {CADS_MAX_ATTEMPTS} "
        f"attempts: the per-dataset queue limit is in force for this account. "
        f"This is temporary - retry later rather than changing the request. "
        f"Upstream said: {last}",
        status_code=_status_of(last) if last is not None else None,
    )
