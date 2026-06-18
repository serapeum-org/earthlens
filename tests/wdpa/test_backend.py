"""Unit tests for the WDPA protected-area backend."""

from __future__ import annotations

import pytest
from geopandas import GeoDataFrame
from shapely.geometry import MultiPolygon, Polygon

from earthlens.biodiversity import LicenseWarning
from earthlens.wdpa import WDPA, AuthenticationError


def _backend(tmp_path, variables=None, token="test-token", **kwargs):
    """Build a WDPA backend over a whole-Earth bbox with a test token."""
    return WDPA(
        start="2024-01-01",
        end="2024-12-31",
        variables=variables if variables is not None else ["KEN"],
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
        token=token,
        **kwargs,
    )


@pytest.mark.wdpa
class TestConstruction:
    """Construction validates inputs and requires a token."""

    def test_output_kind_is_vector(self, tmp_path):
        """The backend declares vector output."""
        assert _backend(tmp_path).OUTPUT_KIND == "vector"

    def test_missing_token_raises(self, tmp_path, monkeypatch):
        """Constructing without a token raises naming WDPA_TOKEN."""
        monkeypatch.delenv("WDPA_TOKEN", raising=False)
        with pytest.raises(AuthenticationError, match="WDPA_TOKEN"):
            _backend(tmp_path, token=None)

    def test_dict_variables_rejected(self, tmp_path):
        """A mapping `variables` raises a clear TypeError."""
        with pytest.raises(TypeError, match="not a mapping"):
            _backend(tmp_path, variables={"KEN": 1})

    def test_empty_variables_rejected(self, tmp_path):
        """An empty `variables` raises a ValueError."""
        with pytest.raises(ValueError, match="at least one country"):
            _backend(tmp_path, variables=[])


@pytest.mark.wdpa
class TestFetchAndDownload:
    """`download` fetches protected-area polygons over the v4 REST API."""

    def test_country_request_returns_polygons(self, tmp_path, fake_wdpa):
        """A country request authenticates, fetches, and returns polygons."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        fc = _backend(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1
        assert fc.crs.to_epsg() == 4326
        assert isinstance(fc.geometry.iloc[0], (Polygon, MultiPolygon))

    def test_request_uses_query_token_and_geometry(self, tmp_path, fake_wdpa):
        """The request carries the token as a query param and with_geometry."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        _backend(tmp_path).download()
        call = fake_wdpa.state.calls[0]
        assert call["url"].startswith(
            "https://api.protectedplanet.net/v4/protected_areas/search"
        )
        assert call["params"]["token"] == "test-token"
        assert call["params"]["with_geometry"] == "true"
        assert call["params"]["country"] == "KEN"

    def test_pagination_fetches_second_page(self, tmp_path, fake_wdpa):
        """A full first page (50) triggers a second page fetch."""
        full_page = [fake_wdpa.area(wdpa_id=str(i)) for i in range(50)]
        fake_wdpa.state.set_responses(
            [
                fake_wdpa.response({"protected_areas": full_page}),
                fake_wdpa.response({"protected_areas": [fake_wdpa.area(wdpa_id="x")]}),
            ]
        )
        fc = _backend(tmp_path).download()
        assert len(fake_wdpa.state.calls) == 2
        assert len(fc) == 51

    def test_point_only_area_dropped(self, tmp_path, fake_wdpa):
        """A point-only protected area is dropped from the polygon result."""
        point = {"type": "Point", "coordinates": [0, 0]}
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area(geometry=point)]})]
        )
        assert len(_backend(tmp_path).download()) == 0

    def test_by_id_request(self, tmp_path, fake_wdpa):
        """A numeric selector fetches a single protected area by id."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_area": fake_wdpa.area(wdpa_id="555")})]
        )
        fc = _backend(tmp_path, variables=["555"]).download()
        assert fake_wdpa.state.calls[0]["url"].endswith("/protected_areas/555")
        assert len(fc) == 1

    def test_license_warning_fires(self, tmp_path, fake_wdpa):
        """Every fetch raises the UNEP-WCMC LicenseWarning."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        with pytest.warns(LicenseWarning):
            _backend(tmp_path).download()

    def test_http_401_raises_auth_error(self, tmp_path, fake_wdpa):
        """An HTTP 401 surfaces as an AuthenticationError."""
        fake_wdpa.state.set_responses([fake_wdpa.response({}, status_code=401)])
        with pytest.raises(AuthenticationError, match="401"):
            _backend(tmp_path).download()

    def test_download_writes_geoparquet(self, tmp_path, fake_wdpa):
        """A non-empty result is written to a GeoParquet file under path."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        _backend(tmp_path).download()
        assert (tmp_path / "wdpa_protected_areas.parquet").exists()

    def test_aggregate_rejected(self, tmp_path, fake_wdpa):
        """A non-None aggregate raises NotImplementedError mentioning vector."""
        with pytest.raises(NotImplementedError, match="vector"):
            _backend(tmp_path).download(aggregate=object())
