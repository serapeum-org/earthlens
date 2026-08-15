"""CatRaRE heavy-rainfall event-catalogue backend (`earthlens.catrare`).

Fetches the DWD Catalogue of Radar-based Rainfall Events (CatRaRE v2026.01):
objectively-defined heavy-rainfall events over Germany, 2001-2025, derived from
the RADKLIM radar climatology. Two threshold selections (`T5` — return period
>= 5 years; `W3` — severity-weighted) each ship a FileGDB of event-footprint
polygons + maximum-rainfall points. The backend downloads the FileGDB, reads
one layer with pyramids, reprojects from the DWD RADOLAN grid to EPSG:4326, and
returns the events (area, duration, severity) filtered by date and bbox. The
companion to the `radklim` grids; CC-BY-4.0 / GeoNutzV, no credentials.
"""

from __future__ import annotations

from earthlens.catrare.backend import CatRaRE
from earthlens.catrare.catalog import CATALOG_PATH, Catalog, CatRaReDataset

__all__ = ["CATALOG_PATH", "Catalog", "CatRaRE", "CatRaReDataset"]
