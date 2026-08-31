"""Unit tests for the bathymetry WCS transport (faked pyramids from_wcs)."""

from __future__ import annotations

from pathlib import Path

import pyramids.dataset as pyramids_dataset
import pytest
import requests

from earthlens.bathymetry import WcsServiceUnavailableError
from earthlens.bathymetry import backend as backend_module
from earthlens.bathymetry.backend import Bathymetry
from earthlens.bathymetry.catalog import Dataset

pytestmark = pytest.mark.bathymetry

#: A North Sea AOI well inside the EMODnet coverage (lat 53..55, lon 2..4).
_NORTH_SEA = {"lat_lim": [53.0, 55.0], "lon_lim": [2.0, 4.0]}


class _FakeWcsDataset:
    """Stand-in for the Dataset from `from_wcs`: records crop, writes a stub tif."""

    def __init__(self, recorder: dict):
        self._recorder = recorder

    def crop(self, mask=None, touch: bool = True):
        """Record the polygon-mask call and return the (masked) dataset."""
        self._recorder["masked"] = {"mask": mask, "touch": touch}
        return self

    def to_file(self, path: str) -> None:
        """Write a tiny stub GeoTIFF and record the destination."""
        Path(path).write_bytes(b"II*\x00stub-geotiff")
        self._recorder.setdefault("written", []).append(path)


@pytest.fixture
def fake_from_wcs(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch pyramids `Dataset.from_wcs` to record its call and return a stub."""
    recorder: dict = {}

    def _fake(endpoint, *, coverage, bbox, crs="EPSG:4326", version=None, **kwargs):
        recorder["call"] = {
            "endpoint": endpoint,
            "coverage": coverage,
            "bbox": bbox,
            "crs": crs,
            "version": version,
            "timeout": kwargs.get("timeout"),
        }
        return _FakeWcsDataset(recorder)

    monkeypatch.setattr(pyramids_dataset.Dataset, "from_wcs", staticmethod(_fake))
    return recorder


def _make(dataset: str, tmp_path: Path, **kwargs) -> Bathymetry:
    """Construct an EMODnet-region Bathymetry writing under tmp_path."""
    params = dict(_NORTH_SEA)
    params.update(kwargs)
    return Bathymetry(dataset=dataset, path=tmp_path, **params)


def test_emodnet_download_calls_from_wcs_and_writes(
    tmp_path: Path, fake_from_wcs: dict
):
    """An EMODnet request routes through from_wcs and returns a .tif path."""
    result = _make("emodnet", tmp_path).download()
    call = fake_from_wcs["call"]
    assert call["endpoint"] == "https://ows.emodnet-bathymetry.eu/wcs"
    assert call["coverage"] == "emodnet:mean"
    assert call["bbox"] == (2.0, 53.0, 4.0, 55.0)
    assert call["crs"] == "EPSG:4326"
    assert call["version"] == "1.0.0"
    assert call["timeout"] == 120.0
    assert result == [tmp_path.absolute() / "emodnet.tif"]
    assert result[0].exists()


def test_release_variant_uses_year_coverage(tmp_path: Path, fake_from_wcs: dict):
    """A year-stamped release row requests its own colon coverage id."""
    _make("emodnet_2020", tmp_path).download()
    assert fake_from_wcs["call"]["coverage"] == "emodnet:mean_2020"


def test_bbox_only_request_is_not_masked(tmp_path: Path, fake_from_wcs: dict):
    """A plain bbox request trusts the WCS subset — no client-side mask."""
    _make("emodnet", tmp_path).download()
    assert "masked" not in fake_from_wcs
    assert fake_from_wcs["written"], "the dataset was still written"


def test_polygon_aoi_masks_before_write(tmp_path: Path, fake_from_wcs: dict):
    """A polygon aoi= is honoured via crop(mask=) before the write."""
    wkt = "POLYGON ((2 53, 4 53, 4 55, 2 55, 2 53))"
    Bathymetry(dataset="emodnet", aoi=wkt, path=tmp_path).download()
    masked = fake_from_wcs.get("masked")
    assert masked is not None, "crop(mask=) should be applied for a polygon aoi"
    assert masked["mask"] is not None, "a polygon mask must be passed to crop()"
    assert masked["touch"] is True, "touch=True should be forwarded to crop()"


def test_out_of_domain_raises_before_request(tmp_path: Path, fake_from_wcs: dict):
    """A mid-Pacific AOI is rejected by the domain guard before any request."""
    backend = Bathymetry(
        dataset="emodnet",
        lat_lim=[-5.0, -3.0],
        lon_lim=[-140.0, -138.0],
        path=tmp_path,
    )
    with pytest.raises(ValueError, match="outside the 'emodnet' coverage"):
        backend.download()
    assert "call" not in fake_from_wcs, "from_wcs must not be called out of domain"


def test_out_of_domain_message_points_at_global_dems(
    tmp_path: Path, fake_from_wcs: dict
):
    """The out-of-domain error names the global GEBCO / ETOPO fallback."""
    backend = Bathymetry(
        dataset="emodnet",
        lat_lim=[-5.0, -3.0],
        lon_lim=[-140.0, -138.0],
        path=tmp_path,
    )
    with pytest.raises(ValueError, match="gebco_2020"):
        backend.download()


def test_partial_overlap_warns_but_proceeds(
    tmp_path: Path, fake_from_wcs: dict, monkeypatch: pytest.MonkeyPatch
):
    """An AOI straddling the coverage edge warns about zero-fill but still fetches."""
    seen: list[str] = []
    monkeypatch.setattr(backend_module.logger, "warning", seen.append)
    # East edge of the EMODnet extent is 43.0; this AOI pokes past it to 45.0.
    Bathymetry(
        dataset="emodnet",
        lat_lim=[40.0, 42.0],
        lon_lim=[42.0, 45.0],
        path=tmp_path,
    ).download()
    assert any("extends beyond the coverage extent" in message for message in seen)
    assert "call" in fake_from_wcs, "a partial-overlap AOI must still fetch"


def test_fully_contained_aoi_does_not_warn(
    tmp_path: Path, fake_from_wcs: dict, monkeypatch: pytest.MonkeyPatch
):
    """A fully in-coverage AOI fetches with no out-of-domain warning."""
    seen: list[str] = []
    monkeypatch.setattr(backend_module.logger, "warning", seen.append)
    _make("emodnet", tmp_path).download()
    assert not any("extends beyond" in message for message in seen)


def test_http_client_is_built_once_and_reused(tmp_path: Path, fake_from_wcs: dict):
    """The pooled HttpClient is created once and reused across calls."""
    backend = _make("emodnet", tmp_path)
    first = backend._client()
    second = backend._client()
    assert first is second, "the client must be cached on the instance, not rebuilt"


def test_domain_guard_noops_without_native_bbox(tmp_path: Path, fake_from_wcs: dict):
    """The domain guard is a no-op for a row that declares no native_bbox."""
    backend = _make("emodnet", tmp_path)
    griddap_row = backend._catalog.get("gebco_2020")
    assert griddap_row.native_bbox is None
    # A row with no advertised extent cannot be guarded — this must not raise.
    backend._guard_wcs_domain(griddap_row, (0.0, 0.0, 1.0, 1.0))


def test_non_wgs84_row_skips_numeric_guard(tmp_path: Path, fake_from_wcs: dict):
    """A wcs row in a projected CRS skips the lon/lat guard rather than mis-reject."""
    projected = Dataset(
        id="proj",
        transport="wcs",
        endpoint="https://x/wcs",
        dataset_id="proj:mean",
        variable="elevation",
        wcs_version="1.0.0",
        crs="EPSG:3857",
        native_bbox=(0.0, 0.0, 1.0, 1.0),
    )
    backend = _make("emodnet", tmp_path)
    # This bbox is far outside native_bbox and would be rejected under EPSG:4326;
    # a projected-CRS row must skip the numeric comparison and not raise.
    backend._guard_wcs_domain(projected, (100.0, 100.0, 200.0, 200.0))


def test_request_error_is_wrapped_as_valueerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A non-service from_wcs failure surfaces as a clear ValueError."""

    def _boom(endpoint, *, coverage, bbox, **kwargs):
        raise RuntimeError("Empty intersection after subsetting")

    monkeypatch.setattr(pyramids_dataset.Dataset, "from_wcs", staticmethod(_boom))
    backend = _make("emodnet", tmp_path)
    with pytest.raises(ValueError, match="WCS request for 'emodnet'"):
        backend.download()


def test_service_failure_raises_typed_unavailable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A non-XML GetCapabilities answer raises the typed service error, not ValueError."""

    def _degraded(endpoint, *, coverage, bbox, **kwargs):
        raise RuntimeError("WCS GetCapabilities returned a non-XML body from ows...")

    monkeypatch.setattr(pyramids_dataset.Dataset, "from_wcs", staticmethod(_degraded))
    backend = _make("emodnet", tmp_path)
    with pytest.raises(WcsServiceUnavailableError, match="unavailable for 'emodnet'"):
        backend.download()


def test_connection_error_raises_typed_unavailable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A dropped connection from from_wcs raises the typed service error."""

    def _dropped(endpoint, *, coverage, bbox, **kwargs):
        raise requests.exceptions.ConnectionError("Connection aborted")

    monkeypatch.setattr(pyramids_dataset.Dataset, "from_wcs", staticmethod(_dropped))
    backend = _make("emodnet", tmp_path)
    with pytest.raises(WcsServiceUnavailableError):
        backend.download()


def test_griddap_row_never_calls_from_wcs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A GEBCO (griddap) request never touches the WCS path."""

    def _guard(*args, **kwargs):
        raise AssertionError("from_wcs must not be called for a griddap row")

    def _no_network(*args, **kwargs):
        raise requests.exceptions.ConnectionError("offline")

    monkeypatch.setattr(pyramids_dataset.Dataset, "from_wcs", staticmethod(_guard))
    monkeypatch.setattr(backend_module.requests, "get", _no_network)
    backend = Bathymetry(
        dataset="gebco_2020",
        lat_lim=[25.0, 26.0],
        lon_lim=[-18.0, -17.0],
        path=tmp_path,
    )
    # The griddap GET is stubbed offline; the point is that from_wcs (guarded
    # above) is never reached for a griddap row.
    with pytest.raises(ValueError):
        backend.download()
