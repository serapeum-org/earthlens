"""Gated end-to-end tests for the FLOPROS backend (live NHESS supplement fetch).

Selected with `-m "e2e and flopros"`; the default suite deselects `e2e`. These
hit the real NHESS public file host, so they need network but no credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.core import EarthLens
from earthlens.flopros import FLOPROS

pytestmark = [pytest.mark.e2e, pytest.mark.flopros]


def test_live_download_returns_real_collection(tmp_path: Path) -> None:
    """A live download returns the ~4650 FLOPROS protection polygons."""
    fc = FLOPROS(layer="merged_riverine", path=tmp_path, cache_dir=tmp_path).download()

    assert isinstance(fc, FeatureCollection)
    assert len(fc) > 4000  # ~4650 subnational units in the shipped shapefile
    assert list(fc.columns) == [
        "name",
        "geonunit",
        "type_en",
        "merged_riverine",
        "geometry",
    ]
    assert fc.crs.to_epsg() == 4326
    assert (fc["merged_riverine"] >= 0).all()
    assert list(tmp_path.glob("flopros_*.gpkg"))


def test_live_bbox_filter_subsets_units(tmp_path: Path) -> None:
    """A bbox over the Low Countries returns a handful of subnational units."""
    fc = EarthLens(
        data_source="flopros",
        lat_lim=[50.7, 53.6],
        lon_lim=[3.3, 7.2],
        cache_dir=tmp_path,
    ).download()

    assert isinstance(fc, FeatureCollection)
    assert 0 < len(fc) < 200
