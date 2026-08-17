"""Unit tests for the JRC-flood backend (faked pyramids, no network)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from earthlens.jrc_flood import backend as backend_module
from earthlens.jrc_flood.backend import JRCFlood

pytestmark = pytest.mark.jrc_flood

#: The verified EFHM geotransform + a modest raster size for the fakes.
_GT = (-24.54208333, 0.0008333333333333334, 0.0, 71.13375, 0.0, -0.0008333333333333334)


class _FakeSource:
    """Stand-in for a lazily-opened pyramids Dataset over the EFHM."""

    def __init__(self, geo, columns: int, rows: int, no_data_value=(-9999.0,)):
        self.geotransform = geo
        self.columns = columns
        self.rows = rows
        self.no_data_value = no_data_value
        self.reads: list[list[int]] = []

    def read_array(self, window: list[int]) -> np.ndarray:
        """Record the window and return a zero array of its shape."""
        self.reads.append(window)
        _, _, cols, rows = window
        return np.zeros((rows, cols), dtype="float32")

    def close(self) -> None:
        """No-op."""


class _FakeCropped:
    """Stand-in for the rebuilt window Dataset that writes a stub GeoTIFF."""

    def __init__(self, recorder: dict):
        self._recorder = recorder

    def to_file(self, path: str) -> None:
        """Write a tiny stub and record the destination."""
        Path(path).write_bytes(b"II*\x00stub")
        self._recorder.setdefault("written", []).append(path)

    def close(self) -> None:
        """No-op."""


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch pyramids Dataset (read_file + create_from_array) + mask_to_geometry."""
    recorder: dict = {"source": _FakeSource(_GT, 110162, 51992)}

    class _FakeDataset:
        @classmethod
        def read_file(cls, path: str) -> _FakeSource:
            recorder["read_path"] = path
            return recorder["source"]

        @classmethod
        def create_from_array(cls, array, *, geo, epsg, no_data_value) -> _FakeCropped:
            recorder["create"] = {
                "shape": array.shape,
                "geo": geo,
                "epsg": epsg,
                "nodata": no_data_value,
            }
            return _FakeCropped(recorder)

    import pyramids.dataset as pyr_dataset

    monkeypatch.setattr(pyr_dataset, "Dataset", _FakeDataset)
    monkeypatch.setattr(backend_module, "crop_to_aoi", lambda ds, space, **kw: ds)
    return recorder


def _make(tmp_path: Path, **kwargs) -> JRCFlood:
    """Construct a JRCFlood over a Rhine-delta bbox under tmp_path."""
    return JRCFlood(
        lat_lim=[51.8, 52.0],
        lon_lim=[4.8, 5.0],
        path=tmp_path,
        **kwargs,
    )


class TestReturnPeriods:
    """Tests for return-period resolution + validation."""

    def test_default_is_rp100(self, tmp_path: Path):
        """Omitting return_periods defaults to [100]."""
        assert _make(tmp_path)._return_periods == [100]

    @pytest.mark.parametrize(
        "value, expected",
        [(200, 200), ("200", 200), ("RP200", 200), ("rp200", 200)],
    )
    def test_parse_scalar_forms(self, tmp_path: Path, value, expected: int):
        """A scalar int / '200' / 'RP200' all resolve to the int."""
        assert _make(tmp_path, return_periods=value)._return_periods == [expected]

    def test_list_deduped_sorted(self, tmp_path: Path):
        """A list is de-duplicated and sorted."""
        assert _make(tmp_path, return_periods=[200, 100, 200])._return_periods == [
            100,
            200,
        ]

    def test_unknown_rp_raises(self, tmp_path: Path):
        """An unpublished return period raises."""
        with pytest.raises(ValueError, match="not published"):
            _make(tmp_path, return_periods=[123])

    def test_unparseable_rp_raises(self, tmp_path: Path):
        """A non-numeric return-period token raises."""
        with pytest.raises(ValueError, match="could not parse"):
            _make(tmp_path, return_periods=["big"])

    def test_missing_bbox_raises(self):
        """A missing bounding box raises a clear error."""
        with pytest.raises(ValueError, match="bounding box"):
            JRCFlood(path="x")

    def test_antimeridian_aoi_rejected_at_construction(self, tmp_path: Path):
        """An antimeridian-crossing AOI (west > east) is rejected at construction."""
        with pytest.raises(ValueError, match="antimeridian"):
            JRCFlood(lat_lim=[51.8, 52.0], lon_lim=[179.4, -179.8], path=tmp_path)

    def test_aoi_tag_includes_polygon(self, tmp_path: Path):
        """The cache key folds in the real `aoi=` polygon (a GeoDataFrame)."""
        from earthlens.base.cache import aoi_tag
        from earthlens.earthlens import EarthLens

        aoi = {
            "type": "Polygon",
            "coordinates": [
                [[4.8, 51.8], [5.0, 51.8], [5.0, 52.0], [4.8, 52.0], [4.8, 51.8]]
            ],
        }
        bbox_only = aoi_tag(
            EarthLens(
                data_source="jrc-flood",
                lat_lim=[51.8, 52.0],
                lon_lim=[4.8, 5.0],
                return_periods=[100],
                path=tmp_path,
            ).datasource.space
        )
        with_polygon = aoi_tag(
            EarthLens(
                data_source="jrc-flood", aoi=aoi, return_periods=[100], path=tmp_path
            ).datasource.space
        )
        assert with_polygon != bbox_only
        assert "|" in with_polygon


class TestSearch:
    """Tests for the download plan."""

    def test_search_one_product_per_rp(self, tmp_path: Path):
        """Each return period becomes one product with its EFHM URL."""
        plan = _make(tmp_path, return_periods=[100, 200])._search()
        assert [p.id for p in plan] == ["efhm_RP100", "efhm_RP200"]
        assert plan[0].metadata["url"].endswith("Europe_RP100_filled_depth.tif")
        assert plan[1].metadata["rp"] == 200


class TestFetch:
    """Tests for the windowed read / write path."""

    def test_writes_one_geotiff_per_rp(self, tmp_path: Path, fake_pyramids: dict):
        """A download reads the AOI window and writes one GeoTIFF."""
        # #2: the source's own nodata (not the catalog default) is carried through.
        fake_pyramids["source"].no_data_value = (-8888.0,)
        out = _make(tmp_path, return_periods=[100]).download()
        assert out == [tmp_path / "efhm_RP100.tif"]
        assert out[0].exists()
        assert fake_pyramids["source"].reads == [[35210, 22960, 241, 241]]
        assert fake_pyramids["create"]["nodata"] == -8888.0
        # #3: an AOI sidecar is written next to the output.
        assert (tmp_path / "efhm_RP100.tif.aoi").exists()

    def test_outside_coverage_raises(self, tmp_path: Path, fake_pyramids: dict):
        """An AOI outside the EFHM coverage raises rather than writing."""
        fake_pyramids["source"] = _FakeSource(
            (0.0, 0.01, 0.0, 10.0, 0.0, -0.01), 100, 100
        )
        backend = JRCFlood(lat_lim=[50.0, 51.0], lon_lim=[50.0, 51.0], path=tmp_path)
        with pytest.raises(ValueError, match="outside the EFHM"):
            backend.download()

    def test_idempotent_skip_same_aoi(self, tmp_path: Path, fake_pyramids: dict):
        """A re-request for the same AOI skips the windowed read (sidecar match)."""
        _make(tmp_path, return_periods=[100]).download()
        fake_pyramids["source"].reads.clear()
        out = _make(tmp_path, return_periods=[100]).download()
        assert out == [tmp_path / "efhm_RP100.tif"]
        assert fake_pyramids["source"].reads == []

    def test_different_aoi_not_skipped(self, tmp_path: Path, fake_pyramids: dict):
        """A same-path request for a different AOI re-reads (no stale return)."""
        (tmp_path / "efhm_RP100.tif").write_bytes(b"stale")
        (tmp_path / "efhm_RP100.tif.aoi").write_text("9,9,9.1,9.1", encoding="utf-8")
        _make(tmp_path, return_periods=[100]).download()
        assert fake_pyramids["source"].reads, "a different AOI must re-read the window"

    def test_write_failure_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A crash while writing removes the partial file and re-raises."""

        class _Raising:
            def to_file(self, path: str) -> None:
                raise RuntimeError("disk full")

            def close(self) -> None:
                pass

        class _FakeDataset:
            @classmethod
            def read_file(cls, path: str) -> _FakeSource:
                return _FakeSource(_GT, 110162, 51992)

            @classmethod
            def create_from_array(cls, array, *, geo, epsg, no_data_value) -> _Raising:
                return _Raising()

        import pyramids.dataset as pyr_dataset

        monkeypatch.setattr(pyr_dataset, "Dataset", _FakeDataset)
        monkeypatch.setattr(backend_module, "crop_to_aoi", lambda ds, space, **kw: ds)
        backend = _make(tmp_path, return_periods=[100])
        with pytest.raises(RuntimeError, match="disk full"):
            backend.download()
        assert not (tmp_path / "efhm_RP100.part.tif").exists()
        assert not (tmp_path / "efhm_RP100.tif").exists()
