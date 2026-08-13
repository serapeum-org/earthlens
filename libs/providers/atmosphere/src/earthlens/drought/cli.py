"""Catalog-tooling handlers for the drought backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). Offline structural lint only.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require

#: Drought transports whose output is a raster (vs USDM's vector polygons).
_DROUGHT_RASTER_TRANSPORTS = frozenset({"edo-wcs", "netcdf-url"})


def _check_drought_row(key: str, record: Any) -> list[str]:
    """Flag a drought row missing a core field or with a transport mismatch."""
    issues = require(
        key, record, ("source", "endpoint", "output_kind", "cadence", "native_crs")
    )
    transport = getattr(record, "transport", None)
    output_kind = getattr(record, "output_kind", None)
    if transport == "usdm-geojson" and output_kind != "vector":
        issues.append(f"{key}: usdm-geojson transport must be output_kind=vector")
    if transport in _DROUGHT_RASTER_TRANSPORTS and output_kind != "raster":
        issues.append(f"{key}: {transport} transport must be output_kind=raster")
    if transport == "edo-wcs":
        issues.extend(require(key, record, ("coverage", "timescale")))
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each drought row needs its core fields; edo-wcs rows a coverage + timescale."""
    return lint(catalog, _check_drought_row)
