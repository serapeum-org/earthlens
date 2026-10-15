"""The shared cross-backend contracts, run against GHSL from its own lane.

The counterpart to wdpa's: GHSL is a raster backend that *does* crop to the
polygon mask, so it declares `clips_to_polygon_mask = True` and the contract
checks the opposite branch of the capability flag.
"""

from __future__ import annotations

import pytest

from earthlens.ghsl import GHSL
from earthlens.ghsl import catalog as ghsl_catalog
from earthlens.testing import (
    BaseBackendDateContract,
    BaseCatalogContract,
    BaseOutputKindContract,
    BasePolygonAoiContract,
)

pytestmark = pytest.mark.unit


class TestGhslContracts(
    BaseBackendDateContract,
    BasePolygonAoiContract,
    BaseOutputKindContract,
    BaseCatalogContract,
):
    """GHSL against the four shared contracts."""

    backend = GHSL
    catalog_module = ghsl_catalog
    #: GHSL localises through pyramids and crops to the exact mask, so a
    #: polygon aoi is honoured rather than widened to its bounding box.
    clips_to_polygon_mask = True
    output_kind = "raster"
