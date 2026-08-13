"""Catalog-tooling handlers for the NWM backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._ocean_cli`). The live reads walk the public,
unsigned `noaa-nwm-pds` operational bucket; this module is the single home for
the NWM bucket primitives that both `refresh` and `validate --live` share.
"""

from __future__ import annotations

import re
from typing import Any, cast

#: AWS region and unsigned-config for the public `noaa-nwm-pds` bucket.
_REGION = "us-east-1"

#: Matches an ensemble-member directory suffix (`medium_range_mem3`).
_MEMBER_RE = re.compile(r"_mem\d+$")

#: Objects sampled per configuration directory for the live token check.
_SAMPLE_KEYS = 400


def _collapse_member(directory: str) -> str:
    """Collapse an NWM ensemble-member directory to its configuration key.

    The operational bucket publishes each ensemble member under its own
    `{config}_mem<N>` directory; the curated catalog keys an ensemble by its
    bare `{config}` name. Stripping the `_mem<N>` suffix maps a live directory
    back into the curated namespace so the diff lines up.

    Args:
        directory: A live configuration directory name.

    Returns:
        The base configuration key (unchanged when not a member directory).

    Examples:
        - An ensemble member collapses to its base configuration:

            ```python
            >>> from earthlens.nwm.cli import _collapse_member
            >>> _collapse_member("medium_range_mem3")
            'medium_range'

            ```
        - A non-ensemble directory is returned unchanged:

            ```python
            >>> from earthlens.nwm.cli import _collapse_member
            >>> _collapse_member("short_range")
            'short_range'

            ```
    """
    return _MEMBER_RE.sub("", directory)


def _unsigned_client() -> Any:
    """Return an unsigned `boto3` S3 client for the public `noaa-nwm-pds` bucket.

    Returns:
        An unsigned `boto3` S3 client.
    """
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config

    return boto3.client(
        "s3", region_name=_REGION, config=Config(signature_version=UNSIGNED)
    )


def _latest_complete_day(client: Any) -> str:
    """Return the most recent *complete* `nwm.YYYYMMDD/` day prefix.

    Lists the `nwm.YYYYMMDD/` date prefixes and returns the day before the
    latest (the newest prefix may be mid-publication), falling back to the only
    day when just one is present.

    Args:
        client: An unsigned S3 client (see `_unsigned_client`).

    Returns:
        The selected `nwm.YYYYMMDD` prefix (no trailing slash).

    Raises:
        RuntimeError: If the bucket exposes no `nwm.YYYYMMDD/` date prefix.
    """
    from earthlens.nwm import BUCKET

    paginator = client.get_paginator("list_objects_v2")
    days = sorted(
        prefix.rstrip("/")
        for page in paginator.paginate(Bucket=BUCKET, Delimiter="/")
        for entry in page.get("CommonPrefixes", [])
        if (prefix := entry["Prefix"]).startswith("nwm.")
    )
    if not days:
        raise RuntimeError(f"no nwm.YYYYMMDD/ prefixes found on {BUCKET}")
    return cast("str", days[-2] if len(days) > 1 else days[-1])


def _config_dirs(client: Any, day: str) -> list[str]:
    """Return the configuration directory names published under one NWM day.

    Args:
        client: An unsigned S3 client (see `_unsigned_client`).
        day: An `nwm.YYYYMMDD` prefix (see `_latest_complete_day`).

    Returns:
        The raw configuration directory names (ensemble members still carry
        their `_mem<N>` suffix), sorted.
    """
    from earthlens.nwm import BUCKET

    result = client.list_objects_v2(Bucket=BUCKET, Prefix=f"{day}/", Delimiter="/")
    return sorted(
        entry["Prefix"].split("/")[1] for entry in result.get("CommonPrefixes", [])
    )


def _live_config_dirs() -> list[str]:
    """Return the configuration directories under the latest complete NWM day.

    Returns:
        The raw configuration directory names (ensemble members still carry
        their `_mem<N>` suffix).

    Raises:
        RuntimeError: If the bucket exposes no `nwm.YYYYMMDD/` date prefix.
    """
    client = _unsigned_client()
    return _config_dirs(client, _latest_complete_day(client))


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List the live NWM configurations from the unsigned operational bucket.

    Walks the most recent complete `nwm.YYYYMMDD/` day on `noaa-nwm-pds` and
    collapses each ensemble-member directory to its base configuration key (see
    `_collapse_member`), so the live set is diffed against the catalog's
    `available_configurations:` index in the same namespace.

    Args:
        catalog: The loaded NWM `Catalog` (unused; the bucket is the source).

    Returns:
        A single-group mapping `{"nwm": [sorted configuration keys]}`.
    """
    dirs = {_collapse_member(name) for name in _live_config_dirs()}
    return {"nwm": sorted(dirs)}


def curated_ids(catalog: Any) -> list[str]:
    """Return the configuration keys the NWM catalog curates (its refresh axis)."""
    return sorted(catalog.configurations)


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated NWM products and configurations.

    Every product needs an `s3_token` and a non-empty `variables` map, and every
    configuration's `products` must reference a curated product key.

    Args:
        catalog: The loaded NWM `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per problem.
    """
    from earthlens.cli.toolkit import require

    products = catalog.datasets
    issues: list[str] = []
    for key, product in products.items():
        issues.extend(require(key, product, ("s3_token", "variables")))
    for cfg_key, config in catalog.configurations.items():
        for product_key in getattr(config, "products", None) or []:
            if product_key not in products:
                issues.append(f"{cfg_key}: references unknown product {product_key!r}")
    return len(products), issues


def _sample_tokens(client: Any, day: str, directory: str) -> set[str]:
    """Return the distinct product `{output}` tokens under one config directory.

    Args:
        client: An unsigned S3 client (see `_unsigned_client`).
        day: An `nwm.YYYYMMDD` prefix.
        directory: A configuration directory name under `day`.

    Returns:
        The set of product `{output}` tokens seen in the sample.
    """
    from earthlens.nwm import BUCKET

    sample = client.list_objects_v2(
        Bucket=BUCKET, Prefix=f"{day}/{directory}/", MaxKeys=_SAMPLE_KEYS
    )
    tokens: set[str] = set()
    for entry in sample.get("Contents", []):
        parts = entry["Key"].split("/")[-1].split(".")
        if len(parts) >= 6:
            tokens.add(parts[3])
    return tokens


def _token_present(s3_token: str, tokens: set[str]) -> bool:
    """Whether a product's bare `s3_token` shows among sampled file tokens.

    Matches the bare token or its ensemble form `{s3_token}_{member}`.

    Args:
        s3_token: The product's bare `{output}` token (e.g. `channel_rt`).
        tokens: The sampled file tokens for one configuration directory.

    Returns:
        `True` if the product's token (bare or member-suffixed) is present.

    Examples:
        - A deterministic carrier's bare token counts as present:
            ```python
            >>> from earthlens.nwm.cli import _token_present
            >>> _token_present("channel_rt", {"channel_rt", "land"})
            True

            ```
        - An ensemble carrier's `{token}_{member}` file token counts too:
            ```python
            >>> from earthlens.nwm.cli import _token_present
            >>> _token_present("channel_rt", {"channel_rt_1"})
            True

            ```
        - A token absent from the sample is not present:
            ```python
            >>> from earthlens.nwm.cli import _token_present
            >>> _token_present("channel_rt", {"land", "reservoir"})
            False

            ```
    """
    prefix = f"{s3_token}_"
    return any(token == s3_token or token.startswith(prefix) for token in tokens)


def _config_directory(config: Any, key: str) -> str:
    """Return the live bucket directory for an NWM configuration.

    A deterministic configuration is published under its bare `key` directory;
    an ensemble configuration (`members > 0`) publishes each member under
    `{key}_mem<N>`, so member 1 (`{key}_mem1`) is the directory sampled.

    Args:
        config: The configuration row (duck-typed: reads `members`).
        key: The configuration key (its bare directory name).

    Returns:
        The configuration's live directory name (`{key}_mem1` for an ensemble,
        `key` otherwise).

    Examples:
        - A deterministic configuration maps to its bare directory:
            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.nwm.cli import _config_directory
            >>> _config_directory(SimpleNamespace(members=0), "short_range")
            'short_range'

            ```
        - An ensemble configuration maps to its member-1 directory:
            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.nwm.cli import _config_directory
            >>> _config_directory(SimpleNamespace(members=6), "medium_range")
            'medium_range_mem1'

            ```
    """
    return f"{key}_mem1" if getattr(config, "members", 0) else key


def live_validator(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each NWM product's `s3_token` appears live under a carrier config.

    Every curated product's file token (`channel_rt`, `land`, ...) must show up
    among the live files of at least one configuration that publishes it.
    Carriers are tried deterministic-first and sampling stops at the first hit.

    Args:
        catalog: The loaded NWM `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per token not
        found live under any carrier configuration.
    """
    client = _unsigned_client()
    day = _latest_complete_day(client)
    issues: list[str] = []
    for key, product in catalog.datasets.items():
        carriers = sorted(
            (
                cfg_key
                for cfg_key, config in catalog.configurations.items()
                if key in (getattr(config, "products", None) or [])
            ),
            key=lambda k: catalog.configurations[k].members,
        )
        seen = False
        for cfg_key in carriers:
            directory = _config_directory(catalog.configurations[cfg_key], cfg_key)
            if _token_present(product.s3_token, _sample_tokens(client, day, directory)):
                seen = True
                break
        if not seen:
            issues.append(
                f"{key}: s3_token {product.s3_token!r} not found live under "
                "any carrier configuration"
            )
    return len(catalog.datasets), issues
