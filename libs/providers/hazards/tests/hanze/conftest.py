"""Shared fixtures for the HANZE backend tests.

Everything here is offline: a trimmed events CSV, a tiny NUTS-3 region shapefile
zip (built with geopandas in EPSG:3035, the shape the real release ships), and a
fake `HttpClient` that copies those fixtures instead of hitting Zenodo. The
`hanze_root` fixture pre-populates an output directory with both files under
their real Zenodo names, so a backend constructed against it downloads nothing.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import box

#: The real Zenodo file names the backend resolves from the bundled catalog.
EVENTS_NAME = "HANZE_events_v3_0_1b.csv"
REGIONS_NAME = "Regions_v2024_simplified.zip"
SHP_STEM = "NUTS3_regions_v2024_simplified"

#: The full HANZE events header (verified against the live CSV).
_HEADER = (
    "ID,Country code,Year,Country name,Start date,End date,Type,Profile code,"
    "Profile name,Event source,Regions affected (NUTS 3),Area affected,Fatalities,"
    "Persons affected,Losses (nominal value),Losses (original currency),"
    "Losses (real value),Cause,Notes,Sources,Traceability,Changes,Entry date,"
    "Last update"
)

#: One trimmed row per interesting case; only the columns the backend reads carry
#: meaningful values, the rest are filler. `(id, country, year, type, regions)`.
_ROWS: list[tuple[int, str, int, str, str]] = [
    (1, "DE", 1962, "River", "DE300;DE711"),
    (2, "DE", 2002, "River", "DE711"),
    (3, "DE", 2013, "Coastal", "DE300"),
    (4, "NL", 1953, "Coastal", "NL326;NL414"),
    (5, "NL", 1995, "River", "NL414"),
    (6, "FR", 2010, "River/Coastal", "FR101"),
    (7, "FR", 1875, "Flash", "FR101"),
    (8, "IT", 2000, "Flash", "ITH10;ITX99"),
]

#: NUTS-3 boundary boxes in WGS84; reprojected to EPSG:3035 before writing so the
#: shapefile carries the real release's projection. `ITX99` is deliberately absent
#: so a join can be shown to drop an unmatched code.
_REGION_BOXES: dict[str, tuple[str, tuple[float, float, float, float]]] = {
    "DE300": ("Berlin", (13.0, 52.3, 13.8, 52.7)),
    "DE711": ("Darmstadt", (8.0, 50.0, 8.5, 50.5)),
    "NL326": ("Amsterdam", (4.8, 52.3, 5.2, 52.7)),
    "NL414": ("Zuidoost-Noord-Brabant", (5.0, 51.4, 5.5, 51.8)),
    "FR101": ("Paris", (2.2, 48.7, 2.5, 49.0)),
    "ITH10": ("Bolzano", (11.0, 46.0, 11.5, 46.5)),
}


def _events_csv_text() -> str:
    """Return the trimmed events CSV as text, header plus the canned rows."""
    lines = [_HEADER]
    for event_id, country, year, flood_type, regions in _ROWS:
        cells = [""] * 24
        cells[0] = str(event_id)
        cells[1] = country
        cells[2] = str(year)
        cells[3] = f"{country} name"
        cells[6] = flood_type
        cells[10] = regions
        cells[12] = str(event_id * 10)  # Fatalities — a distinct non-zero per row.
        # Quote the regions cell (it holds no comma, but keep the shape robust).
        cells[10] = f'"{regions}"'
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


def _build_region_zip(destination: Path) -> Path:
    """Write the tiny NUTS-3 shapefile (EPSG:3035) and zip it to `destination`."""
    codes = list(_REGION_BOXES)
    frame = gpd.GeoDataFrame(
        {
            "Code": codes,
            "Name": [_REGION_BOXES[code][0] for code in codes],
        },
        geometry=[box(*_REGION_BOXES[code][1]) for code in codes],
        crs="EPSG:4326",
    ).to_crs("EPSG:3035")
    shp_dir = destination.parent / "_shp_build"
    shp_dir.mkdir(parents=True, exist_ok=True)
    shp_path = shp_dir / f"{SHP_STEM}.shp"
    frame.to_file(shp_path)
    with zipfile.ZipFile(destination, "w") as archive:
        for part in sorted(shp_dir.glob(f"{SHP_STEM}.*")):
            archive.write(part, arcname=part.name)
    return destination


@pytest.fixture(scope="session")
def events_csv_text() -> str:
    """The trimmed HANZE events CSV text."""
    return _events_csv_text()


@pytest.fixture(scope="session")
def region_zip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tiny NUTS-3 region shapefile zip built once per session."""
    root = tmp_path_factory.mktemp("hanze_regions")
    return _build_region_zip(root / REGIONS_NAME)


@pytest.fixture
def hanze_root(tmp_path: Path, events_csv_text: str, region_zip: Path) -> Path:
    """An output dir pre-seeded with both source files under their Zenodo names."""
    (tmp_path / EVENTS_NAME).write_text(events_csv_text, encoding="utf-8")
    (tmp_path / REGIONS_NAME).write_bytes(region_zip.read_bytes())
    return tmp_path


class FakeHttpClient:
    """Recording `HttpClient` stand-in that copies fixtures to the destination."""

    def __init__(self, sources: dict[str, Path]) -> None:
        """Map a Zenodo file name to the local fixture it should copy."""
        self._sources = sources
        self.calls: list[str] = []

    def download(self, url: str, dest: Any, **kwargs: Any) -> Path:
        """Copy the fixture whose name appears in `url` to `dest`, recording it.

        Honours `expect_magic` the way the real `HttpClient.download` does — a
        body not starting with the required prefix raises `ValueError` — so the
        download-time content guard can be exercised offline.
        """
        dest = Path(dest)
        self.calls.append(url)
        name = next(name for name in self._sources if name in url)
        payload = self._sources[name].read_bytes()
        magic = kwargs.get("expect_magic")
        if magic is not None and not payload.startswith(magic):
            raise ValueError(
                f"{url} returned a body that does not start with {magic!r}"
            )
        dest.write_bytes(payload)
        return dest


@pytest.fixture
def fake_http(tmp_path: Path, events_csv_text: str, region_zip: Path) -> FakeHttpClient:
    """A fake client that serves the events CSV and region zip from disk."""
    events_src = tmp_path / f"_src_{EVENTS_NAME}"
    events_src.write_text(events_csv_text, encoding="utf-8")
    return FakeHttpClient({EVENTS_NAME: events_src, REGIONS_NAME: region_zip})
