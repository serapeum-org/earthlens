"""Unit tests for the pure bathymetry URL / bbox helpers."""

from __future__ import annotations

import urllib.error

import pytest
import requests

from earthlens.base import SpatialExtent
from earthlens.bathymetry import WcsServiceUnavailableError
from earthlens.bathymetry._helpers import (
    bbox_from_extent,
    estimate_grid_pixels,
    griddap_subset_url,
    is_wcs_service_failure,
    resolution_degrees,
)

pytestmark = pytest.mark.bathymetry

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
        "https://e.org/erddap",
        "d",
        "z",
        (150.0, 0.0, 151.0, 1.0),
        lon_convention="0..360",
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


class TestIsWcsServiceFailure:
    """Classifier that tells a WCS service outage from a request error."""

    @pytest.mark.parametrize(
        "message",
        [
            "WCS GetCapabilities returned a non-XML body from ows...",
            "the server sent a non xml response",
            "HTTP error code : 503",
            "500 Server Error: Internal Server Error",
            "received HTTP 429 from the endpoint",
            "http 408 request timeout",
            "502 Bad Gateway",
            "504 gateway time-out",
            "Service Unavailable",
            "the resource is temporarily unavailable",
            "Empty reply from server",
            "Max retries exceeded with url",
            "Connection reset by peer",
            "Connection aborted",
            "Remote end closed connection without response",
            "read timed out",
            "failed to establish a new connection",
            "Temporary failure in name resolution",
            "Name or service not known",
        ],
    )
    def test_service_messages_classify_true(self, message: str):
        """A message carrying a service / transport signature is a service failure.

        Args:
            message: The exception text under test.

        Test scenario:
            Each string is a real GDAL / requests availability signature, so the
            classifier must report `True`.
        """
        assert is_wcs_service_failure(RuntimeError(message)) is True, message

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError("boom"),
            requests.exceptions.Timeout("slow"),
            ConnectionError("dropped"),
            TimeoutError("late"),
            urllib.error.URLError("unreachable"),
        ],
    )
    def test_transport_exception_types_classify_true(self, exc: BaseException):
        """A transport exception type is a service failure regardless of message.

        Args:
            exc: The transport exception under test.

        Test scenario:
            `requests` / stdlib connection and timeout types are unambiguous
            transport failures, so the classifier reports `True`.
        """
        assert is_wcs_service_failure(exc) is True, type(exc).__name__

    @pytest.mark.parametrize(
        "message",
        [
            "Could not find coverage 'emodnet:mean'",
            "InvalidSubsetting: Empty intersection after subsetting",
            "grid is 5000 x 5000 pixels, too large",
            "unknown band 'depth'",
            "",
        ],
    )
    def test_request_errors_classify_false(self, message: str):
        """A request-shaping error carries no service signature, so it is False.

        Args:
            message: The exception text under test.

        Test scenario:
            A bad coverage id, an empty subset, an oversize grid (whose stray
            `5000` must not read as a status), an unknown band, and an empty
            message must all stay hard failures (`False`).
        """
        assert is_wcs_service_failure(RuntimeError(message)) is False, message

    def test_walks_cause_chain(self):
        """A transport error linked via __cause__ is detected through the wrapper."""
        inner = requests.exceptions.ConnectionError("Connection reset by peer")
        outer = RuntimeError("from_wcs failed")
        outer.__cause__ = inner
        assert is_wcs_service_failure(outer) is True

    def test_walks_context_chain(self):
        """A transport error linked via an implicit __context__ is also detected."""
        try:
            try:
                raise requests.exceptions.Timeout("connection lost")
            except requests.exceptions.Timeout:
                raise RuntimeError("wrapping a transport failure")
        except RuntimeError as exc:
            assert is_wcs_service_failure(exc) is True

    def test_self_referential_chain_is_cycle_safe(self):
        """A cyclic cause chain terminates instead of looping forever."""
        exc = RuntimeError("no service signal")
        exc.__cause__ = exc
        assert is_wcs_service_failure(exc) is False


class TestWcsServiceUnavailableError:
    """The typed error the WCS path raises for an unavailable service."""

    def test_is_a_runtime_error(self):
        """It subclasses RuntimeError so a broad transport catch still catches it."""
        assert issubclass(WcsServiceUnavailableError, RuntimeError)

    def test_preserves_its_message(self):
        """The human-facing message is carried through unchanged."""
        err = WcsServiceUnavailableError("the WCS service is unavailable, retry later")
        assert str(err) == "the WCS service is unavailable, retry later"

    def test_is_exported_from_the_package(self):
        """It is importable from the package surface for tests to skip on."""
        import earthlens.bathymetry as pkg

        assert pkg.WcsServiceUnavailableError is WcsServiceUnavailableError
        assert "WcsServiceUnavailableError" in pkg.__all__
