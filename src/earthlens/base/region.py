"""Cloud region-affinity helper for the download-orchestration policy.

`region_affinity(bucket_region)` answers one question: *is this process
running in the same cloud region as the target bucket?* — so a backend
can prefer a cheap in-region streaming read over slow, billed
cross-region egress. It generalises the AWS-only decision the earthdata
backend already ships (stream via `earthaccess.open` only when the caller
runs in the DAAC's region, else HTTPS `earthaccess.download`) and adds
GCP + Azure detection.

Resolution is **env-first** (zero-cost, covers most CI / Lambda / EC2
cases), then an optional **instance-metadata probe** behind a hard short
timeout and a per-process cache (`C2`). Detection **never raises and
never blocks**: any failure yields `"unknown"`, and `probe=False`
disables the network probe entirely.

Detection is stdlib `os.environ` + (in `C2`) a small `urllib.request`
GET with an explicit timeout — no `boto3` / `google-cloud-*` /
`azure-*` import merely to read a region, so this module adds no
dependency.
"""

from __future__ import annotations

import http.client
import os
import urllib.request

from loguru import logger

#: The process runs in the same region as the target bucket.
IN_REGION = "in-region"

#: The process and the target bucket are in different (known) regions.
EGRESS = "egress"

#: The caller region could not be determined.
UNKNOWN = "unknown"

#: Default size above which a cross-region pull warrants an egress warning
#: (1 GiB). A parameter of :func:`warn_if_egress`, never hard-coded at a
#: call site.
DEFAULT_EGRESS_THRESHOLD_BYTES = 1 << 30

#: Hard timeout (seconds) for every instance-metadata probe. Keeps the
#: fallback from ever blocking a download when the endpoint is absent.
PROBE_TIMEOUT = 1.0

#: AWS IMDSv2 base — a token PUT then a region GET.
_AWS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
_AWS_REGION_URL = "http://169.254.169.254/latest/meta-data/placement/region"

#: GCP metadata zone endpoint (needs the `Metadata-Flavor: Google` header).
_GCP_ZONE_URL = "http://metadata.google.internal/computeMetadata/v1/instance/zone"

#: Azure IMDS location endpoint (needs the `Metadata: true` header).
_AZURE_LOCATION_URL = (
    "http://169.254.169.254/metadata/instance/compute/location?api-version=2021-02-01"
)

#: Sentinel marking the per-process probe cache as not-yet-populated.
_UNSET: object = object()

#: Per-process cache of the detected caller region (`None` = probed but
#: undetermined). Reset with :func:`clear_region_cache`. Intentionally
#: lock-free: a first-call race between threads only repeats the fail-safe
#: probe, and the assignment is atomic under the GIL, so there is no
#: correctness bug — only (at most) a redundant probe on a cold start.
_cached_region: str | None | object = _UNSET


def clear_region_cache() -> None:
    """Reset the per-process caller-region probe cache.

    The metadata probe runs at most once per process; call this to force
    a fresh probe (chiefly in tests that inject different probe results).
    """
    global _cached_region
    _cached_region = _UNSET


def _caller_region_from_env() -> str | None:
    """Return the caller's AWS region from the environment, if set.

    Reads `AWS_REGION` first, then `AWS_DEFAULT_REGION`. This is the
    zero-cost signal that covers most CI, AWS Lambda, and
    EC2-with-env-config callers.

    Returns:
        The region string, or `None` when neither variable is set.
    """
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None


def _metadata_request(
    url: str,
    *,
    headers: dict[str, str],
    method: str = "GET",
    timeout: float = PROBE_TIMEOUT,
) -> str | None:
    """Fetch an instance-metadata endpoint, fail-safe to `None`.

    The single network seam for every probe: any error (no route,
    timeout, non-200, decode failure) yields `None` rather than raising,
    so detection never blocks or crashes a download.

    Args:
        url: The metadata endpoint URL.
        headers: Request headers required by the endpoint.
        method: HTTP method (`"GET"`, or `"PUT"` for the AWS token).
        timeout: Hard timeout in seconds.

    Returns:
        The stripped response body, or `None` on any failure / empty body.
    """
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = getattr(response, "status", 200) or 200
            if status != 200:
                return None
            body = response.read().decode("utf-8", "replace").strip()
            return body or None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _probe_aws(timeout: float) -> str | None:
    """Probe AWS IMDSv2 for the caller region (token dance, then region)."""
    token = _metadata_request(
        _AWS_TOKEN_URL,
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        method="PUT",
        timeout=timeout,
    )
    if not token:
        return None
    return _metadata_request(
        _AWS_REGION_URL,
        headers={"X-aws-ec2-metadata-token": token},
        timeout=timeout,
    )


def _normalize_gcp_zone(zone: str) -> str | None:
    """Normalise a GCP zone string to a region.

    Turns `projects/123/zones/us-west1-a` (or the bare `us-west1-a`) into
    `us-west1` by dropping the path prefix and the trailing zone letter.

    Args:
        zone: The raw zone string from the metadata endpoint.

    Returns:
        The region, or `None` when the input is empty.
    """
    tail = zone.rsplit("/", 1)[-1]
    if not tail:
        return None
    region, _, letter = tail.rpartition("-")
    return region if region and letter else tail


def _probe_gcp(timeout: float) -> str | None:
    """Probe the GCP metadata server for the caller region."""
    zone = _metadata_request(
        _GCP_ZONE_URL, headers={"Metadata-Flavor": "Google"}, timeout=timeout
    )
    return _normalize_gcp_zone(zone) if zone else None


def _probe_azure(timeout: float) -> str | None:
    """Probe Azure IMDS for the caller region (a `location` string)."""
    return _metadata_request(
        _AZURE_LOCATION_URL, headers={"Metadata": "true"}, timeout=timeout
    )


def _detect_caller_region(*, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Detect the caller region from instance metadata, cached per process.

    Tries AWS IMDSv2, then GCP, then Azure — each behind the hard
    `timeout` and each fail-safe to `None`. The first non-empty result
    wins. The outcome (including `None`) is cached for the process so a
    cold, off-cloud caller pays the probe cost at most once; reset with
    :func:`clear_region_cache`.

    Args:
        timeout: Hard per-probe timeout in seconds.

    Returns:
        The detected region, or `None` when no cloud metadata answers.
    """
    global _cached_region
    if _cached_region is not _UNSET:
        return _cached_region  # type: ignore[return-value]
    region = _probe_aws(timeout) or _probe_gcp(timeout) or _probe_azure(timeout) or None
    _cached_region = region
    return region


def region_affinity(
    bucket_region: str | None,
    *,
    caller_region: str | None = None,
    probe: bool = True,
) -> str:
    """Classify the caller's region relative to a target bucket.

    Resolves the caller region in order — explicit `caller_region`, then
    `AWS_REGION` / `AWS_DEFAULT_REGION`, then (when `probe`) an
    instance-metadata probe — and compares it to `bucket_region`. The
    result guides a backend's choice between an in-region streaming read
    and a cross-region egress download.

    Args:
        bucket_region: The region the target bucket lives in (e.g.
            `"us-west-2"`). `None` or empty yields `"unknown"`.
        caller_region: An explicit override for the caller's region. When
            given, the environment and probe are not consulted.
        probe: Whether to fall back to the instance-metadata probe when
            the environment carries no region. `False` skips all network
            access (tests, air-gapped runs, and the earthdata re-point).

    Returns:
        `"in-region"` when the caller region equals `bucket_region`,
        `"egress"` when both are known and differ, or `"unknown"` when
        the caller region cannot be determined (or `bucket_region` is
        empty).

    Examples:
        - An explicit caller region drives the verdict without touching
          the environment:
            ```python
            >>> from earthlens.base.region import region_affinity
            >>> region_affinity("us-west-2", caller_region="us-west-2")
            'in-region'
            >>> region_affinity("us-west-2", caller_region="eu-central-1")
            'egress'
            >>> region_affinity("", caller_region="us-west-2")
            'unknown'

            ```
    """
    if not bucket_region:
        return UNKNOWN
    caller = caller_region or _caller_region_from_env()
    if caller is None and probe:
        caller = _detect_caller_region()
    if caller is None:
        return UNKNOWN
    return IN_REGION if caller == bucket_region else EGRESS


def warn_if_egress(
    bucket_region: str | None,
    *,
    size_bytes: int,
    threshold_bytes: int = DEFAULT_EGRESS_THRESHOLD_BYTES,
    caller_region: str | None = None,
    probe: bool = True,
) -> str:
    """Warn once before a large cross-region pull, returning the hint.

    Computes the region affinity and, when the caller is in a different
    region than the bucket (`"egress"`) and the transfer exceeds
    `threshold_bytes`, emits a single `loguru` warning so the user learns
    about the cross-region cost and latency up front. The hint is
    returned so a caller can branch on it too.

    Args:
        bucket_region: The region the target bucket lives in.
        size_bytes: The size of the pending transfer in bytes.
        threshold_bytes: The size above which the warning fires (default
            1 GiB). A parameter, never hard-coded at the call site.
        caller_region: An explicit override for the caller's region.
        probe: Whether to allow the instance-metadata probe (see
            :func:`region_affinity`).

    Returns:
        The affinity hint — `"in-region"`, `"egress"`, or `"unknown"`.
    """
    hint = region_affinity(bucket_region, caller_region=caller_region, probe=probe)
    if hint == EGRESS and size_bytes > threshold_bytes:
        logger.warning(
            f"Cross-region egress: pulling {size_bytes / (1 << 30):.2f} GiB "
            f"from {bucket_region!r} while running elsewhere — expect slower, "
            f"billed transfer. Run in {bucket_region!r} to stream in-region."
        )
    return hint
