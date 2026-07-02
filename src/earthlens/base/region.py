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

import os

#: The process runs in the same region as the target bucket.
IN_REGION = "in-region"

#: The process and the target bucket are in different (known) regions.
EGRESS = "egress"

#: The caller region could not be determined.
UNKNOWN = "unknown"


def _caller_region_from_env() -> str | None:
    """Return the caller's AWS region from the environment, if set.

    Reads `AWS_REGION` first, then `AWS_DEFAULT_REGION`. This is the
    zero-cost signal that covers most CI, AWS Lambda, and
    EC2-with-env-config callers.

    Returns:
        The region string, or `None` when neither variable is set.
    """
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None


def _detect_caller_region() -> str | None:
    """Detect the caller region from instance metadata (env-only in `C1`).

    The metadata-probe fallback (AWS IMDSv2 / GCP / Azure) lands in `C2`;
    the `C1` env-only cut returns `None` so callers relying purely on the
    environment resolve deterministically.

    Returns:
        The detected region, or `None` when it cannot be determined.
    """
    return None


def region_affinity(
    bucket_region: str,
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
            `"us-west-2"`).
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
