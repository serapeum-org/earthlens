"""Climate / teleconnection index backend (`earthlens.climate_indices`).

Fetches monthly teleconnection-index series (ENSO/ONI, NAO, AO, PDO,
AMO, SOI, PNA, …) from two open ASCII sources — NOAA PSL and the KNMI
Climate Explorer — and returns them as a long-format
:class:`pandas.DataFrame` (`date`, `index`, `value`, `source`). These are
**global scalar monthly series** with no geometry, so the backend is
`OUTPUT_KIND = "tabular"`: spatial arguments are accepted for signature
parity but ignored, and `aggregate=` is rejected.

The public surface is the :class:`Catalog` (index id → URL + dialect +
metadata) and its :class:`Index` rows, plus the two pure ASCII parsers
:func:`parse_psl` / :func:`parse_climexp`. The :class:`ClimateIndices`
backend orchestrates download → parse → long frame.
"""

from __future__ import annotations

from earthlens.climate_indices._helpers import (
    empty_canonical,
    parse_climexp,
    parse_psl,
)
from earthlens.climate_indices.catalog import Catalog, Index

__all__ = [
    "Catalog",
    "Index",
    "parse_psl",
    "parse_climexp",
    "empty_canonical",
]
