"""Backend tests for `earthlens.erddap` (faked erddapy + HTTP, no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

from earthlens.aggregate import AggregationConfig
from earthlens.erddap import ERDDAP
from earthlens.erddap._helpers import build_constraints

pytestmark = pytest.mark.erddap

# Seeded catalog ids used across the tests.
GRIDDAP_ID = "NOAA_DHW"
TABLEDAP_ID = "cwwcNDBCMet"


def _table_backend(tmp_path: Path, variables=None) -> ERDDAP:
    """A tabledap-backed ERDDAP over a tiny bbox/window."""
    return ERDDAP(
        start="2023-01-01",
        end="2023-01-02",
        lat_lim=[36.0, 37.0],
        lon_lim=[-123.0, -122.0],
        dataset=TABLEDAP_ID,
        variables=variables or ["station", "time", "WTMP"],
        path=str(tmp_path),
    )


def _grid_backend(tmp_path: Path, variables=None) -> ERDDAP:
    """A griddap-backed ERDDAP over a tiny bbox/window."""
    return ERDDAP(
        start="2023-06-01",
        end="2023-06-01",
        lat_lim=[0.0, 1.0],
        lon_lim=[150.0, 151.0],
        dataset=GRIDDAP_ID,
        variables=variables or ["CRW_SSTANOMALY"],
        path=str(tmp_path),
    )


class TestConstructionAndOutputKind:
    """Per-instance OUTPUT_KIND and construction guards."""

    def test_tabledap_is_tabular(self, tmp_path):
        """A tabledap dataset resolves OUTPUT_KIND to 'tabular'."""
        assert _table_backend(tmp_path).OUTPUT_KIND == "tabular"

    def test_griddap_is_raster(self, tmp_path):
        """A griddap dataset resolves OUTPUT_KIND to 'raster'."""
        assert _grid_backend(tmp_path).OUTPUT_KIND == "raster"

    def test_empty_dataset_rejected(self, tmp_path):
        """Constructing without dataset= fails loud."""
        with pytest.raises(ValueError, match="requires dataset="):
            ERDDAP(
                start="2023-01-01",
                end="2023-01-02",
                lat_lim=[0.0, 1.0],
                lon_lim=[0.0, 1.0],
                path=str(tmp_path),
            )

    def test_unknown_dataset_did_you_mean(self, tmp_path):
        """An unknown dataset id raises the catalog did-you-mean error."""
        with pytest.raises(ValueError, match="ERDDAP catalog"):
            ERDDAP(
                start="2023-01-01",
                end="2023-01-02",
                lat_lim=[0.0, 1.0],
                lon_lim=[0.0, 1.0],
                dataset="NOAA_DHX",
                path=str(tmp_path),
            )

    def test_invalid_output_format_rejected(self, tmp_path):
        """An out-of-domain output_format fails at construction."""
        with pytest.raises(ValueError, match="output_format must be one of"):
            ERDDAP(
                start="2023-01-01",
                end="2023-01-02",
                lat_lim=[0.0, 1.0],
                lon_lim=[0.0, 1.0],
                dataset=TABLEDAP_ID,
                output_format="feather",
                path=str(tmp_path),
            )

    def test_variables_mapping_rejected(self, tmp_path):
        """Passing variables as a mapping is a TypeError."""
        with pytest.raises(TypeError, match="list of variable"):
            ERDDAP(
                start="2023-01-01",
                end="2023-01-02",
                lat_lim=[0.0, 1.0],
                lon_lim=[0.0, 1.0],
                dataset=TABLEDAP_ID,
                variables={TABLEDAP_ID: ["WTMP"]},
                path=str(tmp_path),
            )

    def test_default_variables_from_catalog(self, tmp_path):
        """Omitting variables falls back to the catalog row's default set."""
        backend = ERDDAP(
            start="2023-01-01",
            end="2023-01-02",
            lat_lim=[0.0, 1.0],
            lon_lim=[150.0, 151.0],
            dataset=GRIDDAP_ID,
            path=str(tmp_path),
        )
        assert list(backend.vars) == ["CRW_SSTANOMALY", "CRW_DHW"]


class TestTabledap:
    """The tabledap (to_pandas) realisation path."""

    def test_download_returns_dataframe(self, tmp_path, fake_erddapy):
        """download() returns the frame and wires the erddapy request."""
        fake_erddapy.frame = pd.DataFrame({"station": ["46042"], "WTMP": [12.3]})
        backend = _table_backend(tmp_path)
        result = backend.download()

        assert isinstance(result, pd.DataFrame)
        assert list(result["station"]) == ["46042"]
        client = fake_erddapy.last
        assert client.server.endswith("/erddap")
        assert client.protocol == "tabledap"
        assert client.dataset_id == TABLEDAP_ID
        assert client.variables == ["station", "time", "WTMP"]
        assert client.constraints == build_constraints(
            backend.space, backend.time, "tabledap"
        )

    def test_download_writes_csv(self, tmp_path, fake_erddapy):
        """The frame is also written to disk as CSV."""
        fake_erddapy.frame = pd.DataFrame({"station": ["46042"], "WTMP": [12.3]})
        _table_backend(tmp_path).download()
        assert (tmp_path / f"{TABLEDAP_ID}.csv").is_file()

    def test_download_writes_parquet(self, tmp_path, fake_erddapy, monkeypatch):
        """output_format='parquet' writes a .parquet file."""
        fake_erddapy.frame = pd.DataFrame({"station": ["46042"], "WTMP": [12.3]})
        written: dict = {}
        monkeypatch.setattr(
            pd.DataFrame,
            "to_parquet",
            lambda self, path, index=True: written.update(path=path),
        )
        backend = _table_backend(tmp_path)
        backend._output_format = "parquet"
        backend.download()
        assert str(written["path"]).endswith(f"{TABLEDAP_ID}.parquet")

    def test_aggregate_rejected_for_tabledap(self, tmp_path, fake_erddapy):
        """aggregate= on a tabledap dataset raises NotImplementedError."""
        fake_erddapy.frame = pd.DataFrame({"station": ["46042"]})
        backend = _table_backend(tmp_path)
        with pytest.raises(NotImplementedError, match="tabledap"):
            backend.download(aggregate=AggregationConfig(freq="1D"))

    def test_empty_result_returns_canonical_frame(self, tmp_path, fake_erddapy):
        """A no-match 404 yields an empty frame whose warning echoes the extent."""
        fake_erddapy.error = requests.exceptions.HTTPError(
            "Error 404: Your query produced no matching results."
        )
        backend = _table_backend(tmp_path, variables=["time", "WTMP"])
        # The warning echoes the bbox so a typo'd extent is debuggable.
        with pytest.warns(UserWarning, match=r"matched no rows over bbox \[-123.0"):
            result = backend.download()
        assert list(result.columns) == ["time", "WTMP"]
        assert len(result) == 0

    def test_other_http_error_propagates(self, tmp_path, fake_erddapy):
        """A non-empty HTTP error is not swallowed."""
        fake_erddapy.error = requests.exceptions.HTTPError("Error 500: boom")
        with pytest.raises(requests.exceptions.HTTPError, match="500"):
            _table_backend(tmp_path).download()


class TestGriddap:
    """The griddap (direct URL + requests) realisation path."""

    def test_download_returns_paths(self, tmp_path, fake_nc_get):
        """download() GETs the OPeNDAP URL and returns the written .nc path."""
        backend = _grid_backend(tmp_path)
        result = backend.download()

        from .conftest import FAKE_NETCDF_BYTES

        assert isinstance(result, list)
        assert result == [tmp_path / f"{GRIDDAP_ID}.nc"]
        assert result[0].read_bytes() == FAKE_NETCDF_BYTES
        assert len(fake_nc_get) == 1
        url = fake_nc_get[0]
        assert f"/griddap/{GRIDDAP_ID}.nc?CRW_SSTANOMALY" in url
        assert "[(2023-06-01T00:00:00Z):1:(2023-06-01T00:00:00Z)]" in url

    def test_no_xarray_import_on_griddap(self, tmp_path, fake_nc_get, monkeypatch):
        """A griddap fetch never imports xarray."""
        monkeypatch.delitem(sys.modules, "xarray", raising=False)
        _grid_backend(tmp_path).download()
        assert "xarray" not in sys.modules

    def test_aggregate_routes_through_pyramids(
        self, tmp_path, fake_nc_get, monkeypatch
    ):
        """aggregate= reduces the downloaded .nc via aggregate_netcdf."""
        seen = {}

        def _fake_aggregate(nc_path, var_info, config):
            seen["nc_path"] = nc_path
            seen["nc_variable"] = var_info.nc_variable
            seen["is_flux"] = var_info.is_flux
            seen["out_dir"] = config.out_dir
            return [("2023-06-01", None, tmp_path / "agg.tif")]

        monkeypatch.setattr("earthlens.aggregate.aggregate_netcdf", _fake_aggregate)
        backend = _grid_backend(tmp_path)
        result = backend.download(aggregate=AggregationConfig(freq="1D"))

        assert result == [tmp_path / "agg.tif"]
        assert seen["nc_path"] == tmp_path / f"{GRIDDAP_ID}.nc"
        assert seen["nc_variable"] == "CRW_SSTANOMALY"
        assert seen["is_flux"] is False  # a state field reduces by mean
        assert seen["out_dir"] == tmp_path / "aggregated"

    def test_aggregate_marks_flux_variable(self, tmp_path, fake_nc_get, monkeypatch):
        """A catalog flux_variables member is marked is_flux=True for the reducer."""
        seen = {}

        def _fake_aggregate(nc_path, var_info, config):
            seen[var_info.nc_variable] = var_info.is_flux
            return [("2023-06-01", None, tmp_path / f"{var_info.nc_variable}.tif")]

        monkeypatch.setattr("earthlens.aggregate.aggregate_netcdf", _fake_aggregate)
        backend = _grid_backend(tmp_path)
        # Curate the resolved row so one variable is a flux (frozen → copy).
        backend._dataset = backend._dataset.model_copy(
            update={"flux_variables": ["CRW_SSTANOMALY"]}
        )
        backend.download(aggregate=AggregationConfig(freq="1D"))
        assert seen["CRW_SSTANOMALY"] is True

    def test_aggregate_respects_explicit_out_dir(
        self, tmp_path, fake_nc_get, monkeypatch
    ):
        """An aggregate config with an explicit out_dir is used as-is."""
        seen = {}

        def _fake_aggregate(nc_path, var_info, config):
            seen["out_dir"] = config.out_dir
            return [("2023-06-01", None, tmp_path / "agg.tif")]

        monkeypatch.setattr("earthlens.aggregate.aggregate_netcdf", _fake_aggregate)
        custom = tmp_path / "custom_out"
        _grid_backend(tmp_path).download(
            aggregate=AggregationConfig(freq="1D", out_dir=custom)
        )
        assert seen["out_dir"] == custom

    def test_griddap_http_error_becomes_valueerror(self, tmp_path, monkeypatch):
        """An out-of-coverage griddap response surfaces as a clear ValueError."""
        from .conftest import FakeResponse

        def _get(url, **kwargs):
            return FakeResponse(
                error=requests.exceptions.HTTPError("Error 404: out of range")
            )

        monkeypatch.setattr("earthlens.erddap.backend.requests.get", _get)
        with pytest.raises(ValueError, match="outside the dataset's coverage"):
            _grid_backend(tmp_path).download()

    def test_griddap_non_netcdf_body_rejected(self, tmp_path, monkeypatch):
        """A 200 response with an HTML error body is rejected, not written."""
        from .conftest import FakeResponse

        def _get(url, **kwargs):
            return FakeResponse(content=b"<html><body>Resource not found</body></html>")

        monkeypatch.setattr("earthlens.erddap.backend.requests.get", _get)
        backend = _grid_backend(tmp_path)
        with pytest.raises(ValueError, match="non-NetCDF body"):
            backend.download()
        assert not (tmp_path / f"{GRIDDAP_ID}.nc").exists()


class _NoBufferResponse:
    """Streams its body, and fails loudly if anything reads `.content`."""

    def __init__(self, body: bytes):
        self._body = body
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.closed = False

    @property
    def content(self) -> bytes:
        raise AssertionError("the body must be streamed, not buffered via .content")

    def raise_for_status(self) -> None:
        """A 200 needs no action."""

    def iter_content(self, chunk_size=None):
        """Yield the body in small blocks, as a streamed response does."""
        for start in range(0, len(self._body), 8):
            yield self._body[start : start + 8]

    def close(self) -> None:
        """Record release of the response."""
        self.closed = True


class TestGriddapStreams:
    """The griddap body reaches disk without being buffered in memory."""

    def test_download_never_touches_response_content(self, tmp_path, monkeypatch):
        """A regression to `response.content` fails this test."""
        from .conftest import FAKE_NETCDF_BYTES

        responses: list[_NoBufferResponse] = []

        def _get(url: str, **kwargs):
            response = _NoBufferResponse(FAKE_NETCDF_BYTES)
            responses.append(response)
            return response

        monkeypatch.setattr("earthlens.erddap.backend.requests.get", _get)
        result = _grid_backend(tmp_path).download()
        assert result[0].read_bytes() == FAKE_NETCDF_BYTES
        assert responses[0].closed, "the streamed response must be released"

    def test_download_requests_a_streaming_get(self, tmp_path, monkeypatch):
        """The GET is issued with stream=True so blocks arrive incrementally."""
        from .conftest import FAKE_NETCDF_BYTES

        seen: list[dict] = []

        def _get(url: str, **kwargs):
            seen.append(kwargs)
            return _NoBufferResponse(FAKE_NETCDF_BYTES)

        monkeypatch.setattr("earthlens.erddap.backend.requests.get", _get)
        _grid_backend(tmp_path).download()
        assert seen[0].get("stream") is True, f"got {seen[0]}"

    def test_non_netcdf_body_is_rejected_and_not_written(self, tmp_path, monkeypatch):
        """An HTML error page served with a 200 raises and leaves no .nc."""

        def _get(url: str, **kwargs):
            return _NoBufferResponse(b"<html>ERDDAP error</html>")

        monkeypatch.setattr("earthlens.erddap.backend.requests.get", _get)
        with pytest.raises(ValueError, match="non-NetCDF body"):
            _grid_backend(tmp_path).download()
        assert list(tmp_path.glob("*.nc")) == [], "an error page must not be written"
        assert list(tmp_path.glob("*.part")) == [], "no temp may be left behind"
