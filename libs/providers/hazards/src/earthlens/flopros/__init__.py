"""FLOPROS flood-protection-standard backend (`earthlens.flopros`).

Fetches the FLOPROS global database of flood-protection standards (Scussolini
et al., 2016): ~4650 subnational polygons, each carrying protection standards
as return periods (years) across the Modelled, Merged, Design, and Policy
layers (riverine + coastal). It is the defended-vs-undefended correction —
clip a hazard map by the local protection standard to separate protected from
exposed areas. A single public shapefile (inside the NHESS-2016 supplement
zip), no credentials, geometry read via pyramids.
"""

from __future__ import annotations

from earthlens.flopros.backend import FLOPROS
from earthlens.flopros.catalog import CATALOG_PATH, Catalog, FloprosDataset

__all__ = ["CATALOG_PATH", "Catalog", "FLOPROS", "FloprosDataset"]
