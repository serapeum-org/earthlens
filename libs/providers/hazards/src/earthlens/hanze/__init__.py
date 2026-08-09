"""HANZE historical-flood-impacts backend.

Fetches the HANZE (Historical Analysis of Natural Hazards in Europe) database of
observed European flood events and their impacts (Paprotny et al.) from its
pinned static Zenodo release, and returns the event / impact records as a
:class:`pandas.DataFrame`. It is the observed hazard -> loss record: real floods
with fatalities, persons affected, area flooded and economic losses, against
which a modelled event set can be validated. Companion to the global `emdat`
backend.

This is a `tabular` backend by default: the result is a table of event / impact
rows, not a gridded array, so the :class:`earthlens.earthlens.EarthLens` facade
rejects an `aggregate=` argument. Passing `with_geometry=True` instead returns a
pyramids :class:`~pyramids.feature.collection.FeatureCollection` of the affected
NUTS-3 regions (a per-instance `vector` output).

HANZE needs **no credentials** — the Zenodo record is public (CC-BY-4.0) — so
there is no auth class and no `[hanze]` extra: the only dependencies (HttpClient,
pandas, `base/archive`, pyramids) are all core.

Public surface (re-exported from this package):

* :class:`Catalog` — loader for the bundled `hanze_data_catalog.yaml`.
* :class:`ZenodoRecord` / :class:`HanzeFile` / :class:`FloodType` /
  :class:`GeometryJoin` — the catalog's frozen row models.
* :data:`CATALOG_PATH` — path to the bundled catalog YAML; monkey-patchable in
  tests.

Examples:
    - List the flood-type vocabulary:

        ```python
        >>> from earthlens.hanze import Catalog
        >>> Catalog().flood_types()
        ['Coastal', 'Flash', 'River', 'River/Coastal']

        ```
"""

from __future__ import annotations

from earthlens.hanze.catalog import (
    CATALOG_PATH,
    Catalog,
    FloodType,
    GeometryJoin,
    HanzeFile,
    ZenodoRecord,
)

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "FloodType",
    "GeometryJoin",
    "HanzeFile",
    "ZenodoRecord",
]
