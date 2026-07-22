"""Offline unit tests for the OSM `pbf` fetch + read helpers (`earthlens.osm._pbf`).

Both engines' SDKs (`pyrosm`, `osmium`) live in the `osm-pbf` extra, which is
out of `[all]`, so these tests **fake** them (monkeypatched `sys.modules`) and
fake the `HttpClient` — no network, no real PBF file. Coverage spans the
Geofabrik URL grammar, cache reuse + md5 verification, the large-file warning,
the pyrosm layer dispatch + bbox clip + size guard, and the pyosmium streaming
strategies.
"""

from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from earthlens.osm._pbf import (
    download_extract,
    geofabrik_url,
    read_pbf,
)
from shapely.geometry import LineString, Point, Polygon, box

from earthlens.osm import _pbf

#: The fixed extract payload the fake `download` writes; the fake md5 sidecar
#: serves its digest so a fresh download verifies clean.
_PAYLOAD = b"fake-osm-pbf-bytes"
_PAYLOAD_MD5 = hashlib.md5(_PAYLOAD).hexdigest()  # noqa: S324


class FakeResponse:
    """Minimal stand-in for a `requests.Response` (`.text` / `.headers`)."""

    def __init__(self, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.text = text
        self.headers = headers or {}


class FakeHttp:
    """Records GET / HEAD / download calls and serves canned responses.

    Drives :func:`download_extract` without a network: `get` returns the md5
    sidecar body, `request("HEAD", ...)` returns a `Content-Length`, and
    `download` writes `_PAYLOAD` to the destination.
    """

    def __init__(
        self,
        *,
        md5_body: str | None = f"{_PAYLOAD_MD5}  malta-latest.osm.pbf",
        head_length: int = len(_PAYLOAD),
        get_raises: bool = False,
        head_raises: bool = False,
        payload: bytes = _PAYLOAD,
    ) -> None:
        self._md5_body = md5_body
        self._head_length = head_length
        self._get_raises = get_raises
        self._head_raises = head_raises
        self._payload = payload
        self.get_calls: list[str] = []
        self.download_calls: list[str] = []
        self.head_calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append(url)
        if self._get_raises:
            raise RuntimeError("sidecar unreachable")
        return FakeResponse(text=self._md5_body or "")

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.head_calls.append(url)
        if self._head_raises:
            raise RuntimeError("HEAD unreachable")
        return FakeResponse(headers={"Content-Length": str(self._head_length)})

    def download(self, url: str, dest: Any, **kwargs: Any) -> Path:
        self.download_calls.append(url)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._payload)
        return dest


class TestGeofabrikUrl:
    """The Geofabrik URL grammar."""

    def test_appends_latest_suffix(self):
        """A region path becomes a `-latest.osm.pbf` URL under the base."""
        assert geofabrik_url("europe/malta") == (
            "https://download.geofabrik.de/europe/malta-latest.osm.pbf"
        )


class TestDownloadExtract:
    """Fetch-and-cache with md5 verification and the large-file warning."""

    def test_fresh_download_writes_and_verifies(self, tmp_path):
        """A first fetch downloads, writes the file, and passes the md5 check."""
        http = FakeHttp()
        dest = download_extract("europe/malta", tmp_path, http=http, progress=False)
        assert dest.exists() and dest.read_bytes() == _PAYLOAD
        assert http.download_calls == [geofabrik_url("europe/malta")]

    def test_cache_hit_skips_download(self, tmp_path):
        """A cached file with a matching md5 is reused without re-downloading."""
        http = FakeHttp()
        download_extract("europe/malta", tmp_path, http=http, progress=False)
        http2 = FakeHttp()
        download_extract("europe/malta", tmp_path, http=http2, progress=False)
        assert http2.download_calls == []

    def test_existing_cache_is_trusted_without_network(self, tmp_path):
        """A present, non-empty cached file is reused — no download, no sidecar."""
        dest = tmp_path / "europe_malta-latest.osm.pbf"
        dest.write_bytes(b"already-here")
        http = FakeHttp()
        out = download_extract("europe/malta", tmp_path, http=http, progress=False)
        assert out == dest
        assert http.download_calls == [] and http.get_calls == []

    def test_empty_cache_file_is_redownloaded(self, tmp_path):
        """A zero-byte cached file is treated as a miss and re-downloaded."""
        dest = tmp_path / "europe_malta-latest.osm.pbf"
        dest.write_bytes(b"")
        http = FakeHttp()
        download_extract("europe/malta", tmp_path, http=http, progress=False)
        assert http.download_calls == [geofabrik_url("europe/malta")]

    def test_md5_mismatch_after_download_raises_and_removes(self, tmp_path):
        """A downloaded file that fails the md5 check is removed and raises."""
        http = FakeHttp(md5_body="deadbeef  malta-latest.osm.pbf")
        with pytest.raises(ValueError, match="MD5 mismatch"):
            download_extract("europe/malta", tmp_path, http=http, progress=False)
        assert not (tmp_path / "europe_malta-latest.osm.pbf").exists()

    def test_unreadable_sidecar_skips_check(self, tmp_path):
        """A flaky md5 sidecar (GET raises) is tolerated; the download stands."""
        http = FakeHttp(get_raises=True)
        dest = download_extract("europe/malta", tmp_path, http=http, progress=False)
        assert dest.exists()

    def test_large_extract_warns(self, tmp_path):
        """A HEAD Content-Length over the threshold logs a large-file warning."""
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            http = FakeHttp(head_length=_pbf.LARGE_FILE_WARN_BYTES + 1)
            download_extract("europe/malta", tmp_path, http=http, progress=False)
        finally:
            logger.remove(sink_id)
        assert any("large" in message.lower() for message in messages)

    def test_default_http_client_is_built(self, tmp_path, monkeypatch):
        """Passing no client builds a default HttpClient (patched here)."""
        http = FakeHttp()
        monkeypatch.setattr(_pbf, "HttpClient", lambda *a, **k: http)
        download_extract("europe/malta", tmp_path, progress=False)
        assert http.download_calls == [geofabrik_url("europe/malta")]

    def test_head_failure_is_tolerated(self, tmp_path):
        """A failed size HEAD does not block the download."""
        http = FakeHttp(head_raises=True)
        dest = download_extract("europe/malta", tmp_path, http=http, progress=False)
        assert dest.exists()


# --- pyrosm engine fakes -----------------------------------------------------


class FakePyrosmOSM:
    """Stand-in for `pyrosm.OSM`: records the bbox and serves a preset frame."""

    #: Set by a test to the GeoDataFrame each `get_*` returns (or `None`).
    frame: Any = None
    last_bbox: Any = None

    def __init__(self, path: str, bounding_box: Any = None) -> None:
        FakePyrosmOSM.last_bbox = bounding_box

    def get_buildings(self):
        return FakePyrosmOSM.frame

    def get_network(self, network_type: str | None = None):
        FakePyrosmOSM.last_network_type = network_type
        return FakePyrosmOSM.frame

    def get_landuse(self):
        return FakePyrosmOSM.frame


@pytest.fixture
def fake_pyrosm(monkeypatch):
    """Install a fake `pyrosm` module exposing `OSM`."""
    module = types.ModuleType("pyrosm")
    module.OSM = FakePyrosmOSM
    monkeypatch.setitem(sys.modules, "pyrosm", module)
    return FakePyrosmOSM


def _buildings_frame():
    """Build a one-row WGS84 GeoDataFrame with pyrosm's native `id` column."""
    import geopandas as gpd

    return gpd.GeoDataFrame(
        {"id": [42], "building": ["yes"]},
        geometry=[box(0.0, 0.0, 1.0, 1.0)],
        crs="EPSG:4326",
    )


class TestReadPyrosm:
    """The in-memory `pyrosm` engine path."""

    def test_get_buildings_dispatch(self, tmp_path, fake_pyrosm):
        """`pbf:buildings` maps to `get_buildings`, wraps + normalises `id`."""
        fake_pyrosm.frame = _buildings_frame()
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        fc = read_pbf(pbf, pyrosm_method="get_buildings", engine="pyrosm")
        assert len(fc) == 1 and fc.crs.to_epsg() == 4326
        # pyrosm's `id` is normalised to `osm_id` for a uniform identity column.
        assert "osm_id" in fc.columns and "id" not in fc.columns

    def test_network_type_forwarded(self, tmp_path, fake_pyrosm):
        """`get_network` receives the row's `network_type`."""
        fake_pyrosm.frame = _buildings_frame()
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        read_pbf(
            pbf, pyrosm_method="get_network", network_type="driving", engine="pyrosm"
        )
        assert fake_pyrosm.last_network_type == "driving"

    def test_bbox_becomes_shapely_box(self, tmp_path, fake_pyrosm):
        """A bbox tuple is handed to pyrosm as a shapely box in W,S,E,N order."""
        fake_pyrosm.frame = _buildings_frame()
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        read_pbf(
            pbf,
            pyrosm_method="get_buildings",
            bbox=(14.4, 35.8, 14.6, 36.0),
            engine="pyrosm",
        )
        assert fake_pyrosm.last_bbox.bounds == (14.4, 35.8, 14.6, 36.0)

    def test_empty_result_is_schema_only(self, tmp_path, fake_pyrosm):
        """A `None` / empty pyrosm result yields an empty FeatureCollection."""
        fake_pyrosm.frame = None
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        fc = read_pbf(pbf, pyrosm_method="get_buildings", engine="pyrosm")
        assert len(fc) == 0 and "osm_type" in fc.columns

    def test_oversized_file_refuses_pyrosm(self, tmp_path, fake_pyrosm, monkeypatch):
        """An extract above the pyrosm cap is refused with a pyosmium hint."""
        pbf = tmp_path / "big.osm.pbf"
        pbf.write_bytes(b"x")
        monkeypatch.setattr(_pbf, "MAX_PYROSM_BYTES", 0)
        with pytest.raises(ValueError, match="pyosmium"):
            read_pbf(pbf, pyrosm_method="get_buildings", engine="pyrosm")


class TestReadEngineSelection:
    """Engine routing and validation."""

    def test_unknown_engine_raises(self, tmp_path):
        """An unknown engine name is rejected."""
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        with pytest.raises(ValueError, match="pyrosm.*pyosmium"):
            read_pbf(pbf, pyrosm_method="get_buildings", engine="bogus")

    def test_pyosmium_unmapped_layer_raises(self, tmp_path, fake_osmium):
        """A pyosmium read for a layer with no streaming plan is rejected."""
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        with pytest.raises(ValueError, match="no pyosmium read plan"):
            read_pbf(pbf, pyrosm_method="get_boundaries_extra", engine="pyosmium")


# --- pyosmium engine fakes ---------------------------------------------------


class FakeLocation:
    """Stand-in for an osmium node location."""

    def __init__(self, lon: float, lat: float, valid: bool = True) -> None:
        self.lon = lon
        self.lat = lat
        self._valid = valid

    def valid(self) -> bool:
        return self._valid


class FakeNode:
    """Stand-in for a tagged osmium node."""

    def __init__(self, nid: int, lon: float, lat: float, valid: bool = True) -> None:
        self.id = nid
        self.location = FakeLocation(lon, lat, valid)

    def is_node(self) -> bool:
        return True

    def is_way(self) -> bool:
        return False


class FakeWay:
    """Stand-in for a tagged osmium way carrying its coordinates + tags."""

    def __init__(
        self,
        wid: int,
        coords: list[tuple[float, float]],
        tags: dict[str, str] | None = None,
    ) -> None:
        self.id = wid
        self.coords = coords
        self.tags = tags or {}

    def is_node(self) -> bool:
        return False

    def is_way(self) -> bool:
        return True


class FakeArea:
    """Stand-in for an osmium area (assembled polygon)."""

    def __init__(self, orig: int, from_way: bool, geometry: Polygon) -> None:
        self._orig = orig
        self._from_way = from_way
        self.geometry = geometry

    def orig_id(self) -> int:
        return self._orig

    def from_way(self) -> bool:
        return self._from_way


class FakeFileProcessor:
    """Stand-in for `osmium.FileProcessor` yielding preset objects."""

    #: Objects the next-constructed processor iterates (set per test).
    objects: list[Any] = []

    def __init__(self, path: str) -> None:
        self._objects = list(FakeFileProcessor.objects)

    def with_filter(self, _filter: Any) -> FakeFileProcessor:
        return self

    def with_locations(self) -> FakeFileProcessor:
        return self

    def with_areas(self) -> FakeFileProcessor:
        return self

    def __iter__(self):
        return iter(self._objects)


class FakeWKBFactory:
    """Stand-in for `osmium.geom.WKBFactory` building WKB hex from fakes."""

    def create_linestring(self, obj: FakeWay) -> str:
        return LineString(obj.coords).wkb.hex()

    def create_multipolygon(self, obj: FakeArea) -> str:
        return obj.geometry.wkb.hex()


@pytest.fixture
def fake_osmium(monkeypatch):
    """Install a fake `osmium` module driving `_stream_geometries`."""
    module = types.ModuleType("osmium")
    filter_mod = types.SimpleNamespace(KeyFilter=lambda key: key)
    geom_mod = types.SimpleNamespace(WKBFactory=FakeWKBFactory)
    osm_mod = types.SimpleNamespace(Area=FakeArea)
    module.filter = filter_mod
    module.geom = geom_mod
    module.osm = osm_mod
    module.FileProcessor = FakeFileProcessor
    monkeypatch.setitem(sys.modules, "osmium", module)
    return FakeFileProcessor


class TestReadPyosmium:
    """The streaming `pyosmium` engine path and its geometry strategies."""

    def test_point_layer(self, tmp_path, fake_osmium):
        """A point layer (`get_pois`) yields `Point`s from tagged nodes."""
        fake_osmium.objects = [
            FakeNode(1, 14.5, 35.9),
            FakeNode(2, 0.0, 0.0, valid=False),
        ]
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        fc = read_pbf(pbf, pyrosm_method="get_pois", engine="pyosmium")
        assert len(fc) == 1 and fc.geometry.iloc[0].geom_type == "Point"

    def test_line_layer(self, tmp_path, fake_osmium):
        """A line layer (`get_network`) yields `LineString`s from tagged ways."""
        fake_osmium.objects = [FakeWay(10, [(0.0, 0.0), (1.0, 1.0)])]
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        fc = read_pbf(pbf, pyrosm_method="get_network", engine="pyosmium")
        assert len(fc) == 1 and fc.geometry.iloc[0].geom_type == "LineString"

    def test_driving_network_type_filters_by_highway_value(self, tmp_path, fake_osmium):
        """network_type='driving' keeps drivable ways and drops a footway (M1)."""
        fake_osmium.objects = [
            FakeWay(1, [(0.0, 0.0), (1.0, 1.0)], {"highway": "residential"}),
            FakeWay(2, [(0.0, 0.0), (1.0, 1.0)], {"highway": "footway"}),
        ]
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        fc = read_pbf(
            pbf,
            pyrosm_method="get_network",
            network_type="driving",
            engine="pyosmium",
        )
        assert len(fc) == 1 and fc.osm_id.iloc[0] == 1

    def test_unknown_network_type_warns_and_keeps_all(self, tmp_path, fake_osmium):
        """A non-driving network_type is not filtered and logs a warning."""
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        fake_osmium.objects = [
            FakeWay(1, [(0.0, 0.0), (1.0, 1.0)], {"highway": "footway"})
        ]
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        try:
            fc = read_pbf(
                pbf,
                pyrosm_method="get_network",
                network_type="walking",
                engine="pyosmium",
            )
        finally:
            logger.remove(sink_id)
        assert len(fc) == 1
        assert any("does not replicate" in message for message in messages)

    def test_area_layer(self, tmp_path, fake_osmium):
        """An area layer (`get_buildings`) yields polygons from tagged areas."""
        poly = box(0.0, 0.0, 1.0, 1.0)
        fake_osmium.objects = [FakeArea(20, True, poly)]
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        fc = read_pbf(pbf, pyrosm_method="get_buildings", engine="pyosmium")
        assert len(fc) == 1 and fc.osm_type.iloc[0] == "way"

    def test_bbox_clips_geometry(self, tmp_path, fake_osmium):
        """A bbox that misses the geometry drops it from the result."""
        fake_osmium.objects = [FakeNode(1, 100.0, 80.0)]
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        fc = read_pbf(
            pbf, pyrosm_method="get_pois", bbox=(0.0, 0.0, 1.0, 1.0), engine="pyosmium"
        )
        assert len(fc) == 0

    def test_degenerate_geometry_is_skipped(self, tmp_path, fake_osmium):
        """A way the factory cannot build (one coord) is skipped, not fatal."""
        fake_osmium.objects = [FakeWay(11, [(0.0, 0.0)])]
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"x")
        fc = read_pbf(pbf, pyrosm_method="get_network", engine="pyosmium")
        assert len(fc) == 0
