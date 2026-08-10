"""Unit tests for the FABDEM tiling / download helpers (no network)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import requests

from earthlens.fabdem import _helpers as h

pytestmark = pytest.mark.fabdem


class TestCorners:
    """Tests for the SW-corner token formatting via tile_name / bundle_id."""

    @pytest.mark.parametrize(
        "lat, lon, expected",
        [
            (50, 0, "N50E000_FABDEM_V1-2.tif"),
            (51, 1, "N51E001_FABDEM_V1-2.tif"),
            (-11, -73, "S11W073_FABDEM_V1-2.tif"),
            (0, -180, "N00W180_FABDEM_V1-2.tif"),
        ],
    )
    def test_tile_name(self, lat: int, lon: int, expected: str):
        """tile_name zero-pads latitude to 2 and longitude to 3 digits."""
        assert h.tile_name(lat, lon) == expected

    @pytest.mark.parametrize(
        "lat, lon, expected",
        [
            (50, 0, "N50E000-N60E010"),
            (55, 7, "N50E000-N60E010"),
            (-11, -73, "S20W080-S10W070"),
            (-5, -1, "S10W010-N00E000"),
        ],
    )
    def test_bundle_id(self, lat: int, lon: int, expected: str):
        """bundle_id floors the cell to its containing 10-degree block."""
        assert h.bundle_id(lat, lon) == expected

    def test_bundle_url(self):
        """bundle_url builds the deterministic Bristol .zip URL."""
        assert h.bundle_url("N50E000-N60E010") == (
            "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/"
            "N50E000-N60E010_FABDEM_V1-2.zip"
        )


class TestCellsForBbox:
    """Tests for cells_for_bbox."""

    def test_small_box_four_cells(self):
        """A box straddling two degrees on each axis selects four cells."""
        assert h.cells_for_bbox((0.4, 50.4, 1.6, 51.6)) == [
            (50, 0),
            (50, 1),
            (51, 0),
            (51, 1),
        ]

    def test_integer_edge_touch_excluded(self):
        """An edge-only touch at an integer bound is not counted."""
        assert h.cells_for_bbox((0.0, 50.0, 1.0, 51.0)) == [(50, 0)]

    def test_negative_hemisphere(self):
        """A box crossing a degree edge touches both negative-hemisphere cells."""
        assert h.cells_for_bbox((-73.2, -10.6, -72.8, -10.4)) == [
            (-11, -74),
            (-11, -73),
        ]

    def test_outside_grid_clamped_empty(self):
        """A box beyond the valid grid yields no cells."""
        assert h.cells_for_bbox((181.0, 91.0, 182.0, 92.0)) == []


class TestBundlesForBbox:
    """Tests for bundles_for_bbox."""

    def test_single_bundle_groups_tiles(self):
        """One bundle groups all its intersecting 1-degree tiles."""
        plan = h.bundles_for_bbox((0.4, 50.4, 1.6, 51.6))
        assert plan == {
            "N50E000-N60E010": [
                "N50E000_FABDEM_V1-2.tif",
                "N50E001_FABDEM_V1-2.tif",
                "N51E000_FABDEM_V1-2.tif",
                "N51E001_FABDEM_V1-2.tif",
            ]
        }

    def test_multi_bundle_straddle(self):
        """A box crossing a 10-degree edge needs two bundles."""
        plan = h.bundles_for_bbox((9.4, 50.4, 10.6, 50.6))
        assert sorted(plan) == ["N50E000-N60E010", "N50E010-N60E020"]
        assert plan["N50E010-N60E020"] == ["N50E010_FABDEM_V1-2.tif"]

    def test_ocean_only_box_empty_plan(self):
        """An out-of-grid box yields an empty plan."""
        assert h.bundles_for_bbox((181.0, 91.0, 182.0, 92.0)) == {}


class _FakeClient:
    """Stand-in for HttpClient whose download writes a stub or raises."""

    def __init__(self, *, error: Exception | None = None, **_: object):
        self._error = error

    def download(self, url: str, dest: Path, **_: object) -> None:
        """Write a stub zip, or raise the configured error."""
        if self._error is not None:
            raise self._error
        Path(dest).write_bytes(b"PK\x03\x04stub")


def _http_error(status: int) -> requests.HTTPError:
    """Build a requests.HTTPError carrying a response with `status`."""
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


class TestDownloadBundle:
    """Tests for download_bundle."""

    def test_success_writes_zip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A successful download returns the written .zip path."""
        monkeypatch.setattr(h, "HttpClient", lambda **kw: _FakeClient(**kw))
        url = h.bundle_url("N50E000-N60E010")
        out = h.download_bundle(url, tmp_path)
        assert out == tmp_path / "N50E000-N60E010_FABDEM_V1-2.zip"
        assert out.exists()

    def test_404_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A 404 (ocean-only block) returns None instead of raising."""
        monkeypatch.setattr(
            h, "HttpClient", lambda **kw: _FakeClient(error=_http_error(404), **kw)
        )
        assert h.download_bundle(h.bundle_url("S90W180-S80W170"), tmp_path) is None

    def test_500_reraises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A persistent non-404 HTTP error propagates."""
        monkeypatch.setattr(
            h, "HttpClient", lambda **kw: _FakeClient(error=_http_error(500), **kw)
        )
        with pytest.raises(requests.HTTPError):
            h.download_bundle(h.bundle_url("N50E000-N60E010"), tmp_path)

    def test_transport_error_wrapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A transport error is wrapped as an HTTPError with the URL."""
        monkeypatch.setattr(
            h,
            "HttpClient",
            lambda **kw: _FakeClient(error=requests.ConnectionError("boom"), **kw),
        )
        with pytest.raises(requests.HTTPError, match="failed after"):
            h.download_bundle(h.bundle_url("N50E000-N60E010"), tmp_path)

    def test_idempotent_existing_zip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An already-downloaded zip is returned without a client call."""

        def _boom(**_: object) -> _FakeClient:
            raise AssertionError("HttpClient must not be built when the zip exists")

        url = h.bundle_url("N50E000-N60E010")
        (tmp_path / "N50E000-N60E010_FABDEM_V1-2.zip").write_bytes(b"cached")
        monkeypatch.setattr(h, "HttpClient", _boom)
        assert h.download_bundle(url, tmp_path).read_bytes() == b"cached"


def _write_zip(path: Path, names: list[str]) -> None:
    """Write a zip whose members carry the given arcnames + tiny bodies."""
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"II*\x00tif")


class TestExtractTiles:
    """Tests for extract_tiles."""

    def test_selective_extraction(self, tmp_path: Path):
        """Only the requested member names are extracted."""
        zip_path = tmp_path / "bundle.zip"
        dest = tmp_path / "dest"
        _write_zip(
            zip_path,
            ["N50E000_FABDEM_V1-2.tif", "N50E001_FABDEM_V1-2.tif"],
        )
        out = h.extract_tiles(zip_path, dest, ["N50E000_FABDEM_V1-2.tif"])
        assert [p.name for p in out] == ["N50E000_FABDEM_V1-2.tif"]
        assert (dest / "N50E000_FABDEM_V1-2.tif").exists()
        assert not (dest / "N50E001_FABDEM_V1-2.tif").exists()

    def test_missing_member_skipped(self, tmp_path: Path):
        """A requested member absent from the archive is skipped, not errored."""
        zip_path = tmp_path / "bundle.zip"
        dest = tmp_path / "dest"
        _write_zip(zip_path, ["N50E000_FABDEM_V1-2.tif"])
        out = h.extract_tiles(
            zip_path,
            dest,
            ["N50E000_FABDEM_V1-2.tif", "N59E009_FABDEM_V1-2.tif"],
        )
        assert [p.name for p in out] == ["N50E000_FABDEM_V1-2.tif"]

    def test_traversal_member_flattened_safely(self, tmp_path: Path):
        """A path-traversal member is flattened to its basename inside dest, not escaping."""
        zip_path = tmp_path / "evil.zip"
        dest = tmp_path / "dest"
        _write_zip(zip_path, ["../N50E000_FABDEM_V1-2.tif"])
        out = h.extract_tiles(zip_path, dest, ["N50E000_FABDEM_V1-2.tif"])
        assert out == [dest / "N50E000_FABDEM_V1-2.tif"]
        assert (dest / "N50E000_FABDEM_V1-2.tif").exists()
        # The `../` prefix must NOT have written outside dest.
        assert not (tmp_path / "N50E000_FABDEM_V1-2.tif").exists()
