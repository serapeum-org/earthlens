"""Unit tests for the pure bathymetry URL / bbox helpers."""

from __future__ import annotations

import errno
import socket
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


def _http_error(
    message: str, status: int | None = None
) -> requests.exceptions.HTTPError:
    """Build a requests HTTPError, optionally carrying a response status code."""
    err = requests.exceptions.HTTPError(message)
    if status is not None:
        response = requests.Response()
        response.status_code = status
        err.response = response
    return err


class TestIsWcsServiceFailure:
    """Classifier that tells a WCS service outage from a request error."""

    @pytest.mark.parametrize(
        "message",
        [
            "WCS GetCapabilities returned a non-XML body from ows...",
            "the server sent a non xml response",
            "HTTP error code : 503",
            "HTTP/1.1 503",
            "HTTP/1.1 503 Service Unavailable",
            "500 Server Error: Internal Server Error",
            "500 Internal Server Error",
            "GetCoverage failed with status 500",
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
            "[Errno 101] Network is unreachable",
            "[Errno 113] No route to host",
        ],
    )
    def test_service_messages_classify_true(self, message: str):
        """A message carrying a service / transport signature classifies True."""
        assert is_wcs_service_failure(RuntimeError(message)) is True, message

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError("boom"),
            requests.exceptions.Timeout("slow"),
            ConnectionError("dropped"),
            TimeoutError("late"),
            urllib.error.URLError("unreachable"),
            socket.gaierror("Name or service not known"),
            OSError(errno.EHOSTUNREACH, "a bare network os error"),
        ],
    )
    def test_transport_exception_types_classify_true(self, exc: BaseException):
        """A transport / network-errno exception classifies True by type."""
        assert is_wcs_service_failure(exc) is True, type(exc).__name__

    @pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 522, 511])
    def test_transient_response_status_classifies_true(self, status: int):
        """A transient HTTP status read from the response classifies True."""
        assert is_wcs_service_failure(_http_error("boom", status)) is True, status

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
    def test_non_transient_response_status_classifies_false(self, status: int):
        """A definite non-transient status is a request answer, so it is False."""
        assert is_wcs_service_failure(_http_error("boom", status)) is False, status

    @pytest.mark.parametrize("status, expected", [(503, True), (404, False)])
    def test_urllib_httperror_classified_by_its_code(self, status: int, expected: bool):
        """A urllib HTTPError is classified by its `.code`, transient or not."""
        err = urllib.error.HTTPError("http://x/wcs", status, "msg", None, None)
        assert is_wcs_service_failure(err) is expected, status

    @pytest.mark.parametrize(
        "message",
        [
            "Could not find coverage 'emodnet:mean'",
            "coverage 'foo' not listed in the server's GetCapabilities document",
            "InvalidSubsetting: Empty intersection after subsetting",
            "grid is 5000 x 5000 pixels, too large",
            "512 x 512 grid too large",
            "500 records returned from GetCoverage",
            "503 points requested, too many",
            "coverage returned 512 rows",
            "unknown band 'depth'",
            "failed to read http://server/tiles/429/data",
            "cannot connect to http://host:500/wcs",
            "",
        ],
    )
    def test_request_errors_classify_false(self, message: str):
        """A request error — a leading count, or a status in a URL — classifies False."""
        assert is_wcs_service_failure(RuntimeError(message)) is False, message

    def test_response_without_int_status_falls_through(self):
        """An HTTPError whose response has no int status_code is judged by message."""
        err = requests.exceptions.HTTPError("Service Unavailable")
        err.response = requests.Response()  # default status_code is None
        assert is_wcs_service_failure(err) is True
        plain = requests.exceptions.HTTPError("Could not find coverage")
        plain.response = requests.Response()
        assert is_wcs_service_failure(plain) is False

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

    def test_suppressed_context_is_not_walked(self):
        """A `raise ... from None` hides a transport context, so it stays False."""
        try:
            try:
                raise requests.exceptions.ConnectionError("Connection reset by peer")
            except requests.exceptions.ConnectionError:
                raise RuntimeError("bad coverage request") from None
        except RuntimeError as exc:
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
