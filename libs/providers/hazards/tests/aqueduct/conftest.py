"""Fixtures for the Aqueduct backend tests: canned shapefile zips (no network)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

from earthlens.aqueduct import Catalog

# Column grammar mirrored from the real product so a canned shapefile can carry
# every selectable column; values are deterministic, not real exposure figures.
_INDICATORS = ("G", "P", "U")
_YEAR_SCENARIOS = {"10": ("bh",), "30": ("24", "28", "38", "b4", "b8", "2h", "3h")}
_RETURN_PERIODS = ("2", "5", "10", "25", "50", "100", "250", "500", "1T")


def _all_columns() -> list[str]:
    """Return every `{indicator}{year}_{scenario}_{rp}` column name."""
    cols: list[str] = []
    for indicator in _INDICATORS:
        for year, scenarios in _YEAR_SCENARIOS.items():
            for scenario in scenarios:
                for rp in _RETURN_PERIODS:
                    cols.append(f"{indicator}{year}_{scenario}_{rp}")
    return cols


def _synthetic_gdf() -> gpd.GeoDataFrame:
    """Build a three-unit GeoDataFrame carrying the full column grid.

    Two units sit inside a small equatorial box (so a bbox filter keeps them)
    and share nothing in name; the third is far away for the bbox negative case.
    """
    units = [
        (1.0, "ALPHA", box(0, 0, 1, 1)),
        (2.0, "BETA", box(2, 2, 3, 3)),
        (3.0, "GAMMA", box(40, 40, 41, 41)),
    ]
    data: dict[str, list] = {
        "unit_id": [u[0] for u in units],
        "unit_name": [u[1] for u in units],
    }
    for offset, column in enumerate(_all_columns()):
        data[column] = [float(offset * 10 + i) for i in range(len(units))]
    return gpd.GeoDataFrame(data, geometry=[u[2] for u in units], crs="EPSG:4326")


def _write_shapefile_zip(stem: str, zip_path: Path) -> None:
    """Write the synthetic shapefile under `stem` and zip it to `zip_path`."""
    work = zip_path.parent / f"_build_{stem}"
    work.mkdir(parents=True, exist_ok=True)
    shp = work / f"{stem}.shp"
    _synthetic_gdf().to_file(str(shp))
    with zipfile.ZipFile(zip_path, "w") as archive:
        for member in work.glob(f"{stem}.*"):
            archive.write(member, arcname=member.name)


@pytest.fixture(scope="session")
def country_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A cache dir holding the canned direct-download country zip."""
    cache = tmp_path_factory.mktemp("aqueduct_country_cache")
    row = Catalog().get("country")
    _write_shapefile_zip(row.shapefile_stem, cache / row.zip)
    return cache


@pytest.fixture(scope="session")
def state_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A cache dir holding the canned nested state bundle (zip within a zip)."""
    cache = tmp_path_factory.mktemp("aqueduct_state_cache")
    row = Catalog().get("state")
    build = cache / "_build_state"
    build.mkdir(parents=True, exist_ok=True)
    inner_zip = build / row.zip
    _write_shapefile_zip(row.shapefile_stem, inner_zip)
    with zipfile.ZipFile(cache / row.container_zip, "w") as bundle:
        bundle.write(inner_zip, arcname=row.zip)
    return cache
