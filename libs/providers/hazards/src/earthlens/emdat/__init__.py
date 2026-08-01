"""EM-DAT disaster event and impact backend.

Fetches disaster event/impact records derived from CRED's EM-DAT (the
Emergency Events Database, UCLouvain) through its two sanctioned programmatic
routes. The raw `public.emdat.be` portal is a manual XLSX download behind a
login and is deliberately not used.

Two routes, selected by the dataset id passed in `variables`:

* **`emdat:events`** — the **EM-DAT Archive** on the UCLouvain Dataverse
  (DOI `10.14428/DVN/I0LTPH`). Anonymous, no credentials, and the full public
  table: every disaster from 1900 onwards, natural *and* technological, with
  the human and economic impact columns. EM-DAT's own documentation calls this
  the preferred route for research. Returns a :class:`pandas.DataFrame`.
* **`gdis:points`** / **`gdis:polygons`** — **GDIS** (Geocoded Disasters), the
  CC-BY-4.0 geocoded derivative of EM-DAT *natural* disasters, 1960-2018, as a
  pyramids :class:`~pyramids.feature.collection.FeatureCollection`.
  `gdis:points` is the centroid CSV (about 1 MB, and the only distribution
  carrying `year` / `latitude` / `longitude`); `gdis:polygons` is the
  admin-unit footprint GeoPackage, which is a **2.2 GB** download and is
  therefore opt-in.

Because the two routes emit different shapes, `OUTPUT_KIND` is set **per
instance** from the resolved catalog row rather than fixed on the class:
`emdat:events` is `"tabular"`, both `gdis:*` rows are `"vector"`. The
:class:`earthlens.earthlens.EarthLens` facade reads the instance attribute to
know the return shape and to reject `aggregate=` (these are event records, not
gridded rasters).

Licensing differs per route and is not incidental. GDIS is **CC-BY-4.0** and
may be cached freely. The EM-DAT archive is **CC-BY-NC-ND-4.0** under Terms of
Use that limit free use to academic, non-profit-research, international-public
-organisation, government and media users, and that forbid redistributing the
database or building derivative databases from it — so earthlens fetches it for
the user and never caches or repacks it, and a `LicenseWarning` naming those
restrictions is emitted on download.

Not served here: the EM-DAT country-profile summaries, which are already
reachable through the shipped `hdx` backend as
`EarthLens("hdx", hdx_id="emdat-country-profiles")`.

Public surface (re-exported from this package):

* :class:`Catalog` — pydantic-backed loader for the bundled
  `emdat_data_catalog.yaml`.
* :class:`Dataset` — one catalog row (provider, output kind, transport detail,
  licence).
* :func:`clear_catalog_cache` — drop the module-level parse cache.
* :data:`CATALOG_PATH` — path to the bundled catalog YAML.
"""

from __future__ import annotations

from earthlens.emdat.catalog import (
    CATALOG_PATH,
    Catalog,
    Dataset,
    clear_catalog_cache,
)

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "Dataset",
    "clear_catalog_cache",
]
