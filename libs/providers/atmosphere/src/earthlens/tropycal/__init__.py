"""Tropical-cyclone best-track backend.

Thin wrapper over `tropycal.tracks.TrackDataset` that returns tropical-
cyclone best tracks — from IBTrACS (global, 1848-present) or HURDAT2
(NHC North Atlantic / East Pacific reanalysis) — as a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` of track features
(CRS `EPSG:4326`).

This is a `vector` backend: the result is a table of track features, not
a gridded array, so :data:`TropicalCyclone.OUTPUT_KIND` is `"vector"` and
the :class:`earthlens.earthlens.core.EarthLens` facade rejects an `aggregate=`
argument for it. The default geometry is one `Point` per 6-hourly fix; a
`geometry="track"` kwarg returns one `LineString` per storm with summary
attributes.

tropycal needs **no credentials** (it fetches public best-track files
from NCEI/NHC over HTTPS), so there is no auth class. It is an optional
dependency — the `[tropycal]` extra — imported lazily so this package
imports without it.

Basin selection: for this backend `variables` is a `list[str]` of basin
codes — `variables=["north_atlantic"]`, `variables=["north_atlantic",
"east_pacific"]` — **not** data-variable names. This is an intentional,
documented overload (the facade makes `variables` a required argument).
The data source (`"ibtracs"`/`"hurdat"`), geometry mode, and filters
arrive as explicit :class:`TropicalCyclone` constructor keyword
arguments. Best-track only — realtime / operational / forecast products
are out of scope.

Public surface (re-exported from this package):

* :class:`TropicalCyclone` — the backend; instantiate with a date range,
  a bbox, and `variables=[basin, ...]`, then call
  :meth:`TropicalCyclone.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled
  `tropycal_data_catalog.yaml` basin -> track-field catalog.
* :class:`Basin` — one basin's catalog row (`name`, `sources`,
  `fields`).
* :class:`TrackField` — one per-fix track field (`units`, `long_name`).
* :func:`frame_to_fc` / :func:`empty_fc` — the storm-frame ->
  FeatureCollection mapper and its empty-result counterpart.
* :data:`CATALOG_PATH` — path to the bundled basin YAML;
  monkey-patchable in tests.

Examples:
    - List the registered basin codes:

        ```python
        >>> from earthlens.tropycal import Catalog
        >>> Catalog().codes()
        ['all', 'australia', 'both', 'east_pacific', 'north_atlantic', 'north_indian', 'south_atlantic', 'south_indian', 'south_pacific', 'west_pacific']

        ```
"""

from __future__ import annotations

from earthlens.tropycal._compat import ensure_pkg_resources

# tropycal 1.4 imports the setuptools-removed `pkg_resources` at module load;
# install a stand-in (when absent) before the backend lazily imports tropycal.
ensure_pkg_resources()

from earthlens.tropycal.backend import TropicalCyclone, Tropycal  # noqa: E402
from earthlens.tropycal.catalog import (  # noqa: E402
    CATALOG_PATH,
    Basin,
    Catalog,
    TrackField,
    clear_catalog_cache,
)
from earthlens.tropycal.events import (  # noqa: E402
    empty_fc,
    empty_recon_fc,
    frame_to_fc,
    recon_to_fc,
)

__all__ = [
    "CATALOG_PATH",
    "Basin",
    "Catalog",
    "TrackField",
    "TropicalCyclone",
    "Tropycal",
    "clear_catalog_cache",
    "empty_fc",
    "empty_recon_fc",
    "frame_to_fc",
    "recon_to_fc",
]
