"""Catalog-tooling handlers for the AWS Open-Data S3 backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). The S3 registry has no live
"list all" endpoint — its universe *is* the curated registry — so the refresher
returns the curated names; the probe / live validator list object keys unsigned.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import index_writer, lint, require

#: Persist a live fetch back into the bundled `available_datasets` index.
writer = index_writer("available_datasets")


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List the S3 registry's dataset names (its `available_datasets` universe).

    The AWS Open-Data S3 backend has no single live "list all" endpoint — its
    universe *is* the curated registry — so the refresher returns the curated
    dataset names. `--write` then regenerates the in-file `available_datasets:`
    block from them (the `tools/s3/refresh_s3_catalog.py:refresh` step).

    Args:
        catalog: The loaded S3 `Catalog`.

    Returns:
        A single-group mapping `{"s3": [sorted registered dataset names]}`.
    """
    return {"s3": sorted(str(key) for key in catalog.datasets)}


def _s3_sample_keys(bucket: str, prefix: str, region: str | None) -> list[str]:
    """Return up to five object keys under `prefix` (unsigned `boto3`)."""
    from earthlens.base.s3 import S3Auth, S3Credentials

    client = S3Auth(S3Credentials(region=region)).client()
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=5)
    return [item["Key"] for item in response.get("Contents", [])]


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an AWS Open-Data dataset's bucket layout (unsigned `boto3`).

    Lists a few object keys under the dataset's bucket — the seed for
    confirming a dataset's on-disk key layout.

    Args:
        catalog: The loaded S3 `Catalog` (resolves a key's bucket/prefix).
        dataset: A registered dataset name.

    Returns:
        Mapping of object key to `{}`.

    Raises:
        ValueError: If `dataset` is not a registered S3 dataset.
    """
    record = catalog.datasets.get(dataset)
    if record is None:
        raise ValueError(f"unknown S3 dataset {dataset!r}")
    keys = _s3_sample_keys(
        record.bucket,
        getattr(record, "prefix", "") or "",
        getattr(record, "region", None),
    )
    return {key: {} for key in keys}


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each S3 dataset needs a bucket and a format."""
    return lint(catalog, lambda k, r: require(k, r, ("bucket", "format")))


def _s3_live_keys(bucket: str, prefix: str, region: str | None) -> list[str]:
    """Return one object key under `prefix` (unsigned `boto3`)."""
    from earthlens.base.s3 import S3Auth, S3Credentials

    client = S3Auth(S3Credentials(region=region)).client()
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return [item["Key"] for item in response.get("Contents", [])]


def live_validator(catalog: Any) -> tuple[int, list[str]]:
    """Confirm every S3 dataset's bucket still serves an object (unsigned)."""
    issues: list[str] = []
    for key, record in catalog.datasets.items():
        try:
            keys = _s3_live_keys(
                record.bucket,
                getattr(record, "prefix", "") or "",
                getattr(record, "region", None),
            )
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}: bucket error ({exc})")
            continue
        if not keys:
            issues.append(f"{key}: no objects under s3://{record.bucket}")
    return len(catalog.datasets), issues
