"""Administrative-boundary backend for earthlens.

`earthlens.admin` fetches administrative-boundary polygons from four public
sources — **geoBoundaries** (per-country ADM0–ADM5, CC-BY-4.0), **CGAZ**
(Comprehensive Global Admin Zones — seamless global ADM0/1/2, CC-BY-4.0),
**Natural Earth** (global cultural admin layers, public domain), and **US Census
TIGER/Line** (states / counties / tracts / nation, public domain). Every source
returns polygon boundaries, so this is a `vector` backend: `download()` returns a
pyramids `~pyramids.feature.collection.FeatureCollection` of polygons in
EPSG:4326 (and writes it to a vector file when a `path` is set), and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument.

A request names a **dataset** (`variables=["geoboundaries:adm1"]`) plus the
selector that dataset needs — `country=<ISO3>` for geoBoundaries, an optional
`scale=` for Natural Earth, an optional `year=` (and `state=` for tracts) for
TIGER; CGAZ is seamless and needs none. All four are keyless, so there is no
auth module and no extra SDK (the only dependencies are core `requests` and
`pyramids`).

**GADM is deliberately omitted** — its no-commercial / no-redistribute license
is incompatible with redistribution.

The public surface is the
:class:`~earthlens.admin.backend.AdminBoundaries` backend and the
:class:`~earthlens.admin.catalog.Catalog` of dataset rows.
"""

from __future__ import annotations

from earthlens.admin.backend import AdminBoundaries

from earthlens.admin.catalog import Catalog, Dataset

__all__ = ["AdminBoundaries", "Catalog", "Dataset"]
