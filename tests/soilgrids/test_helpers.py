"""Unit tests for the soilgrids pure helpers (coverage ids + request expansion)."""

from __future__ import annotations

import pytest

from earthlens.base.abstractdatasource import SpatialExtent
from earthlens.soilgrids import Catalog
from earthlens.soilgrids._helpers import (
    DEFAULT_QUANTILE,
    IGH_PROJ4,
    SOILGRIDS_ATTRIBUTION,
    bbox_from_extent,
    coverage_id,
    expand_request,
)

pytestmark = pytest.mark.soilgrids

STD_DEPTHS = ["0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm"]


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """The bundled SoilGrids catalog."""
    return Catalog()


@pytest.mark.parametrize(
    "property_id, depth, quantile, expected",
    [
        ("nitrogen", "5-15cm", "Q0.5", "nitrogen_5-15cm_Q0.5"),
        ("clay", "0-5cm", "mean", "clay_0-5cm_mean"),
        ("ocs", "0-30cm", "uncertainty", "ocs_0-30cm_uncertainty"),
    ],
)
def test_coverage_id_joins_parts(property_id, depth, quantile, expected) -> None:
    """coverage_id joins property, depth, and quantile with underscores."""
    assert coverage_id(property_id, depth, quantile) == expected


def test_expand_request_defaults_to_all_depths_at_mean(catalog: Catalog) -> None:
    """A properties-only request expands to every depth at the mean quantile."""
    triples = expand_request(["clay"], None, None, catalog)
    assert triples == [("clay", depth, "mean") for depth in STD_DEPTHS]
    assert DEFAULT_QUANTILE == "mean"


def test_expand_request_ocs_defaults_to_single_depth(catalog: Catalog) -> None:
    """ocs has a single 0-30cm depth, so its default expansion is one triple."""
    assert expand_request(["ocs"], None, None, catalog) == [("ocs", "0-30cm", "mean")]


def test_expand_request_explicit_quantiles_multiply_cells(catalog: Catalog) -> None:
    """Two quantiles double the number of triples per depth."""
    triples = expand_request(["clay"], None, ["Q0.05", "Q0.95"], catalog)
    assert len(triples) == len(STD_DEPTHS) * 2
    assert ("clay", "0-5cm", "Q0.05") in triples
    assert ("clay", "0-5cm", "Q0.95") in triples


def test_expand_request_explicit_depths_override(catalog: Catalog) -> None:
    """Explicit depths restrict the expansion to just those depths."""
    triples = expand_request(["phh2o"], ["0-5cm", "5-15cm"], ["mean"], catalog)
    assert triples == [("phh2o", "0-5cm", "mean"), ("phh2o", "5-15cm", "mean")]


def test_expand_request_multiple_properties_preserve_order(catalog: Catalog) -> None:
    """Properties are expanded in request order."""
    triples = expand_request(["silt", "sand"], ["0-5cm"], ["mean"], catalog)
    assert triples == [("silt", "0-5cm", "mean"), ("sand", "0-5cm", "mean")]


def test_expand_request_unknown_property_did_you_mean(catalog: Catalog) -> None:
    """An unknown property raises ValueError with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'clay'"):
        expand_request(["clayy"], None, None, catalog)


def test_expand_request_unknown_depth_did_you_mean(catalog: Catalog) -> None:
    """An unknown depth raises ValueError listing the available depths."""
    with pytest.raises(ValueError, match=r"has no depth '0-4cm'"):
        expand_request(["clay"], ["0-4cm"], None, catalog)


def test_expand_request_unknown_quantile_did_you_mean(catalog: Catalog) -> None:
    """An unknown quantile raises ValueError listing the available quantiles."""
    with pytest.raises(ValueError, match=r"has no quantile 'Q0.6'"):
        expand_request(["clay"], None, ["Q0.6"], catalog)


def test_bbox_from_extent_returns_west_south_east_north() -> None:
    """bbox_from_extent returns (west, south, east, north) in degrees."""
    space = SpatialExtent.from_pairs(lat_lim=[51.0, 52.0], lon_lim=[5.0, 6.0])
    assert bbox_from_extent(space) == (5.0, 51.0, 6.0, 52.0)


def test_igh_proj4_is_interrupted_goode_homolosine() -> None:
    """The coverage_crs shim is the Interrupted Goode Homolosine proj4 string."""
    assert "+proj=igh" in IGH_PROJ4


def test_attribution_names_isric_and_cc_by() -> None:
    """The attribution constant credits ISRIC under CC-BY 4.0."""
    assert "ISRIC" in SOILGRIDS_ATTRIBUTION
    assert "CC-BY 4.0" in SOILGRIDS_ATTRIBUTION
