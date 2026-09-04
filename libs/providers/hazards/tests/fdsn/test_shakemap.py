"""Tests for the FDSN ShakeMap side-output (helpers plus backend wiring).

The archive format is reproduced synthetically — a tiny ESRI float grid
plus its header, zipped the way ComCat ships one — so the whole path
runs offline.
"""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

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


class _FakeSession:
    """Stand-in for the `requests.Session` an `HttpClient` owns."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


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
        self.download_kwargs: list[dict[str, Any]] = []
        self.init_kwargs: dict[str, Any] = {}
        self.fail_json_for: set[str] = set()
        self.fail_download_for: set[str] = set()
        self.partial_download_for: set[str] = set()
        self.builds = 0
        self.session = _FakeSession()

    def get_json(self, url: str, **_kwargs: Any) -> dict[str, Any]:
        self.json_urls.append(url)
        if any(marker in url for marker in self.fail_json_for):
            raise RuntimeError(f"ComCat detail unavailable for {url}")
        return self.detail

    def download(self, url: str, dest: str | Path, **kwargs: Any) -> Path:
        dest = Path(dest)
        self.downloads.append((url, dest))
        self.download_kwargs.append(kwargs)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if any(marker in str(dest) for marker in self.partial_download_for):
            # Mirror a non-atomic transport dying mid-stream: bytes on disk,
            # but not a usable archive.
            dest.write_bytes(self.archive[: len(self.archive) // 2])
            raise RuntimeError(f"archive transfer died mid-stream for {url}")
        if any(marker in str(dest) for marker in self.fail_download_for):
            raise RuntimeError(f"archive transfer failed for {url}")
        magic = kwargs.get("expect_magic")
        if magic is not None and not self.archive.startswith(magic):
            # The real client discards the body and raises rather than letting
            # an HTML error page land as a .zip.
            raise ValueError(f"body for {url} does not start with {magic!r}")
        dest.write_bytes(self.archive)
        return dest


def _build_fake_http(fake: _FakeHttp, **kwargs: Any) -> _FakeHttp:
    """Record the kwargs the backend built its client with, then serve it."""
    fake.init_kwargs = kwargs
    fake.builds += 1
    return fake


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> _FakeHttp:
    """Patch the backend's `HttpClient` with the canned fake."""
    fake = _FakeHttp()
    monkeypatch.setattr(
        fdsn_backend, "HttpClient", lambda **kwargs: _build_fake_http(fake, **kwargs)
    )
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


def _to_file_writes_then_raises(self, path, *args, **kwargs):
    """Write bytes then fail, as an interrupted conversion would."""
    Path(path).write_bytes(b"partial")
    raise RuntimeError("conversion failed")


def _to_file_raises(self, path, *args, **kwargs):
    """Fail before writing anything."""
    raise RuntimeError("conversion failed")


def _to_file_writes_nothing(self, path, *args, **kwargs):
    """Return without producing an output file."""
    return None


def _write_manifest_refuses(*_args, **_kwargs):
    """Fail as an unwritable filesystem would."""
    raise OSError("read-only file system")


def _backend_fixture_marker() -> None:
    """Anchor for the module-scope helpers above."""


class _ExtractRecorder:
    """Records each destination directory `extract_layers` is given."""

    def __init__(self, wrapped, seen: list[str]) -> None:
        self._wrapped = wrapped
        self._seen = seen

    def __call__(self, archive, layers, dest_dir):
        self._seen.append(dest_dir.name)
        return self._wrapped(archive, layers, dest_dir)


#: Names captured by `_recording_replace`; a test clears it before acting.
_REPLACE_CALLS: list[str] = []

#: Bound once at import so the recorder cannot wrap itself on a second patch.
_ORIGINAL_REPLACE = Path.replace


def _recording_replace(source, target):
    """Record the staged name, then perform the real rename.

    A plain function rather than a callable object: `Path.replace` is looked
    up on the class, and only a function is a descriptor, so an instance
    would be called without the path it was invoked on.
    """
    _REPLACE_CALLS.append(source.name)
    return _ORIGINAL_REPLACE(source, target)


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

    @pytest.mark.parametrize("length", [1, 63, 64])
    def test_ids_up_to_the_bound_are_accepted(self, length: int):
        """An id at or under the length bound parses."""
        comcat_id = "a" * length
        assert _helpers.parse_comcat_id(f"q?eventid={comcat_id}&f=q") == comcat_id

    @pytest.mark.parametrize("length", [65, 200])
    def test_ids_past_the_bound_are_refused(self, length: int):
        """An overlong id is refused rather than handed to the filesystem."""
        assert _helpers.parse_comcat_id(f"q?eventid={'a' * length}&f=q") is None

    def test_id_at_the_end_of_the_identifier(self):
        """An id terminating the string, with no trailing separator, parses."""
        assert _helpers.parse_comcat_id("q?eventid=us6000jlqa") == "us6000jlqa"

    def test_id_as_a_later_parameter(self):
        """The id is found when it is not the first query parameter."""
        assert (
            _helpers.parse_comcat_id("q?format=quakeml&eventid=us6000jlqa")
            == "us6000jlqa"
        )

    @pytest.mark.parametrize(
        "encoded", ["%2e%2e", "..%2fetc", "us/evil", "C:", "us evil"]
    )
    def test_encoded_and_separator_forms_are_refused(self, encoded: str):
        """Percent-encoded dots and path separators never survive the parse."""
        assert _helpers.parse_comcat_id(f"q?eventid={encoded}&f=q") is None


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

    @pytest.mark.parametrize("bad", [[None], [1], [object()]])
    def test_non_string_members_are_refused(self, bad: list):
        """A non-string layer name is refused rather than stringified."""
        with pytest.raises(ValueError, match="unknown ShakeMap layer"):
            _helpers.normalize_layers(bad)

    def test_accepts_any_iterable(self):
        """A tuple works as well as a list."""
        assert _helpers.normalize_layers(("pga_mean",)) == ("pga_mean",)


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

    def test_empty_shakemap_list_is_none(self):
        """A shakemap key holding no entries yields None."""
        detail = {"properties": {"products": {"shakemap": []}}}
        assert _helpers.shakemap_raster_url(detail) is None

    def test_null_shakemap_entry_is_none(self):
        """A null first entry does not raise."""
        detail = {"properties": {"products": {"shakemap": [None]}}}
        assert _helpers.shakemap_raster_url(detail) is None

    def test_raster_entry_without_url_is_none(self):
        """A raster entry carrying no url yields None."""
        detail = {
            "properties": {
                "products": {"shakemap": [{"contents": {"download/raster.zip": {}}}]}
            }
        }
        assert _helpers.shakemap_raster_url(detail) is None


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

    def test_traversing_member_is_never_extracted(self, tmp_path: Path):
        """A member named to escape the destination is not read at all.

        Only member names built from the requested layers are opened, so a
        hostile archive cannot place a file outside dest_dir.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("../evil.flt", b"xxxx")
            bundle.writestr("../evil.hdr", "NROWS 1")
            bundle.writestr("mmi_mean.flt", b"xxxx")
            bundle.writestr("mmi_mean.hdr", "NROWS 1")
        archive = tmp_path / "raster.zip"
        archive.write_bytes(buffer.getvalue())

        extracted = _helpers.extract_layers(archive, ["mmi_mean"], tmp_path / "out")

        assert sorted(extracted) == ["mmi_mean"]
        assert not (tmp_path / "evil.flt").exists(), "nothing may escape dest_dir"

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
        from pyramids.dataset import Dataset

        archive = tmp_path / "raster.zip"
        archive.write_bytes(_make_archive(layers=("mmi_mean",)))
        extracted = _helpers.extract_layers(archive, ["mmi_mean"], tmp_path)
        dest = _helpers.flt_to_geotiff(extracted["mmi_mean"], tmp_path / "mmi_mean.tif")

        assert dest.is_file()
        dataset = Dataset.read_file(dest)
        try:
            assert dataset.driver_type == "geotiff"
            assert dataset.columns == _COLS
            assert dataset.rows == _ROWS
            assert dataset.epsg == 4326, (
                "the CRS should carry an EPSG authority code, not merely the "
                "digits 4326 somewhere in its WKT"
            )
        finally:
            dataset.close()


class TestFltToGeotiffFailures:
    """Conversion inputs that cannot produce a raster."""

    def test_missing_header_raises_and_publishes_nothing(self, tmp_path: Path):
        """A .flt with no .hdr sibling fails without leaving an output."""
        grid = tmp_path / "mmi_mean.flt"
        grid.write_bytes(bytes(16))

        with pytest.raises(RuntimeError):
            _helpers.flt_to_geotiff(grid, tmp_path / "mmi_mean.tif")

        assert not (tmp_path / "mmi_mean.tif").exists(), "no raster may be published"
        assert list(tmp_path.glob("*.partial.tif")) == [], "no staged partial"


class TestManifestRoundTrip:
    """The per-event manifest read/write pair."""

    def test_absent_manifest_reads_as_none(self, tmp_path: Path):
        """A directory with no manifest reads as None."""
        assert _helpers.read_manifest(tmp_path) is None

    def test_round_trip(self, tmp_path: Path):
        """What is written is read back, sorted and de-duplicated."""
        _helpers.write_manifest(
            tmp_path, ["pga_mean", "mmi_mean", "mmi_mean"], ["mmi_mean"]
        )
        manifest = _helpers.read_manifest(tmp_path)
        assert manifest["requested"] == ["mmi_mean", "pga_mean"]
        assert manifest["produced"] == ["mmi_mean"]

    def test_write_creates_the_directory(self, tmp_path: Path):
        """The event directory is created if it does not exist yet."""
        target = tmp_path / "us6000jlqa"
        _helpers.write_manifest(target, ["mmi_mean"], [])
        assert (target / _helpers.MANIFEST_NAME).is_file()

    def test_non_dict_json_reads_as_none(self, tmp_path: Path):
        """A manifest holding a JSON list is treated as absent."""
        (tmp_path / _helpers.MANIFEST_NAME).write_text("[1, 2]", encoding="utf-8")
        assert _helpers.read_manifest(tmp_path) is None


class TestClientLifecycle:
    """Building and releasing the pooled ComCat client."""

    def test_close_without_a_client_is_a_no_op(self, tmp_path: Path):
        """Closing before any client was built does not raise."""
        backend = _backend(tmp_path)
        backend._close_client()

    def test_close_is_idempotent(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A second close after a download does not raise or double-close."""
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        backend._close_client()
        assert fake_http.session.closed == 1, "the session closes exactly once"

    def test_client_is_reused_within_one_call(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn, fake_http: _FakeHttp, make_event
    ):
        """Two events in one download share a single client."""
        fake_fdsn.default_result = _usgs_catalog(make_event, "us1111", "us2222")
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        assert fake_http.builds == 1, (
            f"the client should be built once for the batch, got {fake_http.builds}"
        )
        assert fake_http.session.closed == 1, "one client for the whole batch"


class TestBackendConstruction:
    """The constructor's ShakeMap surface."""

    def test_default_output_kind_is_vector(self, tmp_path: Path):
        """Without the flag the backend stays a vector backend."""
        assert _backend(tmp_path).OUTPUT_KIND == "vector"

    def test_shakemap_keeps_output_kind_vector(self, tmp_path: Path):
        """The rasters are a side effect, so the return shape is unchanged."""
        assert _backend(tmp_path, with_shakemap=True).OUTPUT_KIND == "vector"

    def test_bad_layer_rejected_even_when_flag_off(self, tmp_path: Path):
        """A typo'd layer name fails at construction, flag or not."""
        with pytest.raises(ValueError, match="unknown ShakeMap layer"):
            _backend(tmp_path, shakemap_layers=["nope"])

    def test_aggregate_still_refused_with_shakemap(self, tmp_path: Path):
        """A ShakeMap instance still refuses the aggregator."""
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

    def test_event_directory_holds_exactly_the_rasters(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """Nothing but the requested GeoTIFFs survives in the output folder.

        Asserts the exact directory listing rather than the absence of a few
        suffixes: GDAL writes a `.prj` beside the grid when its CRS is
        assigned, which an allowlist of forbidden extensions would miss.
        """
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        event_dir = tmp_path / "shakemap" / "us6000jlqa"
        assert sorted(p.name for p in event_dir.iterdir()) == [
            _helpers.MANIFEST_NAME,
            "mmi_mean.tif",
        ]

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

    def test_partially_written_archive_is_cleaned_up(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A transfer that dies mid-stream leaves no half-archive behind.

        The fake writes real bytes before raising, so the cleanup has
        something to actually remove.
        """
        fake_http.partial_download_for = {"us6000jlqa"}
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        leftovers = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.zip")]
        assert leftovers == [], f"no archive should survive: {leftovers}"
        assert not (tmp_path / "shakemap" / "us6000jlqa").exists(), (
            "an event that produced no raster should leave no directory"
        )


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


class TestShakemapPathSafety:
    """The ComCat id becomes a directory name, so it is constrained."""

    @pytest.mark.parametrize("degenerate", ["..", ".", "../../etc"])
    def test_traversing_id_is_not_parsed(self, degenerate: str):
        """An id that would escape the output directory is refused outright."""
        assert _helpers.parse_comcat_id(f"quakeml:x?eventid={degenerate}&f=q") is None

    def test_traversing_id_deletes_nothing(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """A traversing id leaves files in the output root untouched."""
        from obspy.core.event import Catalog

        event = make_event()
        event.resource_id = "quakeml:x?eventid=..&format=quakeml"
        fake_fdsn.default_result = Catalog(events=[event])

        keep_flt = tmp_path / "keepme.flt"
        keep_hdr = tmp_path / "keepme.hdr"
        keep_flt.write_bytes(b"keep")
        keep_hdr.write_text("keep")

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert keep_flt.is_file(), "an unrelated .flt must not be deleted"
        assert keep_hdr.is_file(), "an unrelated .hdr must not be deleted"


class TestShakemapAtomicWrite:
    """A GeoTIFF on disk is a finished one."""

    def test_no_partial_file_survives(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The staged partial name is never left in the output directory."""
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        event_dir = tmp_path / "shakemap" / "us6000jlqa"
        partials = [p.name for p in event_dir.iterdir() if "partial" in p.name]
        assert partials == [], f"staged partials should be renamed away: {partials}"

    def test_force_refetches_an_existing_raster(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """force=True re-fetches instead of trusting what is on disk."""
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.downloads)

        backend.download(progress_bar=False, force=True)
        assert len(fake_http.downloads) == first + 1, "force should refetch"

    def test_without_force_a_present_raster_is_reused(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The default still reuses a finished raster."""
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.downloads)

        backend.download(progress_bar=False)
        assert len(fake_http.downloads) == first, "no refetch without force"


class TestShakemapFanOutCeiling:
    """The per-event fan-out is bounded rather than merely documented."""

    def test_events_beyond_the_ceiling_are_skipped(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """Only up to max_shakemap_events events are fetched."""
        fake_fdsn.default_result = _usgs_catalog(
            make_event, "us1111", "us2222", "us3333"
        )
        backend = _backend(tmp_path, with_shakemap=True, max_shakemap_events=2)
        fc = backend.download(progress_bar=False)

        assert len(fc) == 3, "every event still reaches the table"
        fetched = sorted(p.name for p in (tmp_path / "shakemap").iterdir())
        assert fetched == ["us1111", "us2222"], f"ceiling not applied: {fetched}"

    def test_the_skip_is_announced(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """Dropping events past the ceiling is logged, never silent."""
        from loguru import logger as loguru_logger

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda message: messages.append(str(message)))
        try:
            fake_fdsn.default_result = _usgs_catalog(make_event, "us1111", "us2222")
            _backend(tmp_path, with_shakemap=True, max_shakemap_events=1).download(
                progress_bar=False
            )
        finally:
            loguru_logger.remove(sink_id)

        assert any(
            "max_shakemap_events" in m and "deferring 1" in m for m in messages
        ), f"the deferred count should be announced, got: {messages}"

    def test_ceiling_rejects_non_positive(self, tmp_path: Path):
        """A zero or negative ceiling is refused at construction."""
        with pytest.raises(ValueError):
            _backend(tmp_path, with_shakemap=True, max_shakemap_events=0)


class TestShakemapClientWiring:
    """The real HttpClient contract the fake stands in for."""

    def test_archive_fetch_asserts_zip_magic(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The archive download demands a zip magic prefix."""
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert fake_http.download_kwargs, "the archive should have been fetched"
        assert fake_http.download_kwargs[0]["expect_magic"] == b"PK", (
            "an HTML error page served with a 200 must not land as a .zip"
        )

    def test_client_is_throttled_and_retries_transport_errors(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The client is built with a politeness interval and transport retries."""
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert fake_http.init_kwargs.get("min_interval") > 0, "expected a throttle"
        assert fake_http.init_kwargs.get("timeout"), "expected an explicit timeout"
        retried = fake_http.init_kwargs.get("retry_on_exceptions") or ()
        assert requests.ConnectionError in retried, "a dropped connection retries"
        assert requests.Timeout in retried, "a timeout retries"
        assert requests.exceptions.ChunkedEncodingError in retried, (
            "a multi-megabyte body dies mid-stream, which is neither of the above"
        )

    def test_progress_flag_reaches_the_archive_fetch(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """progress_bar governs the long part of the call."""
        _backend(tmp_path, with_shakemap=True).download(progress_bar=True)
        assert fake_http.download_kwargs[0]["progress"] is True

    def test_client_is_closed_after_the_loop(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The pooled session is released once the ShakeMap work is done."""
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        assert fake_http.session.closed == 1, "the session should close once"


class TestShakemapLayersTypeGuard:
    """A bare string is a sequence of characters, not a layer list."""

    def test_bare_string_is_refused(self, tmp_path: Path):
        """Passing one layer as a bare string raises a legible TypeError."""
        with pytest.raises(TypeError, match="not the bare string"):
            _backend(tmp_path, shakemap_layers="mmi_mean")


class TestShakemapDefenceInDepth:
    """Guards that a tightened parser should already make unreachable."""

    def test_containment_guard_refuses_an_escaping_id(
        self,
        tmp_path: Path,
        usgs_events: _FakeFdsn,
        fake_http: _FakeHttp,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """An id that escapes the shakemap root is refused before any fetch.

        Forces the guard by making the parser hand back a traversing id, which
        the tightened pattern would otherwise never produce.
        """
        monkeypatch.setattr(fdsn_backend._helpers, "parse_comcat_id", lambda _id: "..")

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert fake_http.json_urls == [], "no request should be made for a bad id"

    def test_oversized_member_is_refused(
        self,
        tmp_path: Path,
        usgs_events: _FakeFdsn,
        fake_http: _FakeHttp,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A member whose declared size is absurd is skipped, not expanded."""
        monkeypatch.setattr(fdsn_backend._helpers, "MAX_MEMBER_BYTES", 1)

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert list((tmp_path / "shakemap").rglob("*.tif")) == [], (
            "an over-large member must not be written"
        )


class TestShakemapManifestReuse:
    """A rerun distinguishes 'not fetched' from 'not published upstream'."""

    def test_partial_archive_is_not_refetched(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """An archive permanently missing a layer stops re-downloading."""
        fake_http.archive = _make_archive(layers=("mmi_mean",))
        backend = _backend(
            tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean", "pga_mean"]
        )
        backend.download(progress_bar=False)
        first = len(fake_http.downloads)

        backend.download(progress_bar=False)
        assert len(fake_http.downloads) == first, (
            "the missing layer does not exist upstream, so a rerun must not refetch"
        )

    def test_event_without_shakemap_is_not_re_requested(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """An event publishing no ShakeMap is not asked about twice."""
        fake_http.detail = {"properties": {"products": {}}}
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.json_urls)

        backend.download(progress_bar=False)
        assert len(fake_http.json_urls) == first, "the negative result is cached"

    def test_widening_the_request_refetches(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """Asking for more layers than last time fetches again."""
        _backend(tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean"]).download(
            progress_bar=False
        )
        first = len(fake_http.downloads)

        _backend(
            tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean", "pga_mean"]
        ).download(progress_bar=False)
        assert len(fake_http.downloads) == first + 1, "a wider request must refetch"

    def test_unreadable_manifest_refetches(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A corrupt manifest is treated as absent rather than trusted."""
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.downloads)

        manifest = tmp_path / "shakemap" / "us6000jlqa" / _helpers.MANIFEST_NAME
        manifest.write_text("{not json", encoding="utf-8")
        backend.download(progress_bar=False)
        assert len(fake_http.downloads) == first + 1, "a corrupt manifest refetches"


class TestShakemapConversionFailure:
    """The atomic-write failure path, which the success path cannot reach."""

    def test_failed_conversion_publishes_nothing(
        self,
        tmp_path: Path,
        usgs_events: _FakeFdsn,
        fake_http: _FakeHttp,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A conversion that dies leaves no raster and no staged partial."""

        monkeypatch.setattr(
            "pyramids.dataset.Dataset.to_file",
            _to_file_writes_then_raises,
            raising=True,
        )

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert list(tmp_path.rglob("*.tif")) == [], "nothing should be published"
        assert list(tmp_path.rglob("*.partial.tif")) == [], "no staged partial"

    def test_failed_conversion_leaves_no_staging_directory(
        self,
        tmp_path: Path,
        usgs_events: _FakeFdsn,
        fake_http: _FakeHttp,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The scratch directory is gone even when the conversion raised.

        The GDAL handle on the extracted grid must be released before the
        caller cleans up, or Windows refuses to unlink it and the scratch
        directory survives inside the user's output.
        """

        monkeypatch.setattr(
            "pyramids.dataset.Dataset.to_file", _to_file_raises, raising=True
        )

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        staging = list(tmp_path.rglob("_staging"))
        assert staging == [], f"the scratch directory must not survive: {staging}"

    def test_missing_output_is_refused(self, tmp_path: Path, monkeypatch):
        """A conversion that writes nothing raises instead of renaming."""

        archive = tmp_path / "raster.zip"
        archive.write_bytes(_make_archive(layers=("mmi_mean",)))
        extracted = _helpers.extract_layers(archive, ["mmi_mean"], tmp_path / "in")
        monkeypatch.setattr(
            "pyramids.dataset.Dataset.to_file", _to_file_writes_nothing, raising=True
        )

        with pytest.raises(RuntimeError, match="produced no output"):
            _helpers.flt_to_geotiff(extracted["mmi_mean"], tmp_path / "out.tif")


class TestShakemapMagicGuard:
    """The zip-magic guard the real client applies."""

    def test_html_error_page_is_refused(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A 200 carrying an HTML error page never lands as an archive."""
        fake_http.archive = b"<!DOCTYPE html><html>error</html>"

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert list(tmp_path.rglob("*.tif")) == [], "no raster from an HTML body"


class TestSearchDeduplication:
    """A repeated network key must not be queried twice."""

    def test_repeated_network_yields_one_product(self, tmp_path: Path):
        """Duplicate keys collapse to a single product."""
        backend = _backend(tmp_path, variables=["USGS", "USGS", "EMSC"])
        assert [p.id for p in backend._search()] == ["USGS", "EMSC"]


class TestShakemapErrorsIgnore:
    """The third partial-failure policy on the ShakeMap loop."""

    def test_ignore_continues_without_warning(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """errors='ignore' skips a failed event and keeps the healthy one."""
        fake_fdsn.default_result = _usgs_catalog(make_event, "us1111", "us2222")
        fake_http.fail_json_for = {"us1111"}

        from loguru import logger as loguru_logger

        messages: list[str] = []
        sink_id = loguru_logger.add(
            lambda message: messages.append(str(message)), level="WARNING"
        )
        try:
            fc = _backend(tmp_path, with_shakemap=True).download(
                progress_bar=False, errors="ignore"
            )
        finally:
            loguru_logger.remove(sink_id)

        assert len(fc) == 2, "the event table is unaffected"
        rastered = sorted(p.name for p in (tmp_path / "shakemap").iterdir())
        assert rastered == ["us2222"], f"only the healthy event: {rastered}"
        assert not [m for m in messages if "us1111" in m], (
            f"errors='ignore' should not warn about the skipped event: {messages}"
        )


class TestShakemapStagingLeakIsReported:
    """A scratch directory that cannot be removed is announced, not hidden."""

    def test_unremovable_staging_is_logged(
        self,
        tmp_path: Path,
        usgs_events: _FakeFdsn,
        fake_http: _FakeHttp,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A failed rmtree produces a warning naming the directory."""
        from loguru import logger as loguru_logger

        monkeypatch.setattr(fdsn_backend.shutil, "rmtree", lambda *a, **k: None)

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda message: messages.append(str(message)))
        try:
            _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        finally:
            loguru_logger.remove(sink_id)

        assert any("staging directory" in m for m in messages), (
            f"a surviving scratch directory should be reported, got: {messages}"
        )


class _CloselessSession:
    """A session-like object with no `close`, as `RequestsGet` is."""


class TestShakemapCloselessSession:
    """Closing must tolerate a transport that cannot be closed."""

    def test_session_without_close_is_survived(
        self,
        tmp_path: Path,
        usgs_events: _FakeFdsn,
        fake_http: _FakeHttp,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A RequestsGet-style session does not break the download.

        `earthlens.testing` swaps `HttpClient`'s default transport for
        `RequestsGet`, which has no `close()`, so this is the shape the whole
        suite and any backend using that adapter actually sees.
        """
        monkeypatch.setattr(fake_http, "session", _CloselessSession())

        fc = _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert len(fc) == 1, "the download should complete regardless"
        assert list((tmp_path / "shakemap").rglob("*.tif")), "rasters still written"


class TestCeilingAdvancesAcrossRuns:
    """The ceiling bounds work, so a re-run reaches events it deferred."""

    def test_rerun_walks_further_through_the_list(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """Three events under a ceiling of two are all reached in two runs."""
        fake_fdsn.default_result = _usgs_catalog(
            make_event, "us1111", "us2222", "us3333"
        )
        backend = _backend(tmp_path, with_shakemap=True, max_shakemap_events=2)

        backend.download(progress_bar=False)
        first = sorted(p.name for p in (tmp_path / "shakemap").iterdir())
        assert first == ["us1111", "us2222"], f"first run takes two: {first}"

        backend.download(progress_bar=False)
        second = sorted(p.name for p in (tmp_path / "shakemap").iterdir())
        assert second == ["us1111", "us2222", "us3333"], (
            f"the re-run must reach the deferred event, got {second}"
        )

    def test_cached_events_do_not_consume_budget(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """An event already on disk costs no ceiling budget."""
        fake_fdsn.default_result = _usgs_catalog(make_event, "us1111", "us2222")
        _backend(tmp_path, with_shakemap=True, max_shakemap_events=1).download(
            progress_bar=False
        )
        _backend(tmp_path, with_shakemap=True, max_shakemap_events=1).download(
            progress_bar=False
        )

        fetched = sorted(p.name for p in (tmp_path / "shakemap").iterdir())
        assert fetched == ["us1111", "us2222"], f"both reached in two runs: {fetched}"


class TestNegativeCacheExpiry:
    """A no-ShakeMap answer ages out rather than sticking forever."""

    def test_fresh_negative_is_reused(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A recent negative result still skips the detail request."""
        fake_http.detail = {"properties": {"products": {}}}
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.json_urls)

        backend.download(progress_bar=False)
        assert len(fake_http.json_urls) == first, "a fresh negative is cached"

    def test_stale_negative_is_rechecked(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """An aged negative result is checked again, so a late grid is found."""
        fake_http.detail = {"properties": {"products": {}}}
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)

        manifest_path = tmp_path / "shakemap" / "us6000jlqa" / _helpers.MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        # A fixed epoch value, not one derived from the constant under test:
        # deriving it means the test passes for any TTL, including an absurd one.
        payload["checked"] = 1.0
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        fake_http.detail = _detail()
        backend.download(progress_bar=False)

        assert list((tmp_path / "shakemap").rglob("*.tif")), (
            "the grid published since the last run should now be fetched"
        )

    def test_positive_result_does_not_expire(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """Rasters on disk are reused however old the manifest is."""
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.downloads)

        manifest_path = tmp_path / "shakemap" / "us6000jlqa" / _helpers.MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["checked"] = 0.0
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        backend.download(progress_bar=False)
        assert len(fake_http.downloads) == first, "a present raster is still reused"


class TestManifestMerge:
    """Narrowing the requested layers must not lose what is on disk."""

    def test_narrowing_then_widening_does_not_refetch(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """mmi, then pga, then mmi again costs two fetches, not three."""
        for layers in (["mmi_mean"], ["pga_mean"], ["mmi_mean"]):
            _backend(tmp_path, with_shakemap=True, shakemap_layers=layers).download(
                progress_bar=False
            )

        assert len(fake_http.downloads) == 2, (
            "the third run repeats the first, which is already on disk: "
            f"{len(fake_http.downloads)} fetches"
        )

    def test_manifest_describes_everything_on_disk(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """After two narrow runs the manifest lists both rasters."""
        for layers in (["mmi_mean"], ["pga_mean"]):
            _backend(tmp_path, with_shakemap=True, shakemap_layers=layers).download(
                progress_bar=False
            )

        event_dir = tmp_path / "shakemap" / "us6000jlqa"
        manifest = _helpers.read_manifest(event_dir)
        on_disk = sorted(p.stem for p in event_dir.glob("*.tif"))
        assert manifest["produced"] == on_disk, (
            f"manifest should describe disk, got {manifest['produced']} vs {on_disk}"
        )


class TestManifestValidation:
    """A structurally invalid manifest refetches instead of raising."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"schema": 1, "requested": 5, "produced": [], "checked": 0.0},
            {"schema": 1, "requested": [], "produced": "no", "checked": 0.0},
            {"schema": 1, "requested": [1], "produced": [], "checked": 0.0},
            {"schema": 99, "requested": [], "produced": [], "checked": 0.0},
            {"requested": [], "produced": [], "checked": 0.0},
            {"schema": 1, "requested": [], "produced": [], "checked": "soon"},
        ],
    )
    def test_invalid_payload_reads_as_absent(self, tmp_path: Path, payload: dict):
        """Any wrong field type or schema reads as no manifest at all."""
        (tmp_path / _helpers.MANIFEST_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )
        assert _helpers.read_manifest(tmp_path) is None

    def test_invalid_manifest_refetches_and_self_heals(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A bad manifest causes a refetch and is rewritten valid."""
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.downloads)

        manifest_path = tmp_path / "shakemap" / "us6000jlqa" / _helpers.MANIFEST_NAME
        manifest_path.write_text('{"requested": 5}', encoding="utf-8")

        backend.download(progress_bar=False)
        assert len(fake_http.downloads) == first + 1, "a bad manifest refetches"
        assert _helpers.read_manifest(manifest_path.parent) is not None, (
            "the rewrite should repair the manifest"
        )


class TestCorruptArchiveIsBadZip:
    """A body that passes the magic check but is not a usable zip."""

    def test_truncated_zip_is_skipped(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A PK-prefixed but truncated archive is skipped, not raised."""
        fake_http.archive = _make_archive()[:40]

        fc = _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert len(fc) == 1, "the event table is unaffected"
        assert list(tmp_path.rglob("*.tif")) == [], "no raster from a broken zip"


class TestShakemapProductVersion:
    """Reading the ShakeMap product's updateTime off a detail document."""

    def test_reads_update_time(self):
        """A product carrying updateTime yields it as a string."""
        detail = {
            "properties": {"products": {"shakemap": [{"updateTime": 1756575631263}]}}
        }
        assert _helpers.shakemap_product_version(detail) == "1756575631263"

    def test_missing_update_time_is_none(self):
        """A product without updateTime yields None rather than raising."""
        detail = {"properties": {"products": {"shakemap": [{}]}}}
        assert _helpers.shakemap_product_version(detail) is None

    def test_null_entry_is_none(self):
        """A null first product entry yields None."""
        detail = {"properties": {"products": {"shakemap": [None]}}}
        assert _helpers.shakemap_product_version(detail) is None

    def test_no_product_is_none(self):
        """An event with no ShakeMap yields None."""
        assert (
            _helpers.shakemap_product_version({"properties": {"products": {}}}) is None
        )


class TestManifestWriteFailure:
    """A manifest that cannot be saved must not cost the rasters."""

    def test_unwritable_manifest_warns_but_keeps_rasters(
        self,
        tmp_path: Path,
        usgs_events: _FakeFdsn,
        fake_http: _FakeHttp,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The raster survives and the failure is reported."""
        from loguru import logger as loguru_logger

        monkeypatch.setattr(
            fdsn_backend._helpers, "write_manifest", _write_manifest_refuses
        )

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda message: messages.append(str(message)))
        try:
            _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        finally:
            loguru_logger.remove(sink_id)

        assert list(tmp_path.rglob("*.tif")), "the raster must still be written"
        assert any("could not record the ShakeMap manifest" in m for m in messages), (
            f"the failure should be reported, got: {messages}"
        )


class TestForceAndCeiling:
    """force= makes every event pending, so the ceiling applies to all."""

    def test_force_makes_cached_events_spend_budget(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """Under force= a cached event counts against the ceiling again."""
        fake_fdsn.default_result = _usgs_catalog(make_event, "us1111", "us2222")
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        before = len(fake_http.downloads)

        _backend(tmp_path, with_shakemap=True, max_shakemap_events=1).download(
            progress_bar=False, force=True
        )
        assert len(fake_http.downloads) == before + 1, (
            "force= should refetch exactly one event under a ceiling of one"
        )


class TestCachedRastersSemantics:
    """`None` means fetch; an empty list means nothing to fetch."""

    def test_absent_manifest_is_none(self, tmp_path: Path):
        """An event never seen before must be fetched."""
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._cached_rasters(tmp_path / "nope") is None

    def test_known_empty_is_an_empty_list(self, tmp_path: Path):
        """A fresh no-ShakeMap record is a hit that yields no rasters."""
        backend = _backend(tmp_path, with_shakemap=True)
        _helpers.write_manifest(tmp_path, ["mmi_mean"], [], checked=time.time())
        assert backend._cached_rasters(tmp_path) == []

    def test_force_overrides_a_hit(self, tmp_path: Path):
        """force= turns any hit back into a fetch."""
        _helpers.write_manifest(tmp_path, ["mmi_mean"], [], checked=time.time())
        backend = _backend(tmp_path, with_shakemap=True)
        backend._force = True
        assert backend._cached_rasters(tmp_path) is None

    def test_is_cached_matches_the_decision(self, tmp_path: Path):
        """`_is_cached` agrees with `_cached_rasters` for both hit kinds."""
        backend = _backend(tmp_path, with_shakemap=True)
        event_id = _quakeml_id("us6000jlqa")
        assert backend._is_cached(event_id) is False

        event_dir = backend._event_dir("us6000jlqa")
        _helpers.write_manifest(event_dir, ["mmi_mean"], [], checked=time.time())
        assert backend._is_cached(event_id) is True

    def test_unparseable_id_is_not_fetchable(self, tmp_path: Path):
        """An id yielding no ComCat id is dropped before the ceiling counts it.

        It can never become cached, so treating it as pending work would let it
        occupy the ceiling on every run and starve the events behind it.
        """
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._is_fetchable("smi:local/whatever") is False


class TestStagingIsProcessUnique:
    """Two runs over one output root must not share scratch space."""

    def test_staging_directory_carries_the_pid(
        self,
        tmp_path: Path,
        usgs_events: _FakeFdsn,
        fake_http: _FakeHttp,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The scratch directory name includes the process id."""
        seen: list[str] = []
        recorder = _ExtractRecorder(_helpers.extract_layers, seen)
        monkeypatch.setattr(fdsn_backend._helpers, "extract_layers", recorder)

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        assert seen, "the archive should have been extracted"
        assert seen[0] == f"_staging-{os.getpid()}", (
            f"the scratch directory should be process-unique, got {seen[0]}"
        )


class TestCeilingIsNotStarved:
    """Events that can never be fetched must not occupy the ceiling."""

    def test_unparseable_ids_do_not_consume_budget(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """Three dead ids plus one good event still fetches the good one."""
        from obspy.core.event import Catalog

        events = []
        for index in range(3):
            event = make_event(lon=139.0 + index, lat=35.0 + index)
            event.resource_id = f"smi:local/dead-{index}"
            events.append(event)
        good = make_event(lon=150.0, lat=40.0)
        good.resource_id = _quakeml_id("us6000jlqa")
        events.append(good)
        fake_fdsn.default_result = Catalog(events=events)

        _backend(tmp_path, with_shakemap=True, max_shakemap_events=2).download(
            progress_bar=False
        )

        assert fake_http.json_urls, "the fetchable event must not be starved out"
        assert list((tmp_path / "shakemap").rglob("*.tif")), "its raster is written"

    def test_permanent_failures_are_retried_not_cached(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """An event failing every run is retried rather than cached as dead."""
        fake_fdsn.default_result = _usgs_catalog(make_event, "us1111", "us2222")
        fake_http.fail_json_for = {"us1111", "us2222"}

        backend = _backend(tmp_path, with_shakemap=True, max_shakemap_events=2)
        backend.download(progress_bar=False)
        backend.download(progress_bar=False)

        assert len(fake_http.json_urls) == 4, (
            "a transient failure must not become a permanent negative"
        )


class TestPartialProductionExpires:
    """The TTL applies per requested layer, not only to a total blank."""

    def test_missing_layer_is_rechecked_after_the_window(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A layer published upstream later is picked up once the record ages."""
        fake_http.archive = _make_archive(layers=("mmi_mean",))
        backend = _backend(
            tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean", "pga_mean"]
        )
        backend.download(progress_bar=False)

        manifest_path = tmp_path / "shakemap" / "us6000jlqa" / _helpers.MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["checked"] = 1.0
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        fake_http.archive = _make_archive(layers=("mmi_mean", "pga_mean"))
        backend.download(progress_bar=False)

        event_dir = tmp_path / "shakemap" / "us6000jlqa"
        written = sorted(p.name for p in event_dir.glob("*.tif"))
        assert written == ["mmi_mean.tif", "pga_mean.tif"], (
            f"the late-published layer should be picked up, got {written}"
        )

    def test_short_result_is_warned_about(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """Returning fewer layers than requested is announced."""
        from loguru import logger as loguru_logger

        fake_http.archive = _make_archive(layers=("mmi_mean",))
        backend = _backend(
            tmp_path, with_shakemap=True, shakemap_layers=["mmi_mean", "pga_mean"]
        )
        backend.download(progress_bar=False)

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda message: messages.append(str(message)))
        try:
            backend.download(progress_bar=False)
        finally:
            loguru_logger.remove(sink_id)

        assert any("are not available for this event" in m for m in messages), (
            f"a short result should be reported, got: {messages}"
        )

    @pytest.mark.parametrize("stamp", [4102444800.0, "soon", None])
    def test_untrustworthy_timestamp_refetches(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp, stamp: Any
    ):
        """A future or unusable stamp fails towards refetching."""
        fake_http.detail = {"properties": {"products": {}}}
        backend = _backend(tmp_path, with_shakemap=True)
        backend.download(progress_bar=False)
        first = len(fake_http.json_urls)

        manifest_path = tmp_path / "shakemap" / "us6000jlqa" / _helpers.MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["checked"] = stamp
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        backend.download(progress_bar=False)
        assert len(fake_http.json_urls) > first, (
            f"a {stamp!r} timestamp must not cache the answer forever"
        )


class TestRoundFourHardening:
    """The remaining round-4 guards."""

    def test_non_https_archive_url_is_refused(self):
        """An archive URL that is not https is not handed to the downloader."""
        detail = {
            "properties": {
                "products": {
                    "shakemap": [
                        {"contents": {"download/raster.zip": {"url": "http://x/r.zip"}}}
                    ]
                }
            }
        }
        assert _helpers.shakemap_raster_url(detail) is None

    def test_product_version_must_be_a_string(self, tmp_path: Path):
        """A non-string product_version invalidates the manifest."""
        (tmp_path / _helpers.MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema": _helpers.MANIFEST_SCHEMA,
                    "requested": [],
                    "produced": [],
                    "checked": 0.0,
                    "product_version": 12,
                }
            ),
            encoding="utf-8",
        )
        assert _helpers.read_manifest(tmp_path) is None

    def test_product_version_is_recorded(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """The ShakeMap updateTime reaches the manifest."""
        detail = _detail()
        detail["properties"]["products"]["shakemap"][0]["updateTime"] = 1756575631263
        fake_http.detail = detail

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)

        manifest = _helpers.read_manifest(tmp_path / "shakemap" / "us6000jlqa")
        assert manifest["product_version"] == "1756575631263"

    def test_manifest_write_is_atomic(self, tmp_path: Path):
        """The staged manifest is process-unique and does not survive."""
        _helpers.write_manifest(tmp_path, ["mmi_mean"], ["mmi_mean"], checked=1.0)
        leftovers = [p.name for p in tmp_path.iterdir() if "partial" in p.name]
        assert leftovers == [], f"no staged manifest should survive: {leftovers}"
        assert (tmp_path / _helpers.MANIFEST_NAME).is_file()

    def test_write_does_not_warn_about_refetching(self, tmp_path: Path):
        """Repairing a malformed manifest is not a read, so it does not warn."""
        from loguru import logger as loguru_logger

        (tmp_path / _helpers.MANIFEST_NAME).write_text("{oops", encoding="utf-8")

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda message: messages.append(str(message)))
        try:
            _helpers.write_manifest(tmp_path, ["mmi_mean"], [], checked=1.0)
        finally:
            loguru_logger.remove(sink_id)

        assert not [m for m in messages if "refetching" in m], (
            f"a write should not log a reader remedy: {messages}"
        )

    def test_empty_shakemap_root_is_removed(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A run that produced nothing leaves no shakemap directory."""
        fake_http.archive = b"not a zip at all"
        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        assert not (tmp_path / "shakemap").exists(), (
            "an unproductive run should not leave an empty root behind"
        )

    def test_orphaned_staging_is_reported(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A scratch directory from another process is announced, not deleted."""
        from loguru import logger as loguru_logger

        event_dir = tmp_path / "shakemap" / "us6000jlqa"
        orphan = event_dir / "_staging-999999"
        orphan.mkdir(parents=True)
        (orphan / "leftover.flt").write_bytes(b"x")
        fake_http.archive = b"not a zip at all"

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda message: messages.append(str(message)))
        try:
            _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        finally:
            loguru_logger.remove(sink_id)

        assert orphan.exists(), "another process scratch space is not ours to delete"
        assert any("orphaned scratch" in m for m in messages), (
            f"the orphan should be reported, got: {messages}"
        )

    def test_repeated_event_id_is_fetched_once(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """The same ComCat event reported twice costs one fetch."""
        from obspy.core.event import Catalog

        first = make_event()
        first.resource_id = _quakeml_id("us6000jlqa")
        second = make_event(lon=140.0, lat=36.0)
        second.resource_id = _quakeml_id("us6000jlqa")
        fake_fdsn.default_result = Catalog(events=[first, second])

        _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        assert len(fake_http.downloads) == 1, "a duplicate id should not refetch"

    @pytest.mark.parametrize("bad", ["ten", 3.5, None])
    def test_ceiling_rejects_non_integer(self, tmp_path: Path, bad: Any):
        """A non-integer ceiling is refused with a TypeError."""
        with pytest.raises(TypeError, match="must be an integer"):
            _backend(tmp_path, with_shakemap=True, max_shakemap_events=bad)


class TestDefensiveGuardsDirectly:
    """Guards `_is_fetchable` now shields, exercised on their own.

    The filter added for the ceiling means these branches are unreachable
    through `download()`, but both methods are callable directly and keep the
    checks, so they are pinned here rather than left untested.
    """

    def test_is_cached_refuses_an_unparseable_id(self, tmp_path: Path):
        """`_is_cached` still declines an id it cannot resolve."""
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._is_cached("smi:local/whatever") is False

    def test_is_cached_refuses_an_escaping_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`_is_cached` declines an id whose directory escapes the root."""
        monkeypatch.setattr(fdsn_backend._helpers, "parse_comcat_id", lambda _id: "..")
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._is_cached(_quakeml_id("us6000jlqa")) is False

    def test_shakemap_for_event_refuses_an_unparseable_id(self, tmp_path: Path):
        """Called directly, the fetch still declines an unresolvable id."""
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._shakemap_for_event("smi:local/whatever") == []

    def test_shakemap_for_event_refuses_an_escaping_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Called directly, the fetch still declines an escaping directory."""
        monkeypatch.setattr(fdsn_backend._helpers, "parse_comcat_id", lambda _id: "..")
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._shakemap_for_event(_quakeml_id("us6000jlqa")) == []

    def test_event_directory_with_real_leftovers_is_kept_quietly(
        self, tmp_path: Path, usgs_events: _FakeFdsn, fake_http: _FakeHttp
    ):
        """A directory holding a real file is neither removed nor reported."""
        from loguru import logger as loguru_logger

        event_dir = tmp_path / "shakemap" / "us6000jlqa"
        event_dir.mkdir(parents=True)
        (event_dir / "notes.txt").write_text("keep me", encoding="utf-8")
        fake_http.archive = b"not a zip at all"

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda message: messages.append(str(message)))
        try:
            _backend(tmp_path, with_shakemap=True).download(progress_bar=False)
        finally:
            loguru_logger.remove(sink_id)

        assert (event_dir / "notes.txt").is_file(), "an unrelated file must survive"
        assert not [m for m in messages if "orphaned scratch" in m], (
            "a real file is not an orphaned scratch directory"
        )


class TestRecordIsStale:
    """The staleness predicate, exercised directly.

    read_manifest rejects a non-numeric `checked`, so these shapes cannot
    reach the predicate through a manifest; it keeps the check anyway and it
    is pinned here.
    """

    @pytest.mark.parametrize("stamp", ["soon", None, True, [1]])
    def test_unusable_stamp_is_stale(self, tmp_path: Path, stamp: Any):
        """A stamp that is not a real number reads as stale."""
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._record_is_stale({"checked": stamp}) is True

    def test_recent_stamp_is_fresh(self, tmp_path: Path):
        """A stamp from a moment ago is not stale."""
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._record_is_stale({"checked": time.time()}) is False

    def test_future_stamp_is_stale(self, tmp_path: Path):
        """A stamp from the future fails towards refetching."""
        backend = _backend(tmp_path, with_shakemap=True)
        assert backend._record_is_stale({"checked": time.time() + 86400}) is True


class TestSurvivorGuards:
    """Guards a mutation pass found unpinned by the round-4 tests."""

    def test_duplicate_ids_do_not_consume_extra_ceiling(
        self,
        tmp_path: Path,
        fake_fdsn: _FakeFdsn,
        fake_http: _FakeHttp,
        make_event: Any,
    ):
        """A repeated event must not spend the ceiling twice.

        Counting the duplicate would defer a distinct event that the run had
        budget for. The download count alone cannot show this — the manifest
        already stops the second copy being fetched — so the assertion is on
        which events were reached.
        """
        from obspy.core.event import Catalog

        first = make_event(lon=139.0, lat=35.0)
        first.resource_id = _quakeml_id("us1111")
        duplicate = make_event(lon=139.5, lat=35.5)
        duplicate.resource_id = _quakeml_id("us1111")
        other = make_event(lon=141.0, lat=37.0)
        other.resource_id = _quakeml_id("us2222")
        fake_fdsn.default_result = Catalog(events=[first, duplicate, other])

        _backend(tmp_path, with_shakemap=True, max_shakemap_events=2).download(
            progress_bar=False
        )

        reached = sorted(p.name for p in (tmp_path / "shakemap").iterdir())
        assert reached == ["us1111", "us2222"], (
            f"the duplicate must not push us2222 past the ceiling, got {reached}"
        )

    def test_staged_manifest_name_is_process_unique(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The manifest is staged under a name carrying this process id.

        Two runs sharing an output root would otherwise stage the same path and
        rename each other half-written file into place.
        """
        _REPLACE_CALLS.clear()
        monkeypatch.setattr(Path, "replace", _recording_replace)
        _helpers.write_manifest(tmp_path, ["mmi_mean"], ["mmi_mean"], checked=1.0)

        assert _REPLACE_CALLS, "the manifest should be staged then renamed"
        assert str(os.getpid()) in _REPLACE_CALLS[0], (
            f"the staged name should carry the pid, got {_REPLACE_CALLS[0]}"
        )
