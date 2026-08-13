"""Catalog-tooling handlers for the USGS Water backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._ocean_cli`). The live reads use the public
`dataretrieval` SDK; `--write` persists the full live parameter table to a
sibling `available_parameters.yaml` (the catalog's `available_*` attribute is
computed from the curated rows at load time, so there is no in-file block).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import (
    BackendInfo,
    curated_attr_ids,
    lint,
    write_sibling_index,
)

#: Curated-id resolver over each row's `code` (the `audit` drift axis).
curated_ids = curated_attr_ids("code")


def _parameter_codes() -> list[str]:
    """Return every USGS parameter code from the live reference table (SDK)."""
    from dataretrieval import waterdata

    result = waterdata.get_reference_table(collection="parameter-codes")
    frame = result[0] if isinstance(result, tuple) else result
    return [str(code) for code in frame["parameter_code"]]


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List every USGS parameter code, live (public `dataretrieval` SDK).

    Args:
        catalog: The loaded USGS Water `Catalog` (unused; the SDK is the source).

    Returns:
        A single-group mapping `{"usgs_water": [sorted parameter codes]}`.
    """
    return {"usgs_water": sorted(set(_parameter_codes()))}


def _parameter_rows() -> dict[str, dict[str, str]]:
    """Return the live USGS parameter table keyed by code (name/group/unit)."""
    from dataretrieval import waterdata

    result = waterdata.get_reference_table(collection="parameter-codes")
    frame = result[0] if isinstance(result, tuple) else result
    rows: dict[str, dict[str, str]] = {}
    for _, row in frame.iterrows():
        code = str(
            row.get("parameter_code") or row.get("parameterCode") or row.get("id") or ""
        ).strip()
        if not code:
            continue
        rows[code] = {
            "name": str(row.get("parameter_name") or row.get("name") or ""),
            "group": str(row.get("parameter_group_code") or row.get("group") or ""),
            "unit": str(row.get("unit_of_measure") or row.get("unit") or ""),
        }
    return dict(sorted(rows.items()))


def writer(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite USGS Water's sibling `available_parameters.yaml` (full table)."""
    return write_sibling_index(
        info,
        "available_parameters.yaml",
        {"available_parameters": _parameter_rows()},
    )


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each USGS Water parameter's `services` must be known service names.

    Args:
        catalog: The loaded USGS Water `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    from earthlens.usgs_water.backend import SERVICES

    def check(key: str, record: Any) -> list[str]:
        """Flag a parameter declaring a service that is not a known service name."""
        return [
            f"{key}: unknown service {service!r}"
            for service in getattr(record, "services", None) or []
            if service not in SERVICES
        ]

    return lint(catalog, check)


def emitter(catalog: Any, upstream_id: str, *, key: str, **opts: Any) -> dict[str, Any]:
    """Seed a USGS Water parameter row from a parameter code (no network).

    Args:
        catalog: The loaded USGS Water `Catalog` (unused).
        upstream_id: The 5-digit NWIS parameter code (e.g. `"00060"`).
        key: The friendly catalog key.
        **opts: `name`, `units`, `group`, `services`.

    Returns:
        The seeded row.
    """
    services = opts.get("services") or ["daily", "instantaneous"]
    return {
        "code": upstream_id,
        "name": str(opts.get("name") or key.replace("_", " ").title()),
        "units": str(opts.get("units") or ""),
        "group": str(opts.get("group") or "Physical"),
        "services": list(services),
    }
