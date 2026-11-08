"""The shared cross-backend contracts, run against WDPA from its own lane.

The worked example for the pattern: core's registry sweep already checks every
backend, but it reports a failure against core's lane and its Codecov flag, and
it can only reach backends that live in this repo. Subclassing the `Base*`
classes puts the same assertions in the provider's own directory, so `-m wdpa`
selects them and a new backend has a checklist to copy.
"""

from __future__ import annotations

import pytest

from earthlens.testing import (
    BaseBackendDateContract,
    BaseCatalogContract,
    BaseOutputKindContract,
    BasePolygonAoiContract,
)
from earthlens.wdpa import WDPA
from earthlens.wdpa import catalog as wdpa_catalog

pytestmark = pytest.mark.unit


class TestWdpaContracts(
    BaseBackendDateContract,
    BasePolygonAoiContract,
    BaseOutputKindContract,
    BaseCatalogContract,
):
    """WDPA against the four shared contracts."""

    backend = WDPA
    catalog_module = wdpa_catalog
    #: Protected Planet filters server-side by bounding box, so a polygon mask
    #: is advisory and the facade must warn rather than imply an exact clip.
    clips_to_polygon_mask = False
    output_kind = "vector"
