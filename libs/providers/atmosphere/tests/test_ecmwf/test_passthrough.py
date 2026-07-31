"""Unit tests for the ECMWF raw-request passthrough (the coverage lever).

Covers passthrough construction, endpoint auto-resolution, output naming, the
retrieve routing, the licence-error mapping, and the zip post-processing — all
offline (the cdsapi client is faked).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.unit]

_EAC4 = "cams-global-reanalysis-eac4"
_EAC4_REQUEST = {
    "variable": ["2m_temperature"],
    "date": ["2023-01-01/2023-01-01"],
    "time": ["00:00"],
    "data_format": "netcdf_zip",
}


class _RecordingClient:
    """Fake cdsapi client that records retrieves and writes a payload file."""

    def __init__(self, payload: bytes = b"payload"):
        self.calls: list[tuple] = []
        self._payload = payload

    def retrieve(self, dataset, request, target):
        self.calls.append((dataset, dict(request), target))
        Path(target).write_bytes(self._payload)


class _LicenceRejectingClient:
    """Fake client whose retrieve fails as an unaccepted licence would."""

    def retrieve(self, dataset, request, target):
        raise RuntimeError("required licences not accepted")


def _passthrough(tmp_path, dataset=_EAC4, request=None, endpoint=None):
    """Build a passthrough ECMWF instance (no typed args)."""
    return ECMWF(
        dataset=dataset,
        request=request if request is not None else dict(_EAC4_REQUEST),
        endpoint=endpoint,
        path=str(tmp_path),
    )


class TestPassthroughConstruction:
    """Passthrough construction skips the typed catalog/date/grid machinery."""

    def test_constructs_without_typed_args(self, tmp_path):
        """`dataset=`+`request=` builds a passthrough instance with no bbox/dates."""
        backend = _passthrough(tmp_path)
        assert backend._passthrough is not None
        assert backend._passthrough["dataset"] == _EAC4

    def test_request_without_dataset_is_rejected(self, tmp_path):
        """`request=` with no `dataset=` raises a clear error."""
        with pytest.raises(ValueError, match="dataset"):
            ECMWF(request={"variable": ["x"]}, path=str(tmp_path))

    def test_typed_mode_still_requires_standard_args(self, tmp_path):
        """Typed construction with a missing arg names what is missing."""
        with pytest.raises(ValueError, match="lat_lim"):
            ECMWF(start="2022-01-01", end="2022-01-01", path=str(tmp_path))


class TestEndpointResolution:
    """`_resolve_endpoint` picks the store from the row, index, then default."""

    def test_curated_row_endpoint_wins(self, tmp_path):
        """A curated dataset resolves to its row's endpoint."""
        backend = _passthrough(tmp_path)
        assert backend._resolve_endpoint("reanalysis-era5-single-levels") == "cds"

    def test_uncurated_id_uses_per_store_index(self, tmp_path):
        """An uncurated ADS id resolves via the per-store availability index."""
        backend = _passthrough(tmp_path)
        assert backend._resolve_endpoint(_EAC4) == "ads"

    def test_unknown_id_defaults_to_cds(self, tmp_path):
        """An id in no store defaults to cds."""
        backend = _passthrough(tmp_path)
        assert backend._resolve_endpoint("not-a-real-dataset") == "cds"


class TestTargetNaming:
    """`_passthrough_target` picks the suffix from the request format."""

    @pytest.mark.parametrize(
        "request_fmt, suffix",
        [
            ({"data_format": "netcdf"}, ".nc"),
            ({"data_format": "netcdf_zip"}, ".zip"),
            ({"download_format": "zip"}, ".zip"),
            ({"data_format": "grib"}, ".grib"),
            ({"data_format": "grib2"}, ".grib"),
            ({}, ".bin"),
        ],
    )
    def test_suffix_from_format(self, tmp_path, request_fmt, suffix):
        """Each format hint maps to the right output suffix."""
        backend = _passthrough(tmp_path)
        assert backend._passthrough_target("ds", request_fmt) == f"ds{suffix}"


class TestPassthroughRetrieve:
    """`download()` routes a raw retrieve through the resolved endpoint."""

    def test_routes_to_resolved_endpoint_and_returns_path(self, tmp_path):
        """A passthrough retrieves via the store the id belongs to."""
        backend = _passthrough(tmp_path)
        captured: list[str] = []
        client = _RecordingClient()
        backend._client_for = lambda endpoint: captured.append(endpoint) or client
        out = backend.download()
        assert captured == ["ads"], "EAC4 routes to ADS"
        assert client.calls[0][0] == _EAC4
        assert client.calls[0][1] == _EAC4_REQUEST
        assert out == [tmp_path.resolve() / f"{_EAC4}.zip"]

    def test_explicit_endpoint_overrides_resolution(self, tmp_path):
        """An explicit `endpoint=` is used verbatim."""
        backend = _passthrough(tmp_path, dataset="whatever", endpoint="ewds")
        captured: list[str] = []
        backend._client_for = lambda endpoint: captured.append(endpoint) or _RecordingClient()
        backend.download()
        assert captured == ["ewds"]

    def test_aggregate_is_rejected(self, tmp_path):
        """A passthrough cannot aggregate (no curated Variable)."""
        from earthlens.aggregate import AggregationConfig

        backend = _passthrough(tmp_path)
        backend._client_for = lambda endpoint: _RecordingClient()
        with pytest.raises(NotImplementedError, match="passthrough"):
            backend.download(aggregate=AggregationConfig(freq="1D"))

    def test_licence_error_maps_to_permission_error(self, tmp_path):
        """An unaccepted licence surfaces as a PermissionError naming the page."""
        backend = _passthrough(tmp_path)
        backend._client_for = lambda endpoint: _LicenceRejectingClient()
        with pytest.raises(PermissionError, match=f"datasets/{_EAC4}"):
            backend.download()


class TestPostprocess:
    """`_passthrough_postprocess` unwraps single-member NetCDF zips only."""

    def test_single_member_zip_is_unwrapped(self, tmp_path):
        """A one-member `.nc` zip is replaced in place by the member's bytes."""
        target = tmp_path / "ds.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("inner.nc", b"CDF-netcdf-bytes")
        backend = _passthrough(tmp_path)
        backend._passthrough_postprocess(target)
        assert not zipfile.is_zipfile(target), "zip replaced by its member"
        assert target.read_bytes() == b"CDF-netcdf-bytes"

    def test_multi_member_zip_left_raw(self, tmp_path):
        """A multi-member archive is left untouched (C3 handles it)."""
        target = tmp_path / "ds.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("a.nc", b"a")
            archive.writestr("b.nc", b"b")
        backend = _passthrough(tmp_path)
        backend._passthrough_postprocess(target)
        assert zipfile.is_zipfile(target), "multi-member archive left raw"
