"""Unit tests for the pure bathymetry URL / bbox helpers."""

from __future__ import annotations

import pytest

from earthlens.base import SpatialExtent
from earthlens.bathymetry._helpers import (
    bbox_from_extent,
    estimate_grid_pixels,
    griddap_subset_url,
    resolution_degrees,
)

ENDPOINT = "https://coastwatch.pfeg.noaa.gov/erddap"


def test_griddap_url_matches_live_format():
    """A -180..180 row builds the exact live-verified griddap subset URL."""
    url = griddap_subset_url(
        ENDPOINT, "GEBCO_2020", "elevation", (-18.0, 25.0, -17.0, 26.0)
    )
    assert url == (
        "https://coastwatch.pfeg.noaa.gov/erddap/griddap/GEBCO_2020.nc?"
        "elevation[(25.0):1:(26.0)][(-18.0):1:(-17.0)]"
    )


def test_griddap_url_latitude_before_longitude():
    """The latitude range precedes the longitude range in the URL."""
    url = griddap_subset_url(ENDPOINT, "d", "z", (10.0, 1.0, 20.0, 2.0))
    assert url.endswith("z[(1.0):1:(2.0)][(10.0):1:(20.0)]")


def test_griddap_url_tolerates_trailing_slash():
    """A trailing slash on the endpoint does not double up in the URL."""
    url = griddap_subset_url(ENDPOINT + "/", "d", "z", (10.0, 1.0, 20.0, 2.0))
    assert "/erddap/griddap/d.nc?" in url
    assert "erddap//griddap" not in url


def test_griddap_url_custom_step():
    """A non-default stride is written into both axis ranges."""
    url = griddap_subset_url(ENDPOINT, "d", "z", (10.0, 1.0, 20.0, 2.0), step=4)
    assert "[(1.0):4:(2.0)][(10.0):4:(20.0)]" in url


def test_griddap_url_shifts_lon_for_0360_row():
    """A 0..360 row wraps negative longitudes onto the server's frame."""
    url = griddap_subset_url(
        "https://e.org/erddap",
        "DEM360",
        "z",
        (-18.0, 25.0, -17.0, 26.0),
        lon_convention="0..360",
    )
    assert url.endswith("z[(25.0):1:(26.0)][(342.0):1:(343.0)]")


def test_griddap_url_positive_lon_unchanged_for_0360_row():
    """A positive longitude is unchanged when wrapped onto 0..360."""
    url = griddap_subset_url(
        "https://e.org/erddap", "d", "z", (150.0, 0.0, 151.0, 1.0), lon_convention="0..360"
    )
    assert "[(150.0):1:(151.0)]" in url


def test_griddap_url_rejects_antimeridian_crossing():
    """A bbox that crosses the antimeridian after normalisation raises."""
    with pytest.raises(ValueError, match="antimeridian"):
        griddap_subset_url(ENDPOINT, "d", "z", (170.0, 0.0, -170.0, 1.0))


def test_bbox_from_extent_orders_west_south_east_north():
    """bbox_from_extent returns (west, south, east, north) in order."""
    space = SpatialExtent.from_pairs(lat_lim=[25.0, 26.0], lon_lim=[-18.0, -17.0])
    assert bbox_from_extent(space) == (-18.0, 25.0, -17.0, 26.0)


def test_resolution_degrees_arc_units():
    """Arc-second and arc-minute labels convert to degrees."""
    assert resolution_degrees("15 arc-second") == pytest.approx(15 / 3600)
    assert resolution_degrees("1 arc-minute") == pytest.approx(1 / 60)


@pytest.mark.parametrize("label", ["native", "", "15 metres", "1 km"])
def test_resolution_degrees_unparseable_returns_none(label: str):
    """A non arc-second / arc-minute label yields None."""
    assert resolution_degrees(label) is None


def test_estimate_grid_pixels_for_arcsecond_bbox():
    """A 2x1 degree bbox at 15 arc-second is ~480x240 pixels."""
    assert estimate_grid_pixels((-1.0, 0.0, 1.0, 1.0), "15 arc-second") == (480, 240)


def test_estimate_grid_pixels_unparseable_returns_none():
    """An unparseable resolution gives no pixel estimate."""
    assert estimate_grid_pixels((0.0, 0.0, 1.0, 1.0), "native") is None
