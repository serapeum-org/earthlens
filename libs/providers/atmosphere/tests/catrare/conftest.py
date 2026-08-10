"""Fixtures for the CatRaRE backend tests: a canned FileGDB zip (no network)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from earthlens.catrare import Catalog

#: The event attribute columns the real CatRaRE FileGDB carries.
_EVENT_COLUMNS = Catalog().event_columns

# Three events in DWD RADOLAN metres (the file carries no CRS): two inside the
# German extent (kept by a Germany bbox / reprojected to ~lon 6-14, lat 47-54)
# and dated in July 2021; one dated in 2005 for the date-filter negative case.
_EVENTS = [
    dict(Event_ID=1, Date_START="2021-07-14 09:50:00", Date_END="2021-07-14 12:50:00",
         xy=(0.0, -4400000.0)),
    dict(Event_ID=2, Date_START="2021-07-15 00:50:00", Date_END="2021-07-15 06:50:00",
         xy=(100000.0, -4300000.0)),
    dict(Event_ID=3, Date_START="2005-08-01 00:50:00", Date_END="2005-08-01 06:50:00",
         xy=(-200000.0, -4600000.0)),
]


def _rows(geometry_factory) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame carrying every event column, no CRS (RADOLAN)."""
    data: dict[str, list] = {col: [] for col in _EVENT_COLUMNS}
    geoms = []
    for i, event in enumerate(_EVENTS):
        x, y = event["xy"]
        for col in _EVENT_COLUMNS:
            if col in event:
                data[col].append(event[col])
            elif col in ("Start_Time", "End_Time"):
                data[col].append(event["Date_START"])
            elif col in ("Country_RRmax", "BDL_RRmax"):
                data[col].append("DE")
            else:
                data[col].append(float(i + 1))
        geoms.append(geometry_factory(x, y))
    return gpd.GeoDataFrame(data, geometry=geoms, crs=None)


def _write_gdb_zip(threshold: str, zip_path: Path) -> None:
    """Write a two-layer FileGDB (zones + points) and zip it like the real one."""
    cat = Catalog()
    gdb = zip_path.parent / f"_build_{threshold}.gdb"
    zones = _rows(lambda x, y: box(x, y, x + 5000, y + 5000))
    points = _rows(lambda x, y: Point(x, y))
    zones.to_file(gdb, driver="OpenFileGDB", layer=cat.layer_name(threshold, "zones"))
    points.to_file(gdb, driver="OpenFileGDB", layer=cat.layer_name(threshold, "points"))
    with zipfile.ZipFile(zip_path, "w") as archive:
        for member in gdb.rglob("*"):
            if member.is_file():
                archive.write(member, arcname=f"{gdb.name}/{member.relative_to(gdb)}")


@pytest.fixture(scope="session")
def catrare_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A cache dir pre-seeded with the canned T5 FileGDB zip (no download)."""
    cache = tmp_path_factory.mktemp("catrare_cache")
    _write_gdb_zip("t5", cache / "catrare_t5.gdb.zip")
    return cache


@pytest.fixture
def write_canned_gdb():
    """Return a callable that writes the canned T5 FileGDB zip to a given path."""

    def _write(target: Path) -> None:
        _write_gdb_zip("t5", Path(target))

    return _write
