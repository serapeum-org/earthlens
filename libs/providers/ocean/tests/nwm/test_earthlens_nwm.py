"""Facade-routing tests for the `"nwm"` / `"national-water-model"` keys."""

from __future__ import annotations

import pytest
from earthlens.earthlens import EarthLens

import earthlens.nwm
from earthlens.nwm import NWM

pytestmark = [pytest.mark.nwm, pytest.mark.unit]


def test_both_keys_registered():
    """The primary key and the alias are both in the registry."""
    assert "nwm" in EarthLens.DataSources
    assert "national-water-model" in EarthLens.DataSources


def test_keys_resolve_to_nwm_class():
    """Both keys resolve to earthlens.nwm.NWM."""
    assert EarthLens.DataSources["nwm"] is earthlens.nwm.NWM
    assert EarthLens.DataSources["national-water-model"] is NWM


def test_facade_constructs_backend(tmp_path):
    """The facade builds the NWM backend and forwards configuration=."""
    lens = EarthLens(
        data_source="nwm",
        variables={"chrtout": ["streamflow"]},
        start="2026-05-26",
        end="2026-05-26",
        path=str(tmp_path),
        configuration="short_range",
    )
    assert isinstance(lens.datasource, NWM)
    assert lens.datasource.OUTPUT_KIND == "tabular"


def test_facade_default_bbox_is_whole_earth(tmp_path):
    """Omitting lat/lon yields a whole-Earth bbox (no subset)."""
    lens = EarthLens(
        data_source="national-water-model",
        variables={"ldasout": ["SOIL_M"]},
        start="2026-05-26",
        end="2026-05-26",
        path=str(tmp_path),
        configuration="short_range",
    )
    assert lens.datasource._wants_subset() is False
    assert lens.datasource.OUTPUT_KIND == "raster"
