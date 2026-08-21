"""Tests for the FDSN ShakeMap side-output (helpers plus backend wiring).

The archive format is reproduced synthetically — a tiny ESRI float grid
plus its header, zipped the way ComCat ships one — so the whole path
runs offline.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import earthlens.fdsn.backend as fdsn_backend
from earthlens.fdsn import _helpers
from earthlens.fdsn.backend import FDSN

from .conftest import _FakeFdsn

pytestmark = pytest.mark.fdsn

_ROWS, _COLS = 4, 5


def _make_hdr() -> str:
    """Return an EHdr header matching the synthetic grid."""
    return (
        "BYTEORDER  LSBFIRST\n"
        "LAYOUT  BIL\n"
        f"NROWS  {_ROWS}\n"
        f"NCOLS  {_COLS}\n"
        "NBANDS  1\n"
        "NBITS  32\n"
        f"BANDROWBYTES  {_COLS * 4}\n"
        f"TOTALROWBYTES  {_COLS * 4}\n"
        "PIXELTYPE  FLOAT\n"
        "ULXMAP  35.2\n"
        "ULYMAP  39.5\n"
        "XDIM  0.00833333333333\n"
        "YDIM  0.00833333333333\n"
        "NODATA  999.0\n"
    )


def _make_archive(layers: tuple[str, ...] = ("mmi_mean", "pga_mean")) -> bytes:
    """Return zipped `.flt`/`.hdr` pairs for the named layers."""
    payload = np.arange(_ROWS * _COLS, dtype="<f4").tobytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for layer in layers:
            bundle.writestr(f"{layer}.flt", payload)
            bundle.writestr(f"{layer}.hdr", _make_hdr())
    return buffer.getvalue()


def _detail(url: str = "https://example.invalid/raster.zip") -> dict[str, Any]:
    """Return a minimal ComCat detail document carrying a ShakeMap."""
    return {
        "properties": {
            "products": {
                "shakemap": [{"contents": {_helpers.RASTER_CONTENT_KEY: {"url": url}}}]
            }
        }
    }


def _quakeml_id(comcat_id: str) -> str:
    """Return the QuakeML resource identifier USGS returns for an id."""
    return (
        "quakeml:earthquake.usgs.gov/fdsnws/event/1/query"
        f"?eventid={comcat_id}&format=quakeml"
    )


class _FakeHttp:
    """Stand-in for `HttpClient` serving one detail document and archive.

    Failures are injectable: `fail_json_for` / `fail_download_for` raise for
    any request whose URL mentions one of the given ComCat ids, so a
    multi-event run can fail one event and keep another healthy.
    """

    def __init__(
        self,
        detail: dict[str, Any] | None = None,
        archive: bytes | None = None,
    ) -> None:
        self.detail = _detail() if detail is None else detail
        self.archive = _make_archive() if archive is None else archive
        self.json_urls: list[str] = []
        self.downloads: list[tuple[str, Path]] = []
        self.fail_json_for: set[str] = set()
        self.fail_download_for: set[str] = set()

    def get_json(self, url: str, **_kwargs: Any) -> dict[str, Any]:
        self.json_urls.append(url)
        if any(marker in url for marker in self.fail_json_for):
            raise RuntimeError(f"ComCat detail unavailable for {url}")
        return self.detail

    def download(self, url: str, dest: str | Path, **_kwargs: Any) -> Path:
        dest = Path(dest)
        self.downloads.append((url, dest))
        if any(marker in str(dest) for marker in self.fail_download_for):
            raise RuntimeError(f"archive transfer failed for {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.archive)
        return dest


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> _FakeHttp:
    """Patch the backend's `HttpClient` with the canned fake."""
    fake = _FakeHttp()
    monkeypatch.setattr(fdsn_backend, "HttpClient", lambda **_kwargs: fake)
    return fake


def _usgs_catalog(make_event: Any, *comcat_ids: str) -> Any:
    """Build a catalog whose events carry the given ComCat ids."""
    from obspy.core.event import Catalog

    events = []
    for index, comcat_id in enumerate(comcat_ids):
        event = make_event(lon=139.0 + index, lat=35.0 + index)
        event.resource_id = _quakeml_id(comcat_id)
        events.append(event)
    return Catalog(events=events)


@pytest.fixture
def usgs_events(fake_fdsn: _FakeFdsn, make_event: Any) -> _FakeFdsn:
    """Make the fake client return an event carrying a real ComCat id.

    The shared `make_event` factory leaves obspy's auto-generated
    `smi:local/...` identifier in place, which no ComCat id can be parsed
    out of — so the ShakeMap path would be exercised only up to its first
    guard.
    """
    from obspy.core.event import Catalog

    event = make_event()
    event.resource_id = _quakeml_id("us6000jlqa")
    fake_fdsn.default_result = Catalog(events=[event])
    return fake_fdsn


def _backend(tmp_path: Path, **overrides: Any) -> FDSN:
    """Build an FDSN backend over a fixed window and box."""
    params: dict[str, Any] = dict(
        start="2024-01-01",
        end="2024-01-31",
        variables=["USGS"],
        lat_lim=[30.0, 45.0],
        lon_lim=[130.0, 145.0],
        path=str(tmp_path),
    )
    params.update(overrides)
    return FDSN(**params)


class TestParseComcatId:
    """Recovering a ComCat id from a QuakeML resource identifier."""

    def test_extracts_id(self):
        """A USGS resource identifier yields its bare ComCat id."""
        assert _helpers.parse_comcat_id(_quakeml_id("us6000jlqa")) == "us6000jlqa"

    def test_identifier_without_eventid_is_none(self):
        """An identifier carrying no eventid parameter yields None."""
        assert _helpers.parse_comcat_id("smi:ch.ethz.sed/sc3a/2024abcd") is None

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_is_none(self, value: str | None):
        """An empty or missing identifier yields None."""
        assert _helpers.parse_comcat_id(value) is None


class TestNormalizeLayers:
    """Validating and de-duplicating a requested layer selection."""

    def test_default_is_mmi_mean(self):
        """None selects macroseismic intensity."""
        assert _helpers.normalize_layers(None) == ("mmi_mean",)

    def test_preserves_order_and_dedupes(self):
        """Repeats collapse and first-seen order survives."""
        result = _helpers.normalize_layers(["pga_mean", "mmi_mean", "pga_mean"])
        assert result == ("pga_mean", "mmi_mean")

    def test_unknown_layer_raises(self):
        """An unknown grid name is refused."""
        with pytest.raises(ValueError, match="unknown ShakeMap layer"):
            _helpers.normalize_layers(["mmi_median"])

    def test_empty_selection_raises(self):
        """An explicitly empty selection is refused."""
        with pytest.raises(ValueError, match="empty"):
            _helpers.normalize_layers([])

    def test_every_advertised_layer_is_accepted(self):
        """All fourteen advertised grids validate."""
        assert _helpers.normalize_layers(_helpers.SHAKEMAP_LAYERS) == tuple(
            _helpers.SHAKEMAP_LAYERS
        )


class TestShakemapRasterUrl:
    """Walking a detail document to the raster archive URL."""

    def test_finds_url(self):
        """A document with a ShakeMap raster yields its URL."""
        assert _helpers.shakemap_raster_url(_detail()) == (
            "https://example.invalid/raster.zip"
        )

    def test_no_shakemap_product_is_none(self):
        """An event with no ShakeMap product yields None."""
        assert _helpers.shakemap_raster_url({"properties": {"products": {}}}) is None

    def test_shakemap_without_raster_is_none(self):
        """A ShakeMap shipping no raster bundle yields None."""
        detail = {"properties": {"products": {"shakemap": [{"contents": {}}]}}}
        assert _helpers.shakemap_raster_url(detail) is None

    def test_empty_document_is_none(self):
        """A document with no properties at all yields None."""
        assert _helpers.shakemap_raster_url({}) is None


class TestExtractLayers:
    """Unpacking the requested grids out of the archive."""

    def test_extracts_requested_pair(self, tmp_path: Path):
        """Each requested layer yields its .flt and .hdr on disk."""
        archive = tmp_path / "raster.zip"
        archive.write_bytes(_make_archive())
        extracted = _helpers.extract_layers(archive, ["mmi_mean"], tmp_path / "out")
        assert set(extracted) == {"mmi_mean"}
        assert extracted["mmi_mean"].is_file()
        assert (tmp_path / "out" / "mmi_mean.hdr").is_file()

    def test_missing_layer_is_skipped(self, tmp_path: Path):
        """A layer the archive lacks is skipped, not raised."""
        archive = tmp_path / "raster.zip"
        archive.write_bytes(_make_archive(layers=("mmi_mean",)))
        extracted = _helpers.extract_layers(
            archive, ["mmi_mean", "pgv_std"], tmp_path / "out"
        )
        assert set(extracted) == {"mmi_mean"}


class TestFltToGeotiff:
    """Converting one ESRI float grid to a georeferenced GeoTIFF."""

    def test_writes_georeferenced_tif(self, tmp_path: Path):
        """The GeoTIFF carries EPSG:4326 and the grid's shape."""
        from osgeo import gdal

        archive = tmp_path / "raster.zip"
        archive.write_bytes(_make_archive(layers=("mmi_mean",)))
        extracted = _helpers.extract_layers(archive, ["mmi_mean"], tmp_path)
        dest = _helpers.flt_to_geotiff(extracted["mmi_mean"], tmp_path / "mmi_mean.tif")

        assert dest.is_file()
        dataset = gdal.Open(str(dest))
        assert dataset.GetDriver().ShortName == "GTiff"
        assert dataset.RasterXSize == _COLS
        assert dataset.RasterYSize == _ROWS
        assert "4326" in dataset.GetProjection()


class TestBackendConstruction:
    """The constructor's ShakeMap surface."""

    def test_default_output_kind_is_vector(self, tmp_path: Path):
        """Without the flag the backend stays a vector backend."""
        assert _backend(tmp_path).OUTPUT_KIND == "vector"

    def test_shakemap_makes_output_kind_mixed(self, tmp_path: Path):
        """With the flag the instance emits rasters too."""
        assert _backend(tmp_path, with_shakemap=True).OUTPUT_KIND == "mixed"

    def test_bad_layer_rejected_even_when_flag_off(self, tmp_path: Path):
        """A typo'd layer name fails at construction, flag or not."""
        with pytest.raises(ValueError, match="unknown ShakeMap layer"):
            _backend(tmp_path, shakemap_layers=["nope"])

    def test_aggregate_still_refused_when_mixed(self, tmp_path: Path):
        """A mixed-output instance still refuses the aggregator."""
        backend = _backend(tmp_path, with_shakemap=True)
        with pytest.raises(NotImplementedError, match="aggregate="):
            backend.download(aggregate=object())


class TestBackendShakemapDownload:
    """The side-output as driven through `download()`."""

    def test_writes_geotiff_per_event(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """Each USGS event gets its requested grid as a GeoTIFF."""
        backend = _backend(
            tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean", "pga_mean"]
        )
        fc = backend.download(progress_bar=False)

        assert len(fc) == 1, "the event table is unchanged by the side-output"
        written = sorted(p.name for p in (tmp_path / "shakemap").rglob("*.tif"))
        assert written == ["mmi_mean.tif", "pga_mean.tif"]

    def test_intermediates_are_cleaned_up(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The archive and extracted grids do not survive conversion."""
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        leftovers = [
            p.name
            for p in (tmp_path / "shakemap").rglob("*")
            if p.suffix in {".zip", ".flt", ".hdr"}
        ]
        assert leftovers == []

    def test_skips_network_on_rerun(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A second run reuses the GeoTIFFs already on disk."""
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.downloads)
        backend.download(progress_bar=False)
        assert len(fake_http.downloads) == first, "no second archive fetch"

    def test_no_shakemap_product_writes_nothing(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """An event publishing no ShakeMap yields no raster and no error."""
        fake_http.detail = {"properties": {"products": {}}}
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        assert list((tmp_path / "shakemap").rglob("*.tif")) == []

    def test_flag_off_makes_no_http_call(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """Without the flag the ComCat path is never touched."""
        _backend(tmp_path).download(progress_bar=False)
        assert fake_http.json_urls == []
        assert not (tmp_path / "shakemap").exists()

    def test_non_usgs_network_is_skipped(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A non-USGS network contributes events but no rasters."""
        backend = _backend(tmp_path, variables=["EMSC"], with_shakemap=True)
        fc = backend.download(progress_bar=False)

        assert len(fc) == 1, "the events themselves still arrive"
        assert fake_http.json_urls == [], "ShakeMap is a USGS-only product"

    def test_detail_url_uses_parsed_comcat_id(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The detail request is keyed by the event's ComCat id."""
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        assert fake_http.json_urls == [_helpers.detail_url("us6000jlqa")]


class TestShakemapFailurePolicy:
    """Partial failure across the per-event ShakeMap loop."""

    def test_one_failed_event_does_not_lose_the_others(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """A failing event is skipped while healthy ones still land."""
        fake_fdsn.default_result = _usgs_catalog(make_event, "us1111", "us2222")
        fake_http.fail_json_for = {"us1111"}

        fc = _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert len(fc) == 2, "both events stay in the table"
        written = sorted(p.parent.name for p in (tmp_path / "shakemap").rglob("*.tif"))
        assert written == ["us2222"], (
            f"only the healthy event yields a raster: {written}"
        )

    def test_failed_event_does_not_lose_the_event_table(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """Every ShakeMap failing still returns the events themselves."""
        fake_fdsn.default_result = _usgs_catalog(make_event, "us1111", "us2222")
        fake_http.fail_json_for = {"us1111", "us2222"}

        fc = _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert len(fc) == 2, "the vector output is independent of the side-output"
        assert list((tmp_path / "shakemap").rglob("*.tif")) == []

    def test_raise_policy_propagates(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """errors='raise' surfaces the first ShakeMap failure."""
        fake_fdsn.default_result = _usgs_catalog(make_event, "us1111")
        fake_http.fail_json_for = {"us1111"}

        backend = _backend(tmp_path, with_shakemap=True)
        with pytest.raises(RuntimeError, match="ComCat detail unavailable"):
            backend.download(progress_bar=False, errors="raise")

    def test_download_failure_is_skipped_under_warn(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """An archive transfer that fails mid-way skips the event."""
        fake_http.fail_download_for = {"us6000jlqa"}

        fc = _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert len(fc) == 1, "the event table survives a failed transfer"
        assert list((tmp_path / "shakemap").rglob("*.tif")) == []

    def test_failed_download_leaves_no_partial_archive(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A failed transfer cleans up rather than leaving an archive behind."""
        fake_http.fail_download_for = {"us6000jlqa"}
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        leftovers = [p.name for p in tmp_path.rglob("raster.zip")]
        assert leftovers == [], f"no archive should survive: {leftovers}"


class TestShakemapArchiveProblems:
    """Archives that are unusable or missing the requested grids."""

    def test_corrupt_archive_is_skipped(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A body that is not a zip is skipped, not raised."""
        fake_http.archive = b"this is not a zip file"

        fc = _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert len(fc) == 1, "the event table is unaffected"
        assert list((tmp_path / "shakemap").rglob("*.tif")) == []

    def test_archive_missing_every_requested_layer(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """An archive carrying none of the requested grids writes nothing."""
        fake_http.archive = _make_archive(layers=("pgv_std",))

        backend = _backend(tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean"])
        fc = backend.download(progress_bar=False)

        assert len(fc) == 1
        assert list((tmp_path / "shakemap").rglob("*.tif")) == []

    def test_archive_missing_one_of_several_layers(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The grids the archive does carry are still written."""
        fake_http.archive = _make_archive(layers=("mmi_mean",))

        backend = _backend(
            tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean", "pga_mean"]
        )
        backend.download(progress_bar=False)

        written = sorted(p.name for p in (tmp_path / "shakemap").rglob("*.tif"))
        assert written == ["mmi_mean.tif"], (
            f"partial archive still yields what it has: {written}"
        )


class TestShakemapRerun:
    """Skip-if-present behaviour across repeated runs."""

    def test_partially_present_layers_are_refetched(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A run missing one of its GeoTIFFs refetches rather than half-skipping."""
        backend = _backend(
            tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean", "pga_mean"]
        )
        backend.download(progress_bar=False)
        first = len(fake_http.downloads)

        next((tmp_path / "shakemap").rglob("pga_mean.tif")).unlink()
        backend.download(progress_bar=False)

        assert len(fake_http.downloads) == first + 1, "the incomplete event refetches"
        written = sorted(p.name for p in (tmp_path / "shakemap").rglob("*.tif"))
        assert written == ["mmi_mean.tif", "pga_mean.tif"], "both grids present again"


class TestShakemapMultiNetwork:
    """Requests mixing USGS with another network."""

    def test_only_usgs_events_yield_rasters(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """A mixed request keeps every event but rasters only USGS."""
        fake_fdsn.set_result("USGS", _usgs_catalog(make_event, "us1111"))
        fake_fdsn.set_result("EMSC", _usgs_catalog(make_event, "em9999"))

        backend = _backend(tmp_path, variables=["USGS", "EMSC"], with_shakemap=True)
        fc = backend.download(progress_bar=False)

        assert len(fc) == 2, "both networks contribute events"
        rastered = sorted(p.parent.name for p in (tmp_path / "shakemap").rglob("*.tif"))
        assert rastered == ["us1111"], f"only USGS is rastered: {rastered}"

    def test_empty_non_usgs_network_is_quiet(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """A non-USGS network that matched nothing needs no warning."""
        from obspy.core.event import Catalog

        fake_fdsn.set_result("USGS", _usgs_catalog(make_event, "us1111"))
        fake_fdsn.set_result("EMSC", Catalog(events=[]))

        backend = _backend(tmp_path, variables=["USGS", "EMSC"], with_shakemap=True)
        fc = backend.download(progress_bar=False)

        assert len(fc) == 1, "only USGS matched"
        rastered = sorted(p.parent.name for p in (tmp_path / "shakemap").rglob("*.tif"))
        assert rastered == ["us1111"]


class TestShakemapUnresolvableId:
    """Events whose identifier carries no ComCat id."""

    def test_unparseable_event_id_is_skipped(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A USGS row with a local identifier is skipped without erroring."""
        fc = _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert len(fc) == 1, "the event itself still arrives"
        assert fake_http.json_urls == [], "no detail request without a ComCat id"
        assert list((tmp_path / "shakemap").rglob("*.tif")) == []
