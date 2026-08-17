"""Unit tests for the FABDEM backend (faked download + pyramids)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pyramids.dataset import merge as merge_module

from earthlens.biodiversity import LicenseWarning
from earthlens.fabdem import backend as backend_module
from earthlens.fabdem.backend import FABDEM

pytestmark = pytest.mark.fabdem


class _FakeCropped:
    """Stand-in for a cropped pyramids Dataset that writes a stub GeoTIFF."""

    def __init__(self, recorder: dict):
        self._recorder = recorder

    def to_file(self, path: str) -> None:
        """Write a tiny stub and record the destination."""
        Path(path).write_bytes(b"II*\x00stub-geotiff")
        self._recorder.setdefault("written", []).append(path)

    def close(self) -> None:
        """No-op — the fake holds no GDAL handle."""


class _FakeDataset:
    """Stand-in for `pyramids.dataset.Dataset`, recording the read path."""

    @classmethod
    def read_file(cls, path: str) -> _FakeDataset:
        """Return a fake dataset (no real GDAL)."""
        return cls()

    def close(self) -> None:
        """No-op."""


@pytest.fixture
def fake_localise(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch merge_rasters + pyramids Dataset + crop_to_aoi to touch no GDAL."""
    recorder: dict = {}

    def _fake_merge(*, src, dst, dst_crs, resampling, no_data_value=None) -> None:
        recorder["merge"] = {
            "src": list(src),
            "dst_crs": dst_crs,
            "no_data_value": no_data_value,
        }
        Path(dst).write_bytes(b"II*\x00merged")

    def _fake_crop(dataset, space, *, bbox, touch):
        recorder["crop_bbox"] = bbox
        return _FakeCropped(recorder)

    import pyramids.dataset as pyr_dataset

    monkeypatch.setattr(merge_module, "merge_rasters", _fake_merge)
    monkeypatch.setattr(pyr_dataset, "Dataset", _FakeDataset)
    monkeypatch.setattr(backend_module, "crop_to_aoi", _fake_crop)
    return recorder


def _fake_extract(zip_path, dest, names) -> list[Path]:
    """Fake extract_tiles: write a stub for each wanted tile and return the paths."""
    out: list[Path] = []
    for name in names:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"II*\x00tile")
        out.append(target)
    return out


def _make(tmp_path: Path, **kwargs) -> FABDEM:
    """Construct a FABDEM over a small Channel-coast bbox under tmp_path."""
    return FABDEM(
        lat_lim=[50.4, 50.6],
        lon_lim=[0.4, 0.6],
        path=tmp_path,
        **kwargs,
    )


class TestInit:
    """Tests for construction + validation."""

    def test_missing_bbox_raises(self):
        """A missing bounding box raises a clear error."""
        with pytest.raises(ValueError, match="bounding box"):
            FABDEM(path="x")

    def test_single_elevation_band(self, tmp_path: Path):
        """The facet-only backend fixes the single elevation band."""
        assert _make(tmp_path).vars == ["elevation"]


class TestSearch:
    """Tests for the download plan."""

    def test_search_single_bundle(self, tmp_path: Path):
        """A small AOI resolves to one bundle carrying its tiles + URL."""
        plan = _make(tmp_path)._search()
        assert [p.id for p in plan] == ["N50E000-N60E010"]
        assert plan[0].metadata["tiles"] == ["N50E000_FABDEM_V1-2.tif"]
        assert plan[0].metadata["url"].endswith("N50E000-N60E010_FABDEM_V1-2.zip")

    def test_empty_plan_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """An AOI intersecting no land grid cell raises."""
        monkeypatch.setattr(backend_module, "bundles_for_bbox", lambda bbox: {})
        backend = _make(tmp_path)
        with pytest.raises(ValueError, match="no FABDEM land tiles"):
            backend._search()


class TestDownload:
    """Tests for the download / fetch / localise path."""

    def test_download_emits_license_warning_and_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_localise: dict
    ):
        """download emits a LicenseWarning and writes one cropped GeoTIFF."""
        monkeypatch.setattr(
            backend_module, "download_bundle", lambda url, dest: dest / "b.zip"
        )
        monkeypatch.setattr(
            backend_module,
            "extract_tiles",
            _fake_extract,
        )
        with pytest.warns(LicenseWarning, match="non-commercial"):
            out = _make(tmp_path).download()
        assert out == [tmp_path / "fabdem_V1-2.tif"]
        assert out[0].exists()
        # #1: the source nodata (-9999) is stamped, not merge_rasters' default 0.
        assert fake_localise["merge"]["no_data_value"] == -9999.0
        # #3: an AOI sidecar is written next to the output.
        assert (tmp_path / "fabdem_V1-2.tif.aoi").exists()

    def test_ocean_bundle_skipped_then_empty_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_localise: dict
    ):
        """An ocean-only AOI raises and emits no (spurious) licence warning."""
        import warnings

        monkeypatch.setattr(backend_module, "download_bundle", lambda url, dest: None)
        monkeypatch.setattr(
            backend_module,
            "extract_tiles",
            lambda zip_path, dest, names: [],
        )
        backend = _make(tmp_path)
        with warnings.catch_warnings():
            warnings.simplefilter("error", LicenseWarning)
            with pytest.raises(ValueError, match="no published 1"):
                backend.download()

    def test_idempotent_skip_same_aoi(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_localise: dict
    ):
        """A re-request for the same AOI skips the download (sidecar match)."""
        monkeypatch.setattr(
            backend_module, "download_bundle", lambda url, dest: dest / "b.zip"
        )
        monkeypatch.setattr(
            backend_module,
            "extract_tiles",
            _fake_extract,
        )
        with pytest.warns(LicenseWarning):
            _make(tmp_path).download()

        def _boom(url, dest):
            raise AssertionError("must not download for the same cached AOI")

        monkeypatch.setattr(backend_module, "download_bundle", _boom)
        with pytest.warns(LicenseWarning):
            out = _make(tmp_path).download()
        assert out == [tmp_path / "fabdem_V1-2.tif"]

    def test_different_aoi_not_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_localise: dict
    ):
        """A same-path request for a different AOI re-fetches (no stale return)."""
        # A stale output + sidecar from a different AOI must not be reused.
        (tmp_path / "fabdem_V1-2.tif").write_bytes(b"stale")
        (tmp_path / "fabdem_V1-2.tif.aoi").write_text("9,9,9.1,9.1", encoding="utf-8")
        calls: list[str] = []
        monkeypatch.setattr(
            backend_module,
            "download_bundle",
            lambda url, dest: calls.append(url) or (dest / "b.zip"),
        )
        monkeypatch.setattr(
            backend_module,
            "extract_tiles",
            _fake_extract,
        )
        with pytest.warns(LicenseWarning):
            _make(tmp_path).download()
        assert calls, "a different-AOI request must re-fetch, not return the stale file"

    def test_cached_tile_skips_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_localise: dict
    ):
        """An already-extracted tile is reused without re-downloading the bundle."""
        cache = tmp_path / ".fabdem_cache"
        cache.mkdir(parents=True)
        (cache / "N50E000_FABDEM_V1-2.tif").write_bytes(b"II*\x00tile")

        def _boom(url, dest):
            raise AssertionError("must not download when the tile is already cached")

        monkeypatch.setattr(backend_module, "download_bundle", _boom)
        with pytest.warns(LicenseWarning):
            out = _make(tmp_path).download()
        assert out == [tmp_path / "fabdem_V1-2.tif"]
        assert out[0].exists()

    def test_second_aoi_same_bundle_extracts_new_tile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_localise: dict
    ):
        """A later AOI needing a different tile in the same bundle still extracts it.

        Regression for the removed `.fetched` marker, which returned an
        incomplete mosaic (or a false 'ocean-only' error) for the second AOI.
        """
        monkeypatch.setattr(
            backend_module, "download_bundle", lambda url, dest: dest / "b.zip"
        )
        monkeypatch.setattr(backend_module, "extract_tiles", _fake_extract)
        with pytest.warns(LicenseWarning):
            FABDEM(lat_lim=[50.4, 50.6], lon_lim=[0.4, 0.6], path=tmp_path).download()
        # A different 1° cell (55, 5) in the SAME 10° bundle N50E000-N60E010.
        with pytest.warns(LicenseWarning):
            out = FABDEM(
                lat_lim=[55.4, 55.6], lon_lim=[5.4, 5.6], path=tmp_path
            ).download()
        assert out[0].exists()
        assert (tmp_path / ".fabdem_cache" / "N55E005_FABDEM_V1-2.tif").exists()

    def test_antimeridian_aoi_rejected_at_construction(self, tmp_path: Path):
        """An antimeridian-crossing AOI (west > east) is rejected at construction."""
        with pytest.raises(ValueError, match="antimeridian"):
            FABDEM(lat_lim=[-17.6, -17.4], lon_lim=[179.4, -179.8], path=tmp_path)

    def test_aoi_tag_includes_polygon(self, tmp_path: Path):
        """The cache key folds in the real `aoi=` polygon (a GeoDataFrame)."""
        from earthlens.base.cache import aoi_tag
        from earthlens.earthlens import EarthLens

        aoi = {
            "type": "Polygon",
            "coordinates": [
                [[0.4, 50.4], [0.6, 50.4], [0.6, 50.6], [0.4, 50.6], [0.4, 50.4]]
            ],
        }
        bbox_only = aoi_tag(
            EarthLens(
                data_source="fabdem",
                lat_lim=[50.4, 50.6],
                lon_lim=[0.4, 0.6],
                path=tmp_path,
            ).datasource.space
        )
        with_polygon = aoi_tag(
            EarthLens(data_source="fabdem", aoi=aoi, path=tmp_path).datasource.space
        )
        assert with_polygon != bbox_only
        assert "|" in with_polygon

    def test_force_rewrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_localise: dict
    ):
        """force=True re-fetches even when an output exists."""
        (tmp_path / "fabdem_V1-2.tif").write_bytes(b"old")
        monkeypatch.setattr(
            backend_module, "download_bundle", lambda url, dest: dest / "b.zip"
        )
        monkeypatch.setattr(
            backend_module,
            "extract_tiles",
            _fake_extract,
        )
        with pytest.warns(LicenseWarning):
            out = _make(tmp_path).download(force=True)
        assert out[0].read_bytes() != b"old"

    def test_localise_write_failure_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A crash while writing removes the partial file and re-raises."""
        import pyramids.dataset as pyr_dataset
        from pyramids.dataset import merge as merge_module

        class _Raising:
            def to_file(self, path: str) -> None:
                raise RuntimeError("disk full")

            def close(self) -> None:
                pass

        monkeypatch.setattr(
            merge_module,
            "merge_rasters",
            lambda *, src, dst, dst_crs, resampling, no_data_value=None: Path(
                dst
            ).write_bytes(b"m"),
        )
        monkeypatch.setattr(pyr_dataset, "Dataset", _FakeDataset)
        monkeypatch.setattr(
            backend_module, "crop_to_aoi", lambda ds, space, **kw: _Raising()
        )
        monkeypatch.setattr(
            backend_module, "download_bundle", lambda url, dest: dest / "b.zip"
        )
        monkeypatch.setattr(
            backend_module,
            "extract_tiles",
            _fake_extract,
        )
        # The write fails inside _fetch, before the (post-fetch) licence warning.
        backend = _make(tmp_path)
        with pytest.raises(RuntimeError, match="disk full"):
            backend.download()
        assert not (tmp_path / "fabdem_V1-2.part.tif").exists()
        assert not (tmp_path / "fabdem_V1-2.tif").exists()
        # #5: the merge intermediate is cleaned up even on a write failure.
        assert not (tmp_path / ".fabdem_cache" / "fabdem_merged.tif").exists()
