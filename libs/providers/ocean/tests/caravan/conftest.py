"""Fixtures for the Caravan backend tests — real archive shapes, no network."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
import requests

from earthlens.base.http import HttpClient
from earthlens.caravan.catalog import Catalog

#: The current-era timeseries header, in the alphabetical order the GRDC,
#: Denmark and Germany archives use.
CURRENT_HEADER = (
    "date,potential_evaporation_sum_ERA5_LAND,"
    "potential_evaporation_sum_FAO_PENMAN_MONTEITH,streamflow,"
    "temperature_2m_mean,total_precipitation_sum"
)

#: The same variables in the grouped order the Israel and base v1.2 archives
#: use, so column selection is proven to be by name and not by position.
SHUFFLED_HEADER = (
    "date,temperature_2m_mean,total_precipitation_sum,"
    "potential_evaporation_sum_ERA5_LAND,"
    "potential_evaporation_sum_FAO_PENMAN_MONTEITH,streamflow"
)

#: The legacy header: one `potential_evaporation_sum`, as base v1.2 ships.
LEGACY_HEADER = (
    "date,potential_evaporation_sum,streamflow,temperature_2m_mean,"
    "total_precipitation_sum"
)


def _rows(header: str, streamflow: str = "1.5") -> str:
    """Build three daily rows matching `header`'s column order."""
    values = {
        "date": ["2020-01-01", "2020-01-02", "2020-01-03"],
        "potential_evaporation_sum_ERA5_LAND": ["4.3", "1.9", "7.8"],
        "potential_evaporation_sum_FAO_PENMAN_MONTEITH": ["3.1", "1.2", "5.5"],
        "potential_evaporation_sum": ["4.3", "1.9", "7.8"],
        "streamflow": [streamflow, streamflow, ""],
        "temperature_2m_mean": ["10.1", "11.2", "9.8"],
        "total_precipitation_sum": ["0.0", "2.5", "1.1"],
    }
    columns = header.split(",")
    lines = [header]
    for i in range(3):
        lines.append(",".join(values[column][i] for column in columns))
    return "\n".join(lines) + "\n"


ATTRIBUTES_OTHER = (
    "gauge_id,area,country,gauge_lat,gauge_lon,gauge_name\n"
    "dk_1,100.0,Denmark,56.0,9.5,Aarhus\n"
    "dk_2,200.0,Denmark,57.0,10.5,Aalborg\n"
    "xx_9,300.0,South Africa,-28.0,17.0,Orange\n"
)

ATTRIBUTES_CARAVAN = "gauge_id,aridity_ERA5_LAND,p_mean\ndk_1,0.5,2.1\ndk_2,0.6,2.2\n"


def build_zip(root_prefix: str = "", header: str = CURRENT_HEADER) -> bytes:
    """Build an in-memory Caravan-shaped ZIP.

    Args:
        root_prefix: Directory every member sits under, or `""` for the
            Denmark/Germany shape that has none.
        header: Timeseries column header to write.

    Returns:
        bytes: The ZIP archive.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in _archive_members(header).items():
            archive.writestr(f"{root_prefix}{name}", body)
    return buffer.getvalue()


def build_tar(root_prefix: str = "Caravan/", header: str = LEGACY_HEADER) -> bytes:
    """Build an in-memory Caravan-shaped `.tar.gz`.

    Args:
        root_prefix: Directory every member sits under.
        header: Timeseries column header to write.

    Returns:
        bytes: The gzipped tar archive.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in _archive_members(header).items():
            data = body.encode()
            info = tarfile.TarInfo(f"{root_prefix}{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _archive_members(header: str) -> dict[str, str]:
    """Return the member-name to body map both archive builders share."""
    return {
        "timeseries/csv/dk/dk_1.csv": _rows(header, "1.5"),
        "timeseries/csv/dk/dk_2.csv": _rows(header, "2.5"),
        "timeseries/csv/xx/xx_9.csv": _rows(header, "9.5"),
        "timeseries/netcdf/dk/dk_1.nc": "not-really-netcdf",
        "attributes/dk/attributes_other_dk.csv": ATTRIBUTES_OTHER,
        "attributes/dk/attributes_caravan_dk.csv": ATTRIBUTES_CARAVAN,
        "attributes/xx/attributes_other_xx.csv": ATTRIBUTES_OTHER,
        "shapefiles/dk/dk_basin_shapes.shp": "shp",
        "shapefiles/dk/dk_basin_shapes.shx": "shx",
        "shapefiles/dk/dk_basin_shapes.dbf": "dbf",
        # A second source's shapes, so the multi-source merge path is reachable.
        "shapefiles/xx/xx_basin_shapes.shp": "shp",
        "shapefiles/xx/xx_basin_shapes.shx": "shx",
        "shapefiles/xx/xx_basin_shapes.dbf": "dbf",
        "licenses/dk/license_dk.md": "CC-BY-4.0",
    }


class FakeRangeSession:
    """Serves `Range` requests out of an in-memory blob, recording each call."""

    def __init__(self, blob: bytes, *, ignore_range: bool = False) -> None:
        self.blob = blob
        self.ignore_range = ignore_range
        self.head_calls: list[str] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def head(self, url: str, **kwargs: Any) -> requests.Response:
        self.head_calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Length"] = str(len(self.blob))
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.get_calls.append((url, kwargs))
        response = requests.Response()
        response.url = url
        if self.ignore_range:
            response.status_code = 200
            response._content = self.blob
            return response
        first, last = kwargs["headers"]["Range"].removeprefix("bytes=").split("-")
        response.status_code = 206
        response._content = self.blob[int(first) : int(last) + 1]
        response.headers["Content-Range"] = f"bytes {first}-{last}/{len(self.blob)}"
        return response


@pytest.fixture
def zip_blob() -> bytes:
    """A Caravan-shaped ZIP with no root directory (the Denmark shape)."""
    return build_zip()


@pytest.fixture
def prefixed_zip_blob() -> bytes:
    """A Caravan-shaped ZIP under a root directory (the GRDC shape)."""
    return build_zip(root_prefix="Caravan_extension_DK/")


@pytest.fixture
def fake_client(zip_blob: bytes) -> HttpClient:
    """An `HttpClient` whose transport serves the fixture ZIP."""
    return HttpClient(session=FakeRangeSession(zip_blob))


@pytest.fixture
def catalog() -> Catalog:
    """A catalog whose rows point at a local fixture archive."""
    return Catalog(**_fixture_catalog())


def _zip_file(name: str, prefix: str | None) -> Any:
    """Build a zip `ArchiveFile` descriptor for the fixture catalog."""
    from earthlens.caravan.catalog import ArchiveFile

    return ArchiveFile(
        record=1,
        name=name,
        size=0,
        md5="d41d8cd98f00b204e9800998ecf8427e",
        archive_format="zip",
        root_prefix=prefix,
    )


def _fixture_catalog() -> dict[str, Any]:
    """Build catalog kwargs describing the fixture archives."""
    from earthlens.caravan.catalog import (
        ArchiveFile,
        Extension,
        Source,
        Variable,
        Version,
    )

    versions = {
        "1.0": Version(
            doi="10.5281/zenodo.1",
            release_date="2025-01-01",
            data_period=(2020, 2020),
            n_catchments=3,
            column_set="current",
            files={
                "csv": _zip_file("fixture.zip", None),
                "netcdf": _zip_file("fixture.zip", None),
            },
        )
    }
    tar_versions = {
        "1.6": Version(
            doi="10.5281/zenodo.2",
            release_date="2025-01-01",
            n_catchments=3,
            column_set="legacy",
            files={
                "csv": ArchiveFile(
                    record=2,
                    name="fixture.tar.gz",
                    size=0,
                    md5="d41d8cd98f00b204e9800998ecf8427e",
                    archive_format="tar.gz",
                )
            },
        ),
        "1.2": Version(
            doi="10.5281/zenodo.3",
            release_date="2023-01-01",
            n_catchments=3,
            column_set="legacy",
            files={"csv": _zip_file("legacy.zip", "Caravan/")},
        ),
    }
    variables = {
        "streamflow": Variable(name="streamflow", column="streamflow", units="mm/d"),
        "total_precipitation": Variable(
            name="total_precipitation", column="total_precipitation_sum", units="mm/d"
        ),
        "potential_evaporation": Variable(
            name="potential_evaporation",
            column="potential_evaporation_sum_ERA5_LAND",
            legacy_column="potential_evaporation_sum",
            units="mm/d",
        ),
        "potential_evaporation_fao": Variable(
            name="potential_evaporation_fao",
            column="potential_evaporation_sum_FAO_PENMAN_MONTEITH",
            units="mm/d",
        ),
        "temperature_2m_mean": Variable(
            name="temperature_2m_mean", column="temperature_2m_mean", units="degC"
        ),
        "water_level": Variable(
            name="water_level", column="water_level", units="m", sources=["camelsde"]
        ),
    }
    return {
        "datasets": {
            "demo": Extension(
                key="demo",
                license="CC-BY-4.0",
                sources={"dk": Source(n_catchments=2), "xx": Source(n_catchments=1)},
                default_version="1.0",
                versions=versions,
            ),
            "big": Extension(
                key="big",
                license="CC-BY-4.0",
                sources={"dk": Source(n_catchments=2)},
                default_version="1.6",
                versions=tar_versions,
            ),
        },
        "variables": variables,
        "available_datasets": ["big", "demo"],
    }
