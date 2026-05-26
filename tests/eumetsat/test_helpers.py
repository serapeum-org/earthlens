"""Unit tests for the EUMETSAT helper functions."""

from __future__ import annotations

import pytest

from earthlens.base import SpatialExtent
from earthlens.eumetsat._helpers import (
    antimeridian_bboxes,
    eumdac_bbox,
    safe_product_filename,
)

pytestmark = pytest.mark.eumetsat


def test_eumdac_bbox_west_south_east_north_order():
    """eumdac_bbox emits the W,S,E,N comma string the OpenSearch query expects."""
    assert eumdac_bbox(-1.0, 50.0, 1.0, 52.0) == "-1.0,50.0,1.0,52.0"


def test_antimeridian_bboxes_single_box_for_standard_extent():
    """A standard extent yields exactly one bbox string."""
    space = SpatialExtent.from_pairs(lat_lim=[50, 52], lon_lim=[-1, 1])
    assert antimeridian_bboxes(space) == ["-1.0,50.0,1.0,52.0"]


def test_safe_product_filename_plain_id_unchanged():
    """A normal product id is returned unchanged."""
    assert safe_product_filename("MSG4-SEVI-20240601.nat") == "MSG4-SEVI-20240601.nat"


@pytest.mark.parametrize("raw", ["../../etc/passwd", "a/b/c.nc", "dir\\file.nc"])
def test_safe_product_filename_strips_directories(raw):
    """Any directory component is stripped to the basename."""
    result = safe_product_filename(raw)
    assert "/" not in result and "\\" not in result


@pytest.mark.parametrize("raw", ["", "   ", "/", "..", "."])
def test_safe_product_filename_rejects_unusable_id(raw):
    """An empty or traversal-only id raises ValueError."""
    with pytest.raises(ValueError, match="usable filename"):
        safe_product_filename(raw)
