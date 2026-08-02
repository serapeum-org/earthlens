"""Synthetic fixtures for the EM-DAT backend tests.

Every fixture here is invented. The EM-DAT terms of use forbid reproducing or
distributing the database or a substantial part of it, so no real EM-DAT row is
vendored into this repository — only the real *column names*, which come from
the published documentation and are what the code actually keys on. The GDIS
fixtures likewise carry real field names with invented values, including the
`"extreme temperature "` trailing space that the shipped GeoPackage really has.

The tables are written as inline CSV text rather than tuples so the columns stay
readable side by side, and so the fixture data is not at the mercy of the
formatter's line wrapping.
"""

from __future__ import annotations

import zipfile
from io import StringIO
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

#: Invented EM-DAT events under the documented column names. The real archive
#: has 47 columns; these are the ones the backend reads. Row 5 deliberately has
#: no coordinates, to exercise the bbox path.
EVENTS_CSV = """\
DisNo.,Disaster Group,Disaster Type,ISO,Country,Latitude,Longitude,Start Year,Total Deaths,Total Affected
2009-0001-TST,Natural,Flood,TST,Testland,10.5,20.5,2009,12,300
1995-0002-TST,Natural,Flood,TST,Testland,11.5,21.5,1995,3,40
2009-0003-OTH,Natural,Storm,OTH,Otherland,50.0,60.0,2009,7,90
2009-0004-TST,Technological,Industrial accident,TST,Testland,10.0,20.0,2009,1,2
2020-0005-TST,Natural,Flood,TST,Testland,,,2020,5,60
"""

#: Invented GDIS centroid rows under the real CSV field names. This
#: distribution is the only one carrying `year`, `latitude` and `longitude`.
GDIS_CSV = """\
id,country,iso3,gwno,year,geo_id,geolocation,level,adm1,adm2,adm3,location,historical,hist_country,disastertype,disasterno,latitude,longitude
1,Testland,TST,1,2009,10,Alpha,1,A1,A2,A3,Alpha,0,NA,flood,2009-0001,10.5,20.5
2,Testland,TST,1,1995,11,Beta,1,B1,B2,B3,Beta,0,NA,flood,1995-0002,11.5,21.5
3,Otherland,OTH,2,2009,12,Gamma,1,C1,C2,C3,Gamma,0,NA,storm,2009-0003,50.0,60.0
4,Testland,TST,1,2009,13,Delta,1,D1,D2,D3,Delta,0,NA,extreme temperature,2009-0004,10.0,20.0
5,Testland,TST,1,2011,14,Epsilon,1,E1,E2,E3,Epsilon,0,NA,flood,2011-0005,,
"""

#: Invented GDIS footprint rows under the real GeoPackage field names — no
#: `year`, no coordinates. Row 3 quotes `"extreme temperature "` to preserve the
#: trailing space the shipped file really carries, and row 5 spells its hazard
#: `Flood` so the driver push-down's case-insensitivity is actually exercised —
#: with every value lower-case, `LIKE` and `=` are indistinguishable.
GDIS_GPKG_CSV = """\
id,country,iso3,gwno,geo_id,geolocation,level,adm1,adm2,adm3,location,historical,hist_country,disastertype,disasterno
1,Testland,TST,1,10,Alpha,1,A1,A2,A3,Alpha,0,NA,flood,2009-0001
2,Testland,TST,1,11,Beta,1,B1,B2,B3,Beta,0,NA,flood,1995-0002
3,Otherland,OTH,2,12,Gamma,1,C1,C2,C3,Gamma,0,NA,"extreme temperature ",2009-0004
5,Mixedland,MIX,3,15,Zeta,1,Z1,Z2,Z3,Zeta,0,NA,Flood,2013-0009
4,Testland,TST,1,13,Delta,1,D1,D2,D3,Delta,0,NA,storm,2009-0006
"""


def _read(csv_text: str) -> pd.DataFrame:
    """Parse inline CSV text, keeping every field as written."""
    return pd.read_csv(StringIO(csv_text), skipinitialspace=False)


@pytest.fixture
def events_frame() -> pd.DataFrame:
    """An invented EM-DAT event table with the documented column names."""
    return _read(EVENTS_CSV)


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """A directory for fixture source files, kept out of the backend's output.

    The backend treats a file already sitting in its output directory as
    previously fetched, so a fixture written straight into `tmp_path` would
    silently suppress the download it is meant to exercise.
    """
    path = tmp_path / "_source"
    path.mkdir()
    return path


@pytest.fixture
def events_workbook(source_dir: Path, events_frame: pd.DataFrame) -> Path:
    """The event table written as an xlsx, named like a real archive release."""
    path = source_dir / "990101_emdat_archive.xlsx"
    events_frame.to_excel(path, sheet_name="EM-DAT Data", index=False)
    return path


@pytest.fixture
def gdis_csv_frame() -> pd.DataFrame:
    """An invented GDIS centroid table with the real field names."""
    return _read(GDIS_CSV)


@pytest.fixture
def gdis_csv_zip(source_dir: Path, gdis_csv_frame: pd.DataFrame) -> Path:
    """The GDIS centroid CSV inside a zip, named like the real granule."""
    inner = source_dir / "pend-gdis-1960-2018-disasterlocations.csv"
    gdis_csv_frame.to_csv(inner, index=False, encoding="latin-1")
    archive = source_dir / "pend-gdis-1960-2018-disasterlocations-csv.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(inner, arcname=inner.name)
        bundle.writestr("pend-gdis-1960-2018-codebook.pdf", b"%PDF-1.4 stub")
    inner.unlink()
    return archive


@pytest.fixture
def gdis_gpkg(source_dir: Path) -> Path:
    """An invented GDIS footprint GeoPackage on the real `GPKG` layer."""
    geometries = [
        box(0, 0, 1, 1),
        box(2, 2, 3, 3),
        box(0, 0, 1, 1),
        box(0, 0, 1, 1),
        box(0, 0, 1, 1),
    ]
    gdf = gpd.GeoDataFrame(_read(GDIS_GPKG_CSV), geometry=geometries, crs="EPSG:4326")
    path = source_dir / "pend-gdis-1960-2018-disasterlocations.gpkg"
    gdf.to_file(path, layer="GPKG", driver="GPKG")
    return path


@pytest.fixture
def dataverse_listing() -> dict[str, Any]:
    """A canned Dataverse `:latest` file listing with the archive and its siblings."""
    return {
        "status": "OK",
        "data": [
            {"dataFile": {"id": 1, "filename": "990101_emdat_archive.xlsx"}},
            {"dataFile": {"id": 2, "filename": "990101_emdat_columns.csv"}},
            {"dataFile": {"id": 3, "filename": "01_data_structure_and_content.pdf"}},
        ],
    }


@pytest.fixture
def warning_messages():
    """Collect WARNING-level loguru messages for the test's duration."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


@pytest.fixture
def info_messages():
    """Collect INFO-level loguru messages for the test's duration."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


class FakeHttp:
    """Stand-in for `HttpClient` that serves a canned listing and a local file.

    Attributes:
        json_payload: What :meth:`get_json` returns.
        source: File copied into place by :meth:`download`.
        calls: Every `(method, url)` pair seen, for asserting the request shape.
    """

    def __init__(self, json_payload: Any, source: Path | None = None) -> None:
        """Store the canned payload and the file to serve."""
        self.json_payload = json_payload
        self.source = source
        self.calls: list[tuple[str, str]] = []
        #: Keyword arguments each `download` was given, so a test can assert the
        #: guards actually reach the transport rather than trusting the docstring.
        self.download_kwargs: list[dict[str, Any]] = []

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """Record the call and return the canned payload."""
        self.calls.append(("get_json", url))
        return self.json_payload

    def download(self, url: str, dest: Path, **kwargs: Any) -> Path:
        """Record the call, its keyword arguments, and copy the fixture to `dest`."""
        self.calls.append(("download", url))
        self.download_kwargs.append(dict(kwargs))
        dest.write_bytes(Path(self.source).read_bytes())
        return dest


class FakeGranule:
    """Minimal stand-in for an `earthaccess` `DataGranule`."""

    def __init__(self, link: str) -> None:
        """Store the single data link this granule exposes."""
        self._link = link

    def data_links(self) -> list[str]:
        """Return the granule's data links."""
        return [self._link]


class FakeEarthaccess:
    """Stand-in for the `earthaccess` module used by the GDIS download path.

    Attributes:
        granules: What :meth:`search_data` returns.
        source: File copied into `local_path` by :meth:`download`.
        searched: Keyword arguments seen by :meth:`search_data`.
    """

    def __init__(self, granules: list[FakeGranule], source: Path | None = None) -> None:
        """Store the granules to return and the file to serve."""
        self.granules = granules
        self.source = source
        self.searched: list[dict[str, Any]] = []
        #: Keyword arguments each `download` was given.
        self.download_kwargs: list[dict[str, Any]] = []

    def search_data(self, **kwargs: Any) -> list[FakeGranule]:
        """Record the query and return the canned granules."""
        self.searched.append(kwargs)
        return self.granules

    def download(self, granules: list[FakeGranule], local_path: str, **kwargs: Any):
        """Record the keyword arguments and copy the fixture into `local_path`."""
        self.download_kwargs.append(dict(kwargs))
        source = Path(self.source)
        target = Path(local_path) / source.name
        target.write_bytes(source.read_bytes())
        return [target]
