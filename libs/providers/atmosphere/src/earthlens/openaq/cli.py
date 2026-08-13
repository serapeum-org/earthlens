"""Catalog-tooling handlers for the OpenAQ backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). The refresher lists the live
OpenAQ parameter names (needs `OPENAQ_API_KEY`); `--write` persists the full live
list to a sibling `available_parameters.yaml` the runtime does not load.
"""

from __future__ import annotations

import os
from typing import Any

from earthlens.cli.toolkit import (
    BackendInfo,
    flatten,
    get_json,
    write_sibling_index,
)

#: OpenAQ parameters endpoint (needs an `OPENAQ_API_KEY` header).
_OPENAQ_PARAMETERS_URL = "https://api.openaq.org/v3/parameters"


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List the OpenAQ parameter names, live (needs `OPENAQ_API_KEY`).

    The key is read from the environment; without it the request fails and
    `refresh_one` reports an `"error"` outcome.

    Args:
        catalog: The loaded OpenAQ `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"openaq": [sorted parameter names]}`.
    """
    key = os.environ.get("OPENAQ_API_KEY", "")
    body = get_json(
        _OPENAQ_PARAMETERS_URL,
        headers={"X-API-Key": key} if key else None,
        params={"limit": 1000},
    )
    names = sorted(
        {str(row["name"]) for row in body.get("results", []) if row.get("name")}
    )
    return {"openaq": names}


def writer(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite OpenAQ's sibling `available_parameters.yaml` (full live list)."""
    return write_sibling_index(
        info, "available_parameters.yaml", {"available_parameters": flatten(grouped)}
    )
