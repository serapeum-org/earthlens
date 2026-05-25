"""Shared, network-free helpers for the Earthdata catalog tooling.

The three CLIs (`refresh_earthdata_catalog.py`,
`audit_earthdata_datasets.py`, `probe_earthdata_granule.py`) all need
to (a) locate the bundled catalog / providers files, (b) read a CMR
provider list, and (c) infer a dataset row's `output_kind` / `format`
from collection or granule metadata. Those pure functions live here so
they can be unit-tested without touching `earthaccess` or the network.

Not part of the installed package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# tools/earthdata/_earthdata_cmr.py -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
EARTHDATA_PKG = REPO_ROOT / "src" / "earthlens" / "earthdata"
CATALOG_DIR = EARTHDATA_PKG / "catalog"
PROVIDERS_PATH = EARTHDATA_PKG / "providers.yaml"

# Granule format hints -> a coarse format label used in the catalog row.
_FORMAT_BY_EXT = {
    ".nc": "netcdf4",
    ".nc4": "netcdf4",
    ".h5": "hdf5",
    ".he5": "hdf-eos5",
    ".hdf": "hdf-eos2",
    ".tif": "cog",
    ".tiff": "cog",
    ".csv": "csv",
    ".json": "geojson",
    ".geojson": "geojson",
    ".gpkg": "geopackage",
    ".zip": "zip",
}

# Substrings in a short_name / collection title that imply a point or
# profile (vector) product rather than a gridded raster.
_VECTOR_HINTS = (
    "GEDI",  # footprint lidar
    "ATL0",  # ICESat-2 ATL03/06/08 photon / land products
    "ATL1",
    "GLAH",  # ICESat GLAS
)

# Substrings that imply a plain tabular product.
_TABULAR_HINTS = ("CSV", "_TABLE", "FLUXNET")


def read_providers() -> dict[str, dict[str, Any]]:
    """Return the CMR provider registry as a plain dict keyed by code.

    Returns:
        dict[str, dict]: Map of CMR provider code to its `providers.yaml`
            body (daac / landing_page / cloud_region / …).

    Examples:
        - The bundled registry lists the nine user-relevant DAACs:
            ```python
            >>> from tools.earthdata._earthdata_cmr import read_providers
            >>> sorted(read_providers())[:3]
            ['ASF', 'GES_DISC', 'LAADS']

            ```
    """
    data = yaml.safe_load(PROVIDERS_PATH.read_text(encoding="utf-8")) or {}
    return dict(data.get("daacs") or {})


def format_from_extension(filename: str) -> str:
    """Infer a coarse catalog `format` label from a granule filename.

    Args:
        filename: A granule file name or URL.

    Returns:
        str: A format label (`"netcdf4"`, `"hdf5"`, `"cog"`, …) or `""`
            when the extension is unrecognised.

    Examples:
        - A NetCDF-4 granule:
            ```python
            >>> from tools.earthdata._earthdata_cmr import format_from_extension
            >>> format_from_extension("3B-HHR-L.MS.MRG.3IMERG.20200601.nc4")
            'netcdf4'

            ```
        - An OPERA COG:
            ```python
            >>> from tools.earthdata._earthdata_cmr import format_from_extension
            >>> format_from_extension("OPERA_L2_RTC-S1_VV.tif")
            'cog'

            ```
    """
    suffix = Path(filename.split("?", 1)[0]).suffix.lower()
    return _FORMAT_BY_EXT.get(suffix, "")


def infer_output_kind(short_name: str, fmt: str = "", title: str = "") -> str:
    """Infer a dataset row's `output_kind` from its name / format / title.

    The heuristic favours `raster` (the majority of Earthdata holdings);
    point / profile products (GEDI, ICESat-2 ATL0x) map to `vector`, and
    plain table products to `tabular`. The result is a *seed* — the
    maintainer is expected to vet it.

    Args:
        short_name: CMR collection short name.
        fmt: Coarse format label (e.g. from
            :func:`format_from_extension`).
        title: Collection title, if available.

    Returns:
        str: One of `"raster"`, `"vector"`, `"tabular"`.

    Examples:
        - GPM IMERG is gridded raster:
            ```python
            >>> from tools.earthdata._earthdata_cmr import infer_output_kind
            >>> infer_output_kind("GPM_3IMERGHHL", "hdf5")
            'raster'

            ```
        - GEDI L4A footprints are vector:
            ```python
            >>> from tools.earthdata._earthdata_cmr import infer_output_kind
            >>> infer_output_kind("GEDI04_A", "hdf5")
            'vector'

            ```
        - A FLUXNET CSV table is tabular:
            ```python
            >>> from tools.earthdata._earthdata_cmr import infer_output_kind
            >>> infer_output_kind("FLUXNET_CH4", "csv", title="FLUXNET tower fluxes")
            'tabular'

            ```
    """
    haystack = f"{short_name} {title}".upper()
    if fmt in {"csv", "geojson", "geopackage"} or any(
        hint in haystack for hint in _TABULAR_HINTS
    ):
        if fmt in {"geojson", "geopackage"}:
            return "vector"
        return "tabular"
    if any(hint in short_name.upper() for hint in _VECTOR_HINTS):
        return "vector"
    return "raster"
