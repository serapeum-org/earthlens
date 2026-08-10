"""Fixtures for the FLOPROS backend tests: a canned shapefile zip (no network)."""

from __future__ import annotations

import functools
import zipfile
from collections.abc import Callable
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

from earthlens.flopros import Catalog

#: The source `.dbf` columns the real FLOPROS shapefile carries.
_LAYER_COLUMNS = (
    "MerL_Riv",
    "ModL_Riv",
    "DL_Min_Riv",
    "DL_Max_Riv",
    "PL_Min_Riv",
    "PL_Max_Riv",
    "DL_Min_Co",
    "DL_Max_Co",
    "PL_Min_Co",
    "PL_Max_Co",
)


def _synthetic_gdf() -> gpd.GeoDataFrame:
    """Build a three-unit GeoDataFrame carrying the identity + layer columns.

    Two units sit inside a small equatorial box (so a bbox filter keeps them);
    the third is far away for the bbox negative case. Values are deterministic.
    """
    units = [
        ("Alphaland", "Alphaland", "Country", box(0, 0, 1, 1)),
        ("Beta Province", "Betaland", "Province", box(2, 2, 3, 3)),
        ("Gammaland", "Gammaland", "Country", box(40, 40, 41, 41)),
    ]
    data: dict[str, list] = {
        "name": [u[0] for u in units],
        "geonunit": [u[1] for u in units],
        "type_en": [u[2] for u in units],
    }
    for offset, column in enumerate(_LAYER_COLUMNS):
        data[column] = [float(offset * 10 + i) for i in range(len(units))]
    return gpd.GeoDataFrame(data, geometry=[u[3] for u in units], crs="EPSG:4326")


def _write_shapefile_zip(stem: str, zip_path: Path) -> None:
    """Write the synthetic shapefile under `stem` and zip it (nested like the real one)."""
    work = zip_path.parent / f"_build_{stem}"
    work.mkdir(parents=True, exist_ok=True)
    shp = work / f"{stem}.shp"
    _synthetic_gdf().to_file(str(shp))
    with zipfile.ZipFile(zip_path, "w") as archive:
        for member in work.glob(f"{stem}.*"):
            # Mirror the real supplement's nested member path.
            archive.write(member, arcname=f"Scussolini_etal/{stem}/{member.name}")


@pytest.fixture(scope="session")
def flopros_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A cache dir pre-seeded with the canned supplement zip (no download)."""
    cache = tmp_path_factory.mktemp("flopros_cache")
    stem = Catalog().get("flopros").shapefile_stem
    _write_shapefile_zip(stem, cache / "flopros_supplement.zip")
    return cache


@pytest.fixture
def write_canned_zip() -> Callable[[Path], None]:
    """Return a callable that writes the canned supplement zip to a given path."""
    stem = Catalog().get("flopros").shapefile_stem
    return functools.partial(_write_shapefile_zip, stem)
