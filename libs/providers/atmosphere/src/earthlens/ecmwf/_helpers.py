"""Private, stateless helpers for the ECMWF / CADS backend.

No SDK client is constructed here and no state is held: these classify what a
failed `cdsapi` retrieve *was* and drive the retry policy around it, so the
backend keeps only the `AbstractDataSource` implementation. Mirrors the typed
availability error every sibling backend that faces a flaky upstream ships in
its own `_helpers` (`gdacs`, `osm`, `bathymetry`).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

from earthlens.base import UpstreamUnavailableError, http_status
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


def endpoint_for(dataset: str) -> str:
    """Resolve which CADS store serves `dataset`.

    Checks the curated rows first, since a row's `endpoint` is authoritative
    for the dataset it describes, then the per-store availability index — which
    covers every id the stores publish, curated or not. That second lookup is
    the one that matters for `curate`, whose whole purpose is datasets with no
    curated row yet: without it those resolve to `cds` and every ADS / EWDS /
    ECDS / XDS id fails with `process not found`.

    Args:
        dataset: The upstream dataset id.

    Returns:
        str: The store slug, defaulting to `"cds"` for an id neither the
            curated rows nor the index knows.
    """
    from earthlens.ecmwf.catalog import Catalog

    catalog = Catalog()
    record = catalog.datasets.get(dataset)
    if record is not None:
        return record.endpoint
    store = catalog.store_for(dataset)
    if store is not None:
        return store
    logger.warning(
        f"{dataset!r} is in neither the curated rows nor the availability "
        "index; assuming the CDS store. Run `earthlens datasets refresh ecmwf` "
        "if it is new upstream."
    )
    return "cds"


class CadsUnavailableError(UpstreamUnavailableError):
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

    The `(message, status_code)` constructor is inherited from
    :class:`~earthlens.base.UpstreamUnavailableError`.
    """


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
    # A 429, or any 5xx, is transient by definition and worth the same retry as
    # the CADS queue limit; classifying only on the queue wording would let a
    # store's rate limiter or a bad gateway fail on the first attempt.
    status = _status_of(exc)
    if status is not None and (status == 429 or 500 <= status < 600):
        return True
    message = str(exc).lower()
    return "temporarily limited" in message or "queued requests" in message


def _status_of(exc: BaseException) -> int | None:
    """Return the HTTP status behind a failed retrieve, when discernible.

    A thin wrapper over :func:`earthlens.base.http_status`: it walks the
    exception chain, because `cdsapi` wraps the `requests` error that carries the
    `response` — reading only the outermost exception would answer `None` for
    most real refusals. The status comes from a `response` object when one is
    reachable and otherwise from a `NNN Server/Client Error` in a link's message.

    Args:
        exc: The exception raised by `client.retrieve(...)`.

    Returns:
        int | None: The HTTP status, or `None` when neither a response nor the
            message yields one (a transport drop carries none).
    """
    return http_status(exc)


#: Attempts a throttled retrieve gets before the store is declared unavailable.
CADS_MAX_ATTEMPTS = 3

#: Base seconds for the exponential wait between throttled attempts
#: (`CADS_BACKOFF_SECONDS * 2**attempt`).
CADS_BACKOFF_SECONDS = 2.0


def _is_retryable_failure(exc: BaseException, dataset: str, endpoint: str) -> bool:
    """Say whether a failed retrieve is a transient refusal worth retrying.

    An unaccepted licence is permanent, so it is rewritten into a
    :class:`PermissionError` naming the dataset page; any other non-throttle
    failure propagates untouched, because retrying a malformed request only
    reproduces the same error.

    Args:
        exc: The exception `client.retrieve(...)` raised.
        dataset: Upstream dataset id, for the message.
        endpoint: CADS instance slug, for the message.

    Returns:
        bool: `True` when the store was throttling and the retrieve should be
            tried again; `False` when the failure is the caller's to re-raise.
            The caller does that with a bare `raise` inside its own handler,
            which keeps the original traceback rather than restarting it here.

    Raises:
        PermissionError: The dataset's licence has not been accepted — that is
            permanent, so it is rewritten here rather than retried.
    """
    if _looks_like_licence_not_accepted(exc):
        base = endpoint_url(endpoint).rsplit("/api", 1)[0]
        raise PermissionError(
            f"{endpoint.upper()} rejected the request for {dataset!r}: "
            f"licence not accepted. Open the dataset page at "
            f"{base}/datasets/{dataset} and tick the licence at the "
            "bottom of the 'Download' tab. The acceptance is permanent "
            "and tied to your Copernicus account."
        ) from exc
    return _looks_like_throttled(exc)


def _wait_before_retry(attempt: int, dataset: str, endpoint: str) -> None:
    """Sleep the exponential backoff for `attempt`, unless it was the last."""
    if attempt + 1 >= CADS_MAX_ATTEMPTS:
        return
    wait = CADS_BACKOFF_SECONDS * 2**attempt
    logger.warning(
        f"{endpoint.upper()} is throttling {dataset!r} "
        f"(attempt {attempt + 1}/{CADS_MAX_ATTEMPTS}); retrying in {wait:.0f}s"
    )
    time.sleep(wait)


def _commit_download(part: Path, target: Path, dataset: str, endpoint: str) -> None:
    """Move a completed sidecar onto `target`, or fail if nothing was written.

    Args:
        part: The sidecar the retrieve was told to write.
        target: Where the data belongs once the retrieve succeeded.
        dataset: Upstream dataset id, for the message.
        endpoint: CADS instance slug, for the message.

    Raises:
        CadsUnavailableError: The retrieve reported success but wrote no file;
            returning normally would hand back whatever stale file happened to
            be at `target`.
    """
    if not part.exists():
        raise CadsUnavailableError(
            f"{endpoint.upper()} returned no data for {dataset!r}: the "
            "retrieve reported success but wrote no file.",
            status_code=None,
        )
    os.replace(part, target)


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
            if not _is_retryable_failure(exc, dataset, endpoint):
                raise
            last = exc
            _wait_before_retry(attempt, dataset, endpoint)
        else:
            # Outside the `except`: a failure moving the file is not a failed
            # retrieve, and running it in the handler's scope would unlink the
            # bytes just downloaded.
            _commit_download(part, target, dataset, endpoint)
            return
    raise CadsUnavailableError(
        f"{endpoint.upper()} refused {dataset!r} after {CADS_MAX_ATTEMPTS} "
        f"attempts: the per-dataset queue limit is in force for this account. "
        f"This is temporary - retry later rather than changing the request. "
        f"Upstream said: {last}",
        status_code=_status_of(last) if last is not None else None,
    )
