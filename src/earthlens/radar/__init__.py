"""NEXRAD Level-II radar backend (real-time WSR-88D chunk feed).

Fetches WSR-88D Level-II radar volumes from the unsigned
`unidata-nexrad-level2-chunks` AWS bucket and assembles each volume's
ordered chunks into a single `.ar2v` file, returning a `GeoDataFrame`
inventory of what was fetched. The feed is near-real-time (a rolling
buffer of recent volumes), not a historical archive.

Public surface (re-exported from this package):

* :class:`Radar` — the backend; instantiate with a time window, a bbox,
  and a `{station_id: [...]}` mapping, then call :meth:`Radar.download`.
* :class:`StationCatalog` — loader for the bundled `radar_data_catalog.yaml`.
* :class:`Station` — one WSR-88D site row (name / lat / lon / state).
* :data:`CATALOG_PATH` — absolute path to the bundled station YAML.
* :data:`BUCKET` — the unsigned chunk bucket name.

The `[radar]` extra pulls `boto3` (unsigned S3); it is imported lazily,
so the package imports without the extra installed. Reading / gridding
the assembled volumes (via `pyart`) is a downstream follow-on.
"""

from __future__ import annotations

from earthlens.radar.backend import BUCKET, Radar
from earthlens.radar.catalog import CATALOG_PATH, Station, StationCatalog

__all__ = [
    "BUCKET",
    "CATALOG_PATH",
    "Radar",
    "Station",
    "StationCatalog",
]
