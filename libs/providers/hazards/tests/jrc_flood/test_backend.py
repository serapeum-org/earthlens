"""Unit tests for the JRC-flood backend (faked pyramids, no network)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pyramids.dataset import Dataset

from earthlens.jrc_flood import backend as backend_module
from earthlens.jrc_flood.backend import JRCFlood

pytestmark = pytest.mark.jrc_flood

#: The verified EFHM geotransform + a modest raster size for the fakes.
_GT = (-24.54208333, 0.0008333333333333334, 0.0, 71.13375, 0.0, -0.0008333333333333334)


class _FakeCropped:
    """Stand-in for the cropped window Dataset that writes a stub GeoTIFF."""

    def __init__(self, recorder: dict):
        self._recorder = recorder

    def to_file(self, path: str) -> None:
        """Write a tiny stub and record the destination."""
        Path(path).write_bytes(b"II*\x00stub")
        self._recorder.setdefault("written", []).append(path)

    def close(self) -> None:
        """No-op."""


class _FakeSource:
    """Stand-in for a lazily-opened pyramids Dataset over the EFHM."""

    def __init__(
        self, geo, columns: int, rows: int, no_data_value=(-9999.0,), recorder=None
    ):
        self.geotransform = geo
        self.columns = columns
        self.rows = rows
        self.no_data_value = no_data_value
        self.crops: list = []
        self._recorder = recorder if recorder is not None else {}

    def crop(self, mask=None, touch=True, *, bbox=None, epsg=None) -> _FakeCropped:
        """Record the windowed bbox crop and return a writable fake window."""
        self.crops.append(bbox)
        return _FakeCropped(self._recorder)

    def close(self) -> None:
        """No-op."""


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch pyramids Dataset (read_file + windowed crop) + crop_to_aoi."""
    recorder: dict = {}
    recorder["source"] = _FakeSource(_GT, 110162, 51992, recorder=recorder)

    class _FakeDataset:
        @classmethod
        def read_file(cls, path: str) -> _FakeSource:
            recorder["read_path"] = path
            return recorder["source"]

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
        """A download windowed-crops the AOI and writes one GeoTIFF."""
        out = _make(tmp_path, return_periods=[100]).download()
        assert out == [tmp_path / "efhm_RP100.tif"]
        assert out[0].exists()
        # The AOI bbox is forwarded to the windowed crop (nodata / grid carry
        # through is pyramids' own concern now, covered by its tests).
        assert fake_pyramids["source"].crops == [[4.8, 51.8, 5.0, 52.0]]
        # An AOI sidecar is written next to the output.
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
        fake_pyramids["source"].crops.clear()
        out = _make(tmp_path, return_periods=[100]).download()
        assert out == [tmp_path / "efhm_RP100.tif"]
        assert fake_pyramids["source"].crops == []

    def test_different_aoi_not_skipped(self, tmp_path: Path, fake_pyramids: dict):
        """A same-path request for a different AOI re-reads (no stale return)."""
        (tmp_path / "efhm_RP100.tif").write_bytes(b"stale")
        (tmp_path / "efhm_RP100.tif.aoi").write_text("9,9,9.1,9.1", encoding="utf-8")
        _make(tmp_path, return_periods=[100]).download()
        assert fake_pyramids["source"].crops, "a different AOI must re-read the window"

    def test_write_failure_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A crash while writing removes the partial file and re-raises."""

        class _Raising:
            def to_file(self, path: str) -> None:
                raise RuntimeError("disk full")

            def close(self) -> None:
                pass

        class _RaisingSource(_FakeSource):
            def crop(self, mask=None, touch=True, *, bbox=None, epsg=None) -> _Raising:
                return _Raising()

        class _FakeDataset:
            @classmethod
            def read_file(cls, path: str) -> _RaisingSource:
                return _RaisingSource(_GT, 110162, 51992)

        import pyramids.dataset as pyr_dataset

        monkeypatch.setattr(pyr_dataset, "Dataset", _FakeDataset)
        monkeypatch.setattr(backend_module, "crop_to_aoi", lambda ds, space, **kw: ds)
        backend = _make(tmp_path, return_periods=[100])
        with pytest.raises(RuntimeError, match="disk full"):
            backend.download()
        assert not (tmp_path / "efhm_RP100.part.tif").exists()
        assert not (tmp_path / "efhm_RP100.tif").exists()


def _write_efhm_geotiff(
    path: Path, *, no_data_value: float = -9999.0, fill: float | None = None
) -> None:
    """Write an EFHM-like 4326 GeoTIFF covering lon 4..6, lat 51..53.

    `fill` writes a constant raster (use the no-data value for an all-no-data
    source); otherwise a ramp of distinct values.
    """
    if fill is None:
        arr = np.arange(200 * 200, dtype="float32").reshape(200, 200)
    else:
        arr = np.full((200, 200), fill, dtype="float32")
    Dataset.create_from_array(
        arr,
        top_left_corner=(4.0, 53.0),
        cell_size=0.01,
        epsg=4326,
        no_data_value=no_data_value,
    ).to_file(str(path))


class TestFetchReal:
    """`_fetch_one` against a real (local) pyramids raster — no fakes.

    Pins the delegated crop(bbox=) contract (windowed read + real two-crop trim +
    no-data) that the faked-`crop` tests cannot exercise.
    """

    def test_windowed_crop_writes_subset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A normal AOI writes a small crop carrying the source's own no-data."""
        src = tmp_path / "efhm.tif"
        # A distinctive source no-data (not the catalog -9999 fallback) so the
        # assertion fails if source-carry-through breaks and the fallback stamps.
        _write_efhm_geotiff(src, no_data_value=-8888.0)
        monkeypatch.setattr(backend_module, "efhm_url", lambda rp, **kw: str(src))
        out = JRCFlood(
            lat_lim=[51.8, 52.0],
            lon_lim=[4.8, 5.0],
            return_periods=[100],
            path=tmp_path,
        ).download()
        assert out == [tmp_path / "efhm_RP100.tif"]
        result = Dataset.read_file(str(out[0]))
        assert result.rows < 200, "only the AOI window read"
        assert result.columns < 200, "only the AOI window read"
        assert result.no_data_value[0] == -8888.0, "source no-data carried through"

    def test_all_nodata_aoi_writes_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An in-coverage but all-no-data AOI writes a raster instead of raising."""
        src = tmp_path / "efhm.tif"
        _write_efhm_geotiff(src, no_data_value=-9999.0, fill=-9999.0)
        monkeypatch.setattr(backend_module, "efhm_url", lambda rp, **kw: str(src))
        out = JRCFlood(
            lat_lim=[51.8, 52.0],
            lon_lim=[4.8, 5.0],
            return_periods=[100],
            path=tmp_path,
        ).download()
        assert out[0].exists(), "an all-no-data AOI still writes a file"
        result = Dataset.read_file(str(out[0]))
        assert bool((result.read_array() == -9999.0).all()), "written all-no-data"

    def test_point_aoi_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A degenerate point AOI writes a small crop instead of raising (review H1)."""
        src = tmp_path / "efhm.tif"
        _write_efhm_geotiff(src)
        monkeypatch.setattr(backend_module, "efhm_url", lambda rp, **kw: str(src))
        out = JRCFlood(
            lat_lim=[51.9, 51.9],
            lon_lim=[4.9, 4.9],
            return_periods=[100],
            path=tmp_path,
        ).download()
        assert out[0].exists(), "a point AOI still writes a crop"
