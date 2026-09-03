"""Station registry for the NEXRAD radar backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the
bundled `radar_data_catalog.yaml` — a curated map of WSR-88D site ids
(`"KTLX"`) to name / latitude / longitude / state. The catalog gives
each fetched volume a point geometry and lets a request select the
radars inside a bounding box.

The catalog is **informational**: any valid four-letter site id can be
fetched even if it is absent here (the volume just gets no geometry).
The path to the bundled YAML lives at :data:`CATALOG_PATH`;
monkey-patch it to redirect the loader in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "radar_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level station-catalog parse cache."""
    _CATALOG_CACHE.clear()


def _load_stations(path: Path) -> dict[str, Station]:
    """Return the station registry at `path`, memoised on the file's mtime.

    Args:
        path: Path to `radar_data_catalog.yaml` (or a test override).

    Returns:
        dict[str, Station]: The `stations:` map keyed by site id.

    Raises:
        ValueError: If `path` does not exist, the file has no `stations:`
            block, or a row fails validation.
    """
    return load_catalog(path, _CATALOG_CACHE, _parse_stations, provider="NEXRAD")


def _parse_stations(files: list[Path]) -> dict[str, Station]:
    """Parse and validate the station registry.

    Args:
        files: The contributing YAML files (radar ships a single file).

    Returns:
        dict[str, Station]: The `stations:` map keyed by site id.

    Raises:
        ValueError: If the file has no `stations:` block or a row fails
            validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    rows = data.get("stations") or {}
    if not rows:
        raise ValueError(f"{path} is missing or has an empty 'stations:' block.")
    stations: dict[str, Station] = {}
    for site_id, body in rows.items():
        try:
            stations[site_id] = Station(**(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} station {site_id!r} failed validation:\n{exc}"
            ) from exc
    return stations


class Station(BaseModel):
    """One WSR-88D radar site.

    The site id (e.g. `"KTLX"`) is the parent key in
    :attr:`Catalog.datasets` and is not stored on the row.

    Attributes:
        name: Human-readable site name / location.
        latitude: Site latitude in degrees (south negative).
        longitude: Site longitude in degrees (west negative).
        state: Two-letter US state / territory code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    state: str = ""


class Catalog(AbstractCatalog):
    """Catalog of NEXRAD WSR-88D sites.

    Reads the bundled `radar_data_catalog.yaml` and exposes its `stations:` block
    as a typed `dict[str, Station]`. Instantiate with no arguments
    (`Catalog()`).

    Examples:
        - Look up a site and read its location:
            ```python
            >>> from earthlens.radar import Catalog
            >>> ktlx = Catalog().get_station("KTLX")
            >>> (round(ktlx.latitude, 2), round(ktlx.longitude, 2))
            (35.33, -97.28)

            ```
    """

    _catalog_kind: str = "NEXRAD station catalog"
    _entry_noun: str = "stations"

    datasets: dict[str, Station] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled station registry.

        Returns:
            dict[str, Any]: The `stations:` map keyed by site id.
        """
        return {"datasets": _load_stations(CATALOG_PATH)}

    def get_station(self, site_id: str) -> Station:
        """Resolve a site id to its :class:`Station` (did-you-mean on miss).

        Args:
            site_id: A four-letter WSR-88D id (e.g. `"KTLX"`).

        Returns:
            Station: The resolved site.

        Raises:
            ValueError: When `site_id` is unknown (with a did-you-mean
                hint from the base class).
        """
        return cast("Station", self.get_dataset(site_id))

    def in_bbox(
        self, west: float, south: float, east: float, north: float
    ) -> list[str]:
        """Return the site ids whose location falls inside a bbox.

        Args:
            west: West edge in degrees.
            south: South edge in degrees.
            east: East edge in degrees.
            north: North edge in degrees.

        Returns:
            list[str]: Matching site ids, sorted.

        Examples:
            - Find the catalogued sites over the south-central US:
                ```python
                >>> from earthlens.radar import Catalog
                >>> "KTLX" in Catalog().in_bbox(-100, 33, -95, 37)
                True

                ```
        """
        hits = [
            sid
            for sid, s in self.datasets.items()
            if west <= s.longitude <= east and south <= s.latitude <= north
        ]
        return sorted(hits)


#: Back-compat alias — the radar catalog was originally `StationCatalog`.
#: `Catalog` is the canonical name (uniform with every other backend);
#: `StationCatalog` stays importable.
StationCatalog = Catalog
