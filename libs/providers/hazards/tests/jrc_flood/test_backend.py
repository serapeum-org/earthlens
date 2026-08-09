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

    def __init__(self, geo, columns: int, rows: int):
        self.geotransform = geo
        self.columns = columns
        self.rows = rows
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
    monkeypatch.setattr(backend_module, "mask_to_geometry", lambda ds, space: ds)
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
        out = _make(tmp_path, return_periods=[100]).download()
        assert out == [tmp_path / "efhm_RP100.tif"]
        assert out[0].exists()
        assert fake_pyramids["source"].reads == [[35210, 22960, 241, 241]]
        assert fake_pyramids["create"]["nodata"] == -9999.0

    def test_outside_coverage_raises(self, tmp_path: Path, fake_pyramids: dict):
        """An AOI outside the EFHM coverage raises rather than writing."""
        fake_pyramids["source"] = _FakeSource(
            (0.0, 0.01, 0.0, 10.0, 0.0, -0.01), 100, 100
        )
        with pytest.raises(ValueError, match="outside the EFHM"):
            JRCFlood(
                lat_lim=[50.0, 51.0], lon_lim=[50.0, 51.0], path=tmp_path
            ).download()

    def test_idempotent_skip(self, tmp_path: Path, fake_pyramids: dict):
        """An existing output is returned without a windowed read."""
        (tmp_path / "efhm_RP100.tif").write_bytes(b"cached")
        out = _make(tmp_path, return_periods=[100]).download()
        assert out == [tmp_path / "efhm_RP100.tif"]
        assert fake_pyramids["source"].reads == []

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
        monkeypatch.setattr(backend_module, "mask_to_geometry", lambda ds, space: ds)
        with pytest.raises(RuntimeError, match="disk full"):
            _make(tmp_path, return_periods=[100]).download()
        assert not (tmp_path / "efhm_RP100.part.tif").exists()
        assert not (tmp_path / "efhm_RP100.tif").exists()
