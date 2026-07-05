"""Stateless S3 helpers for the GOES ABI backend.

Thin utilities over the unsigned `boto3` client the `noaa-goes*` public
buckets need: build the client (lazy import, so the package imports
without the `[s3]` extra), list every object key under an
`<Product>/<YYYY>/<DDD>/<HH>/` prefix, parse a granule's `_s<scan-start>`
timestamp out of its key, and stream one key to disk atomically. The
client is passed in explicitly (never held as module state) so tests
inject a fake `boto3` client and no test ever touches the network.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path
from typing import Any

#: AWS region the `noaa-goes*` Open-Data buckets live in.
BUCKET_REGION = "us-east-1"

#: Matches the `_s<YYYYDDDHHMMSSt>_` scan-start token in an ABI key, where
#: the final digit is tenths of a second (14 digits total).
_SCAN_START = re.compile(r"_s(\d{14})_")


def unsigned_s3_client(region: str = BUCKET_REGION) -> Any:
    """Build an anonymous (unsigned) `boto3` S3 client for public buckets.

    Imports `boto3` / `botocore` lazily so `import earthlens.goes` works
    without the `[s3]` extra installed.

    Args:
        region: AWS region to build the client in. Defaults to
            :data:`BUCKET_REGION`.

    Returns:
        An unsigned `boto3` S3 client.

    Raises:
        ImportError: When `boto3` is not installed. The message names
            `earthlens[s3]`.
    """
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "The GOES backend requires the optional 'boto3' dependency. "
            "Install it with: pip install earthlens[s3]"
        ) from exc
    return boto3.client(
        "s3", region_name=region, config=Config(signature_version=UNSIGNED)
    )


def list_prefix_keys(client: Any, bucket: str, prefix: str) -> list[str]:
    """List every object key under `prefix` (following pagination).

    Args:
        client: An S3 client with a `list_objects_v2` method.
        bucket: The bucket to list.
        prefix: The `<Product>/<YYYY>/<DDD>/<HH>/` key prefix.

    Returns:
        list[str]: Every object key under `prefix`, in listing order.
    """
    keys: list[str] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            params["ContinuationToken"] = token
        response = client.list_objects_v2(**params)
        keys.extend(item["Key"] for item in response.get("Contents", []))
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
        if token is None:
            break
    return keys


def parse_scan_start(key: str) -> dt.datetime | None:
    """Parse the `_s<scan-start>_` timestamp from an ABI granule key.

    The token is `%Y%j%H%M%S` plus one tenths-of-a-second digit, e.g.
    `s20261841201180` → 2026-07-03 12:01:18.0 UTC (naive).

    Args:
        key: A granule key or basename.

    Returns:
        datetime.datetime | None: The naive-UTC scan-start, or `None`
            when the key carries no parseable scan-start token.

    Examples:
        - Parse a real granule key:
            ```python
            >>> from earthlens.goes._helpers import parse_scan_start
            >>> parse_scan_start(
            ...     "OR_ABI-L2-MCMIPC-M6_G19_s20261841201180_e..._c....nc"
            ... )
            datetime.datetime(2026, 7, 3, 12, 1, 18)

            ```
    """
    match = _SCAN_START.search(key)
    if match is None:
        return None
    token = match.group(1)
    stamp = dt.datetime.strptime(token[:13], "%Y%j%H%M%S")
    return stamp + dt.timedelta(milliseconds=int(token[13]) * 100)


def download_key(client: Any, bucket: str, key: str, dest: Path) -> Path:
    """Stream one S3 object to `dest`, writing atomically via a `.part` file.

    The body is streamed to disk in chunks (a Full-Disk granule can be
    hundreds of MB) and the temporary `.part` file is renamed onto `dest`
    only after a complete transfer, so a killed download never leaves a
    truncated file at the final path.

    Args:
        client: An S3 client with a `get_object` method.
        bucket: The source bucket.
        key: The object key to download.
        dest: The destination path.

    Returns:
        Path: `dest`.
    """
    tmp = dest.with_name(dest.name + ".part")
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        with open(tmp, "wb") as handle:
            shutil.copyfileobj(body, handle)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest
