"""Catalog-tooling handlers for the FIRMS backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`). Both the refresh listing and
the probe sample carry the `FIRMS_MAP_KEY` in the request URL path, so a failed
request is re-raised with the key masked — it must never reach a surfaced
`detail` / `--json` output / CI log.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from earthlens.cli.toolkit import (
    HTTP_TIMEOUT,
    get_text,
    infer_dtype,
    lint,
    redact,
    require,
)

_DATA_AVAIL_URL = (
    "https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{map_key}/all"
)

#: data_availability also lists burned-area products that are not area-CSV
#: active-fire sources; they belong to the GEE backend, not the catalog.
_EXCLUDED = frozenset({"BA_MODIS", "BA_VIIRS"})


def refresher(_catalog: Any) -> dict[str, list[str]]:
    """List every live FIRMS sensor id from the data_availability endpoint.

    Args:
        _catalog: The loaded FIRMS `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"firms": [sorted sensor ids]}`.

    Raises:
        RuntimeError: If the data_availability fetch fails; the message has
            the `FIRMS_MAP_KEY` redacted.
    """
    key = os.environ.get("FIRMS_MAP_KEY", "")
    try:
        text = get_text(_DATA_AVAIL_URL.format(map_key=key))
    except Exception as exc:  # noqa: BLE001 — scrub the key from the URL in the error
        raise RuntimeError(redact(str(exc), key)) from None
    rows = text.splitlines()
    if not rows or not rows[0].lower().startswith("data_id"):
        raise RuntimeError(f"data_availability returned a non-CSV body: {text[:120]}")
    ids = {
        code
        for row in rows[1:]
        if (code := row.split(",", 1)[0].strip()) and code not in _EXCLUDED
    }
    return {"firms": sorted(ids)}


def _csv_lines(code: str) -> list[str]:
    """Return a tiny FIRMS area-CSV sample's lines (needs `FIRMS_MAP_KEY`).

    Args:
        code: The FIRMS sensor code (e.g. `VIIRS_SNPP_NRT`).

    Returns:
        The sampled CSV body split into lines.

    Raises:
        RuntimeError: If the request fails; the message has the
            `FIRMS_MAP_KEY` redacted.
    """
    key = os.environ.get("FIRMS_MAP_KEY", "")
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{code}/world/1"
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(redact(str(exc), key)) from None
    return response.text.splitlines()


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a FIRMS sensor's live CSV column schema (needs `FIRMS_MAP_KEY`).

    Args:
        catalog: The loaded FIRMS `Catalog` (resolves a key's sensor `code`).
        dataset: A curated key or a FIRMS sensor code.

    Returns:
        Mapping of column name to `{dtype}`.
    """
    record = catalog.datasets.get(dataset)
    code = getattr(record, "code", None) or dataset
    lines = _csv_lines(code)
    if not lines:
        return {}
    header = lines[0].split(",")
    first_row = lines[1].split(",") if len(lines) > 1 else []
    schema: dict[str, dict[str, Any]] = {}
    for index, column in enumerate(header):
        value = first_row[index] if index < len(first_row) else None
        schema[column.strip()] = {"dtype": infer_dtype(value)}
    return schema


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each FIRMS sensor needs a code and a non-empty columns map.

    Args:
        catalog: The loaded FIRMS `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(catalog, lambda k, r: require(k, r, ("code", "columns")))
