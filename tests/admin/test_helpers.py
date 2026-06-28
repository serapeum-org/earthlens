"""Unit tests for the pure admin URL-resolution and vector-read helpers."""

from __future__ import annotations

from typing import Any

import pytest

from earthlens.admin import _helpers
from earthlens.admin._helpers import (
    cgaz_url,
    empty_fc,
    geoboundaries_resolve,
    natural_earth_url,
    read_vector,
    tiger_url,
    vsicurl,
)

pytestmark = pytest.mark.admin


class _FakeResponse:
    """A minimal `requests` response stub returning a fixed JSON payload."""

    def __init__(self, payload: Any, raised: Exception | None = None):
        self._payload = payload
        self._raised = raised

    def raise_for_status(self) -> None:
        """Raise the configured error, mimicking a non-2xx status."""
        if self._raised is not None:
            raise self._raised

    def json(self) -> Any:
        """Return the canned JSON payload."""
        return self._payload


class _FakeCRS:
    """A pyproj-CRS stub whose `to_epsg` returns a fixed value."""

    def __init__(self, epsg: int | None):
        self._epsg = epsg

    def to_epsg(self, min_confidence: int | None = None) -> int | None:
        """Return the fixed EPSG code (or None)."""
        return self._epsg


class _FakeFC:
    """A FeatureCollection stub recording reproject / set-CRS calls."""

    def __init__(self, crs: _FakeCRS | None, n: int = 3):
        self._crs = crs
        self._n = n
        self.reprojected_to: Any = None
        self.set_to: Any = None

    @property
    def crs(self) -> _FakeCRS | None:
        """The current (fake) CRS."""
        return self._crs

    def __len__(self) -> int:
        return self._n

    def to_crs(self, crs: Any, inplace: bool = False) -> None:
        """Record the reproject target and flip the CRS to it."""
        self.reprojected_to = crs
        self._crs = _FakeCRS(4326)

    def set_crs(self, crs: Any, inplace: bool = False, allow_override: bool = False):
        """Record the declared CRS and flip the CRS to it."""
        self.set_to = crs
        self._crs = _FakeCRS(4326)


class _FakeReader:
    """Stands in for the `FeatureCollection` symbol; serves a preset fake FC."""

    def __init__(self, fc: _FakeFC):
        self.fc = fc
        self.calls: list[str] = []

    def read_file(self, path: str, **kwargs: Any) -> _FakeFC:
        """Record the path and return the preset fake FC."""
        self.calls.append(path)
        return self.fc


def test_vsicurl_prefixes_url():
    """vsicurl prepends the GDAL /vsicurl/ virtual-filesystem prefix."""
    assert vsicurl("https://x/y.geojson") == "/vsicurl/https://x/y.geojson"


def test_geoboundaries_resolve_dict_payload(monkeypatch):
    """A dict metadata payload yields its gjDownloadURL."""
    monkeypatch.setattr(
        _helpers.requests,
        "get",
        lambda url, timeout=60: _FakeResponse({"gjDownloadURL": "http://x/k.geojson"}),
    )
    assert geoboundaries_resolve("KEN", "ADM1") == "http://x/k.geojson"


def test_geoboundaries_resolve_list_payload(monkeypatch):
    """A list metadata payload takes the first entry's gjDownloadURL."""
    monkeypatch.setattr(
        _helpers.requests,
        "get",
        lambda url, timeout=60: _FakeResponse([{"gjDownloadURL": "http://x/0.geojson"}]),
    )
    assert geoboundaries_resolve("USA", "ADM0") == "http://x/0.geojson"


def test_geoboundaries_resolve_builds_api_url(monkeypatch):
    """The metadata GET targets the gbOpen API path for the ISO + ADM pair."""
    seen: dict[str, Any] = {}

    def _capture(url, timeout=60):
        seen["url"] = url
        seen["timeout"] = timeout
        return _FakeResponse({"gjDownloadURL": "http://x/y.geojson"})

    monkeypatch.setattr(_helpers.requests, "get", _capture)
    geoboundaries_resolve("ken", "ADM2", timeout=12.0)
    assert seen["url"] == f"{_helpers.GEOBOUNDARIES_API}/ken/ADM2/"
    assert seen["timeout"] == 12.0


def test_geoboundaries_resolve_http_error_propagates(monkeypatch):
    """A non-2xx metadata status propagates rather than being swallowed."""
    import requests

    monkeypatch.setattr(
        _helpers.requests,
        "get",
        lambda url, timeout=60: _FakeResponse(
            {}, raised=requests.HTTPError("500 Server Error")
        ),
    )
    with pytest.raises(requests.HTTPError, match="500"):
        geoboundaries_resolve("KEN", "ADM1")


def test_cgaz_url_matches_live_format():
    """cgaz_url builds the /vsicurl/ GeoPackage URL for an ADM level."""
    assert cgaz_url("ADM0") == (
        "/vsicurl/https://github.com/wmgeolab/geoBoundaries/raw/main/"
        "releaseData/CGAZ/geoBoundariesCGAZ_ADM0.gpkg"
    )


def test_natural_earth_url_matches_live_format():
    """natural_earth_url builds the /vsizip//vsicurl/ ZIP path with the scale-stamped stem."""
    assert natural_earth_url("110m", "admin_0_countries") == (
        "/vsizip//vsicurl/https://naciscdn.org/naturalearth/110m/cultural/"
        "ne_110m_admin_0_countries.zip"
    )


def test_tiger_url_nationwide():
    """tiger_url builds the nationwide cb_ path with scope 'us' by default."""
    assert tiger_url(2023, "state", "500k") == (
        "/vsizip//vsicurl/https://www2.census.gov/geo/tiger/GENZ2023/shp/"
        "cb_2023_us_state_500k.zip"
    )


def test_tiger_url_per_state_scope():
    """A per-state scope (FIPS) is written in place of 'us'."""
    assert tiger_url(2023, "tract", "500k", scope="06").endswith(
        "cb_2023_06_tract_500k.zip"
    )


def test_read_vector_keeps_4326(monkeypatch):
    """An already-4326 read is returned untouched (no reproject / set)."""
    fake = _FakeFC(_FakeCRS(4326))
    monkeypatch.setattr(_helpers, "FeatureCollection", _FakeReader(fake))
    out = read_vector("/vsicurl/http://x/y.geojson")
    assert out is fake
    assert fake.reprojected_to is None and fake.set_to is None


def test_read_vector_reprojects_known_epsg(monkeypatch):
    """A known non-4326 EPSG (TIGER NAD83) is reprojected to 4326."""
    fake = _FakeFC(_FakeCRS(4269))
    monkeypatch.setattr(_helpers, "FeatureCollection", _FakeReader(fake))
    out = read_vector("/vsizip//vsicurl/http://x/t.zip")
    assert out.reprojected_to == "EPSG:4326"
    assert out.set_to is None


def test_read_vector_declares_missing_crs(monkeypatch):
    """A file with no CRS is declared EPSG:4326 (no transform)."""
    fake = _FakeFC(None)
    monkeypatch.setattr(_helpers, "FeatureCollection", _FakeReader(fake))
    out = read_vector("local.geojson")
    assert out.set_to == "EPSG:4326"
    assert out.reprojected_to is None


def test_read_vector_declares_unmappable_geographic_crs(monkeypatch):
    """A present-but-unmappable geographic CRS (CGAZ) is declared 4326, not transformed."""
    fake = _FakeFC(_FakeCRS(None))
    monkeypatch.setattr(_helpers, "FeatureCollection", _FakeReader(fake))
    out = read_vector("/vsicurl/http://x/cgaz.gpkg")
    assert out.set_to == "EPSG:4326"
    assert out.reprojected_to is None


def test_read_vector_calls_read_file_with_path(monkeypatch):
    """read_vector reads through FeatureCollection.read_file with the given path."""
    reader = _FakeReader(_FakeFC(_FakeCRS(4326)))
    monkeypatch.setattr(_helpers, "FeatureCollection", reader)
    read_vector("/vsicurl/http://x/y.geojson")
    assert reader.calls == ["/vsicurl/http://x/y.geojson"]


def test_empty_fc_is_zero_rows_4326():
    """empty_fc returns a zero-row FeatureCollection tagged EPSG:4326."""
    fc = empty_fc()
    assert len(fc) == 0
    assert fc.crs.to_epsg() == 4326
