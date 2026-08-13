"""Catalog-tooling handlers for the GHSL backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`). GHSL has no per-dataset sample
endpoint — availability is the curated `releases` matrix — so `prober` and
`validator` are offline; `tile_regen` rebuilds the bundled Mollweide tile index
from the JRC shapefile, and `live_validator` HEADs one whole-globe artefact per
product/release.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from earthlens.cli.toolkit import http_head, lint, require

#: JRC 54009 land tile-schema shapefile (the GHSL Mollweide tile grid source).
_TILE_SCHEMA_ZIP = (
    "https://ghsl.jrc.ec.europa.eu/download/GHSL_data_54009_shapefile.zip"
)


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Report a GHSL product's curated (epoch, resolution) matrix (offline).

    Args:
        catalog: The loaded GHSL `Catalog`.
        dataset: A curated product code / alias.

    Returns:
        Mapping of `"{epoch}@{resolution}"` to `{release, crs}`.

    Raises:
        ValueError: If `dataset` is not a curated GHSL product.
    """
    record = catalog.datasets.get(dataset)
    if record is None:
        raise ValueError(f"unknown GHSL product {dataset!r}")
    schema: dict[str, dict[str, Any]] = {}
    for release, blocks in (getattr(record, "releases", None) or {}).items():
        for block in blocks:
            crs = ", ".join(sorted(block.source_crs()))
            for epoch in block.epochs:
                for resolution in block.resolutions:
                    schema[f"{epoch}@{resolution}"] = {"release": release, "crs": crs}
    return schema


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each GHSL product needs a code and at least one release.

    Args:
        catalog: The loaded GHSL `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    return lint(catalog, lambda k, r: require(k, r, ("code", "releases")))


def _tile_frame() -> Any:
    """Download the JRC tile shapefile and return its `(tile_id, bounds)` frame."""
    import io
    import tempfile
    import zipfile

    import geopandas as gpd

    response = requests.get(_TILE_SCHEMA_ZIP, timeout=120)
    response.raise_for_status()
    with tempfile.TemporaryDirectory() as workdir:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            archive.extractall(workdir)  # nosec B202 — trusted JRC zip
        shapefile = next(Path(workdir).glob("*tile_schema_land*.shp"))
        frame = gpd.read_file(shapefile)[
            ["tile_id", "left", "top", "right", "bottom", "geometry"]
        ]
    for column in ("left", "top", "right", "bottom"):
        frame[column] = frame[column].astype(int)
    return frame


def tile_regen() -> tuple[str, int]:
    """Regenerate GHSL's bundled `tile_schema.geojson` from the JRC shapefile.

    The GIS analogue of an `available_*` refresh: rewrites the 18x36 Mollweide
    tile index the GHSL backend reads to map a bbox to its covering tiles.

    Returns:
        `(written_path, tile_count)`.
    """
    from earthlens.ghsl._helpers import TILE_SCHEMA_PATH

    frame = _tile_frame()
    frame.to_file(TILE_SCHEMA_PATH, driver="GeoJSON")
    return str(TILE_SCHEMA_PATH), len(frame)


def live_validator(catalog: Any) -> tuple[int, list[str]]:
    """HEAD one whole-globe artefact per GHSL product/release.

    Skips releases whose every resolution ships only as tiles (real-tile
    sampling stays in `tools/ghsl/refresh_ghsl_catalog.py`).

    Args:
        catalog: The loaded GHSL `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    from earthlens.ghsl._helpers import RES_TO_TOKEN, ghsl_url

    issues: list[str] = []
    for code, product in catalog.datasets.items():
        if getattr(product, "kind", "raster") == "tabular":
            continue
        for release, blocks in (getattr(product, "releases", None) or {}).items():
            block = blocks[0]
            whole_globe = [
                r
                for r in block.resolutions
                if r not in block.tiled() and r in RES_TO_TOKEN
            ]
            if not whole_globe:
                continue
            try:
                url = ghsl_url(
                    product.family or code,
                    code,
                    block.epochs[0],
                    release,
                    whole_globe[0],
                    version=block.version,
                    region=block.region,
                    nested=block.nested,
                )
                status = http_head(url)
            except Exception as exc:  # noqa: BLE001 — reported as drift
                issues.append(f"{code} ({release}): {exc}")
                continue
            if status != 200:
                issues.append(f"{code} ({release}): HTTP {status} for {url}")
    return len(catalog.datasets), issues
