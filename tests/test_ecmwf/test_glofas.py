"""Unit tests for the GloFAS (EWDS) catalog row, request shaping, and grid snap.

Covers the `cems-glofas-forecast` catalog metadata, the `glofas` request kind
that drops the `time` slots, the per-dataset grid-resolution snap (0.05° for
GloFAS vs 0.125° for regular CDS datasets), and that `_api` routes by the
variable's endpoint. No network.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.ecmwf import Catalog, Variable
from earthlens.ecmwf import constraints as constraints_mod
from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.unit]

_GLOFAS = "cems-glofas-forecast"
_GLOFAS_CODE = "river-discharge-in-the-last-24-hours"

_CAPTURED_URL: dict[str, str] = {}


class _FakeConstraintsResponse:
    """Minimal urlopen context manager returning an empty constraints list."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"[]"


def _capturing_urlopen(url, timeout=15):
    """Record the constraints URL and return an empty document."""
    _CAPTURED_URL["url"] = url
    return _FakeConstraintsResponse()


def _glofas_backend(tmp_path):
    """Build a GloFAS-only `ECMWF` (offline; the client is never opened)."""
    return ECMWF(
        start="2026-07-01",
        end="2026-07-01",
        variables={_GLOFAS: [_GLOFAS_CODE]},
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        temporal_resolution="daily",
        path=str(tmp_path),
    )


class TestGlofasCatalog:
    """The GloFAS forecast row loads with the right EWDS metadata."""

    def test_dataset_present_with_ewds_endpoint(self):
        """`cems-glofas-forecast` loads with endpoint ewds and grid 0.05."""
        dataset = Catalog().datasets[_GLOFAS]
        assert dataset.endpoint == "ewds"
        assert dataset.grid_resolution == 0.05
        assert dataset.request_kind == "glofas"

    def test_provider_is_copernicus_cems(self):
        """GloFAS maps to the copernicus-cems provider, not plain ecmwf."""
        assert Catalog().datasets[_GLOFAS].provider == "copernicus-cems"

    def test_variable_inherits_endpoint_and_grid(self):
        """The variable inherits the dataset's endpoint and grid resolution."""
        var = Catalog().get_variable(_GLOFAS, _GLOFAS_CODE)
        assert var.endpoint == "ewds"
        assert var.grid_resolution == 0.05
        assert var.nc_variable == "dis24"


class TestCatalogFieldValidation:
    """`endpoint` and `grid_resolution` are validated at catalog-load time."""

    def test_unknown_endpoint_rejected(self):
        """An endpoint slug the router doesn't know fails validation."""
        with pytest.raises(ValidationError, match="unknown endpoint"):
            Variable(
                cds_dataset="x",
                cds_variable="x",
                nc_variable="x",
                units="m",
                product_type=["p"],
                endpoint="edws",
            )

    def test_nonpositive_grid_resolution_rejected(self):
        """A zero/negative grid resolution (would divide the snap by zero) is rejected."""
        with pytest.raises(ValidationError, match="grid_resolution must be > 0"):
            Variable(
                cds_dataset="x",
                cds_variable="x",
                nc_variable="x",
                units="m",
                product_type=["p"],
                grid_resolution=0,
            )


class TestGlofasRequest:
    """`_build_request` shapes a valid GloFAS request."""

    def test_drops_time_and_keeps_day(self, tmp_path):
        """The glofas request kind strips `time` but keeps the `day` selector."""
        backend = _glofas_backend(tmp_path)
        request = backend._build_request(Catalog().get_variable(_GLOFAS, _GLOFAS_CODE))
        assert "time" not in request
        assert "day" in request

    def test_carries_glofas_selectors_as_netcdf(self, tmp_path):
        """The request carries the GloFAS extras and requests netcdf output."""
        backend = _glofas_backend(tmp_path)
        request = backend._build_request(Catalog().get_variable(_GLOFAS, _GLOFAS_CODE))
        assert request["product_type"] == ["control_forecast"]
        assert request["system_version"] == ["operational"]
        assert request["hydrological_model"] == ["lisflood"]
        assert request["leadtime_hour"] == ["24"]
        assert request["data_format"] == "netcdf"
        assert request["download_format"] == "unarchived"
        assert request["variable"] == ["river_discharge_in_the_last_24_hours"]

    def test_monthly_glofas_request_is_rejected(self, tmp_path):
        """Requesting GloFAS monthly raises a clear error (the day selector is required)."""
        backend = ECMWF(
            start="2026-07-01",
            end="2026-07-01",
            variables={_GLOFAS: [_GLOFAS_CODE]},
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            temporal_resolution="monthly",
            path=str(tmp_path),
        )
        with pytest.raises(ValueError, match="temporal_resolution='daily'"):
            backend._build_request(Catalog().get_variable(_GLOFAS, _GLOFAS_CODE))


class TestGridResolution:
    """`_create_grid` snaps to the request's native resolution."""

    def test_glofas_snaps_to_native_005(self, tmp_path):
        """A GloFAS request snaps the bbox to the native 0.05° grid."""
        assert _glofas_backend(tmp_path).space.resolution == 0.05

    def test_era5_unchanged_at_0125(self, tmp_path):
        """A regular ERA5 request keeps the historic 0.125° snap."""
        backend = ECMWF(
            start="2022-01-01",
            end="2022-01-01",
            variables={"reanalysis-era5-single-levels": ["2m-temperature"]},
            lat_lim=[4.19, 4.64],
            lon_lim=[-75.65, -74.73],
            path=str(tmp_path),
        )
        assert backend.space.resolution == 0.125

    def test_mixed_request_snaps_to_finest(self, tmp_path):
        """A request mixing GloFAS and ERA5 snaps to the finest (0.05°)."""
        backend = ECMWF(
            start="2026-07-01",
            end="2026-07-01",
            variables={
                _GLOFAS: [_GLOFAS_CODE],
                "reanalysis-era5-single-levels": ["2m-temperature"],
            },
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            temporal_resolution="daily",
            path=str(tmp_path),
        )
        assert backend.space.resolution == 0.05

    def test_resolver_defaults_without_vars(self):
        """With no `vars` (bare instance) the resolver falls back to ERA5's."""
        bare = ECMWF.__new__(ECMWF)
        assert bare._grid_resolution_for_request() == pytest.approx(0.125)

    def test_resolver_falls_back_when_catalog_fails(self, tmp_path, monkeypatch):
        """A catalog-load failure must not break grid snapping."""
        backend = _glofas_backend(tmp_path)
        import earthlens.ecmwf.backend as backend_module

        def _boom():
            raise RuntimeError("catalog unreadable")

        monkeypatch.setattr(backend_module, "Catalog", _boom)
        assert backend._grid_resolution_for_request() == pytest.approx(0.125)


class TestGlofasConstraints:
    """EWDS constraints are fetched from the EWDS host, not the CDS host."""

    def test_constraints_url_uses_ewds_host(self, monkeypatch):
        """A GloFAS constraints fetch targets the EWDS catalogue, not CDS."""
        _CAPTURED_URL.clear()
        constraints_mod._CACHE.clear()
        monkeypatch.setattr(
            constraints_mod.urllib.request, "urlopen", _capturing_urlopen
        )
        constraints_mod.fetch_constraints(
            _GLOFAS, base_url="https://ewds.climate.copernicus.eu/api"
        )
        assert _CAPTURED_URL["url"] == (
            "https://ewds.climate.copernicus.eu/api/catalogue/v1/collections/"
            f"{_GLOFAS}/constraints.json"
        )


class TestApiRoutesByEndpoint:
    """`_api` selects the client for the variable's endpoint."""

    def test_api_requests_client_for_variable_endpoint(self, ecmwf_stub):
        """`_api` asks `_client_for` for the variable's own endpoint (ewds)."""
        glofas = Variable(
            cds_dataset=_GLOFAS,
            cds_variable="river_discharge_in_the_last_24_hours",
            nc_variable="dis24",
            units="m3 s-1",
            product_type=["control_forecast"],
            request_kind="glofas",
            endpoint="ewds",
            extras={"leadtime_hour": ["24"]},
        )
        captured: list[str] = []
        real_client = ecmwf_stub.client
        ecmwf_stub._client_for = lambda endpoint: captured.append(endpoint) or real_client
        ecmwf_stub._api(glofas)
        assert captured == ["ewds"]
        assert real_client.retrieve.call_count == 1

    def test_licence_rejection_names_the_ewds_dataset_page(self, ecmwf_stub):
        """A licence-not-accepted error links to the EWDS dataset page, not CDS."""
        glofas = Variable(
            cds_dataset=_GLOFAS,
            cds_variable="river_discharge_in_the_last_24_hours",
            nc_variable="dis24",
            units="m3 s-1",
            product_type=["control_forecast"],
            request_kind="glofas",
            endpoint="ewds",
            extras={"leadtime_hour": ["24"]},
        )
        ecmwf_stub.client.retrieve.side_effect = Exception(
            "403 required licences not accepted"
        )
        with pytest.raises(PermissionError) as excinfo:
            ecmwf_stub._api(glofas)
        assert f"ewds.climate.copernicus.eu/datasets/{_GLOFAS}" in str(excinfo.value)
