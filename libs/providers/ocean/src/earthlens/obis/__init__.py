"""OBIS marine-occurrence backend (`earthlens.obis`).

Fetches georeferenced marine species occurrences from the Ocean Biodiversity
Information System through `pyobis.occurrences.search` (anonymous), maps the
species/space/time window to a points `FeatureCollection`, and warns on
restrictive licenses. The marine twin of the GBIF backend. See
:class:`earthlens.obis.backend.OBIS`.

Examples:
    - The catalog resolves friendly species keys:
        ```python
        >>> from earthlens.obis import Catalog
        >>> Catalog().resolve_scientific_name("blue-whale")
        'Balaenoptera musculus'

        ```
"""

from __future__ import annotations

from earthlens.obis.backend import OBIS, OBIS_COLUMNS
from earthlens.obis.catalog import CATALOG_PATH, Catalog, Species

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "OBIS",
    "OBIS_COLUMNS",
    "Species",
]
