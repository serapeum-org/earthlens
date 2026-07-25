"""GBIF species-occurrence backend (`earthlens.gbif`).

Fetches georeferenced species occurrences from the Global Biodiversity
Information Facility through `pygbif.occurrences.search` (anonymous), maps
the taxon/space/time window to a points `FeatureCollection`, and warns on
restrictive per-record licenses. The reference occurrence backend the OBIS
twin mirrors. See :class:`earthlens.gbif.backend.GBIF`.

Examples:
    - The catalog resolves friendly taxon keys:
        ```python
        >>> from earthlens.gbif import Catalog
        >>> Catalog().resolve_taxon_key("birds")
        212

        ```
"""

from __future__ import annotations

from earthlens.gbif.backend import GBIF, GBIF_COLUMNS
from earthlens.gbif.catalog import CATALOG_PATH, Catalog, Taxon

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "GBIF",
    "GBIF_COLUMNS",
    "Taxon",
]
