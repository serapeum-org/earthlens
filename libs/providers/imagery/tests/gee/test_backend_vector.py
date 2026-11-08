"""Tests for the GEE backend's vector/tabular output path (Task 3.12)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, ClassVar

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from earthlens.gee import backend as backend_module
from earthlens.gee.backend import GEE
from earthlens.gee.sampling.histogram import ZonalHistogram
from earthlens.gee.sampling.region import RegionSampler, ZonalStrategy
from earthlens.gee.sampling.timeseries import TimeSeriesSampler
from earthlens.gee.table_sink import TableSink
from earthlens.gee.work import RasterWorkUnit

_WHEN = dt.datetime(2024, 1, 1)
_POLYS = gpd.GeoDataFrame(
    geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])], crs=4326
)
_PTS = gpd.GeoDataFrame(geometry=[Point(0.5, 0.5)], crs=4326)


class _FakeSampler:
    """A duck-typed sampler recording its calls through the backend seams."""

    def __init__(self, output_kind: str = "vector", consumes_stack: bool = False):
        self.output_kind = output_kind
        self.consumes_stack = consumes_stack
        self.calls: list[tuple] = []

    def sample(self, image, geometry):
        self.calls.append(("sample", image, geometry))
        return "fake-fc"

    def backend_to_gdf(self, image, geometry, *, crs=4326):
        self.calls.append(("backend_to_gdf", image, geometry))
        return "fake-gdf"

    def _ensure_backend_compatible(self) -> None:
        return None


class _SpyTableSink(TableSink):
    """A real `TableSink` whose `export` records the sink instead of calling EE."""

    seen: ClassVar[list[TableSink]] = []

    def export(self, work_unit: Any, *, description: str) -> str:
        """Record the constructed sink and return a stand-in `TaskInfo`."""
        type(self).seen.append(self)
        return "fake-taskinfo"


class _StampImage:
    """A fake `ee.Image` recording `set(...)` calls and returning itself."""

    def __init__(self, name: str = ""):
        self.name = name
        self.props: dict = {}

    def set(self, key, value):
        self.props[key] = value
        return self


def _make_gee(tmp_path, **overrides) -> GEE:
    """Construct a GEE offline (its client is lazy) with sane defaults."""
    params = dict(
        start="2000-02-11",
        end="2000-02-12",
        variables={"USGS/SRTMGL1_003": ["elevation"]},
        lat_lim=[29.9, 30.0],
        lon_lim=[31.2, 31.3],
        path=str(tmp_path),
        scale=90.0,
    )
    params.update(overrides)
    return GEE(**params)


class TestEpochMillis:
    """The datetime -> ee `system:time_start` conversion."""

    def test_a_naive_datetime_is_read_as_utc(self):
        """A naive datetime (what `_composite` makes) is treated as UTC."""
        assert backend_module._epoch_millis(dt.datetime(1970, 1, 1)) == 0

    def test_a_tz_aware_datetime_is_respected(self):
        """A tz-aware datetime keeps its offset."""
        aware = dt.datetime(1970, 1, 1, 1, tzinfo=dt.timezone.utc)
        assert backend_module._epoch_millis(aware) == 3_600_000


class TestOutputKind:
    """A sampler flips the instance's OUTPUT_KIND; a raster request does not."""

    def test_raster_default(self, tmp_path):
        """With no sampler the instance keeps the class default `raster`."""
        assert _make_gee(tmp_path).OUTPUT_KIND == "raster"

    def test_a_region_sampler_is_vector(self, tmp_path):
        """A geometry-retaining sampler reports `vector`."""
        gee = _make_gee(tmp_path, sampler=RegionSampler(scale_m=30), geometry=_POLYS)
        assert gee.OUTPUT_KIND == "vector"

    def test_a_timeseries_sampler_is_tabular(self, tmp_path):
        """A time-series sampler collapses geometry, so it reports `tabular`."""
        gee = _make_gee(tmp_path, sampler=TimeSeriesSampler(scale_m=30), geometry=_PTS)
        assert gee.OUTPUT_KIND == "tabular"

    def test_a_sampler_without_geometry_is_refused(self, tmp_path):
        """A sampler needs something to sample over."""
        sampler = RegionSampler(scale_m=30)
        with pytest.raises(ValueError, match="needs a geometry"):
            _make_gee(tmp_path, sampler=sampler)

    def test_materialize_with_a_sampler_is_refused(self, tmp_path):
        """materialize=True is meaningless with a sampler, so it is refused up-front."""
        sampler = RegionSampler(scale_m=30)
        with pytest.raises(ValueError, match="materialize=True has no effect"):
            _make_gee(
                tmp_path,
                sampler=sampler,
                geometry=_POLYS,
                materialize=True,
            )

    def test_a_client_region_sampler_is_refused(self, tmp_path):
        """A CLIENT-strategy region sampler can't be driven by the backend."""
        sampler = RegionSampler(scale_m=30, strategy=ZonalStrategy.CLIENT)
        with pytest.raises(ValueError, match="strategy=CLIENT"):
            _make_gee(
                tmp_path,
                sampler=sampler,
                geometry=_POLYS,
            )


class TestSamplePayload:
    """_sample_payload routes the sampler's result by the sink kind."""

    def test_url_sink_returns_the_reshaped_table(self, tmp_path):
        """A synchronous sink returns the sampler's `backend_to_gdf` result."""
        sampler = _FakeSampler()
        gee = _make_gee(tmp_path, export_via="url", sampler=sampler, geometry=_POLYS)
        var_info = SimpleNamespace(id="USGS/SRTMGL1_003")
        result = gee._sample_payload(
            "img", _WHEN, _WHEN, _WHEN, var_info, ["elevation"]
        )
        assert result == "fake-gdf"
        assert sampler.calls == [("backend_to_gdf", "img", _POLYS)]

    def test_batch_sink_returns_a_taskinfo_via_tablesink(self, tmp_path, monkeypatch):
        """A batch sink queues a TableSink export (over sample()) and returns TaskInfo."""
        built = {}

        class _FakeTableSink:
            def __init__(self, **kwargs):
                built.update(kwargs)

            def export(self, work_unit, *, description):
                built["description"] = description
                built["features"] = work_unit.features
                return "fake-taskinfo"

        monkeypatch.setattr("earthlens.gee.table_sink.TableSink", _FakeTableSink)
        gee = _make_gee(
            tmp_path,
            export_via="drive",
            drive_folder="exports",
            wait_for_export=False,
            sampler=_FakeSampler(),
            geometry=_POLYS,
        )
        var_info = SimpleNamespace(id="USGS/SRTMGL1_003")
        result = gee._sample_payload(
            "img", _WHEN, _WHEN, _WHEN, var_info, ["elevation"]
        )
        assert result == "fake-taskinfo"
        assert built["destination"] == "drive"
        assert built["folder"] == "exports"
        assert built["file_format"] == "GeoJSON"  # vector keeps geometry
        assert built["features"] == "fake-fc"
        assert built["description"] == "USGS_SRTMGL1_003_elevation_20240101"

    def test_tabular_batch_uses_csv(self, tmp_path, monkeypatch):
        """A tabular (time-series) batch export writes CSV, not GeoJSON."""
        built = {}

        class _FakeTableSink:
            def __init__(self, **kwargs):
                built.update(kwargs)

            def export(self, work_unit, *, description):
                return "ti"

        monkeypatch.setattr("earthlens.gee.table_sink.TableSink", _FakeTableSink)
        gee = _make_gee(
            tmp_path,
            export_via="drive",
            drive_folder="exports",
            sampler=_FakeSampler(output_kind="tabular", consumes_stack=True),
            geometry=_PTS,
        )
        gee._sample_payload(
            ["imgA"], _WHEN, _WHEN, _WHEN, SimpleNamespace(id="X"), ["b"]
        )
        assert built["file_format"] == "CSV"

    def test_an_asset_batch_passes_no_file_format(self, tmp_path, monkeypatch):
        """An asset export sends `file_format=None`, which the real sink accepts."""
        _SpyTableSink.seen.clear()
        monkeypatch.setattr(
            "earthlens.gee.table_sink.TableSink", _SpyTableSink, raising=True
        )
        gee = _make_gee(
            tmp_path,
            export_via="asset",
            asset_id="projects/p/assets/out",
            sampler=_FakeSampler(),
            geometry=_POLYS,
        )
        result = gee._sample_payload(
            "img", _WHEN, _WHEN, _WHEN, SimpleNamespace(id="X"), ["b"]
        )
        assert result == "fake-taskinfo", f"unexpected payload result: {result!r}"
        sink = _SpyTableSink.seen[-1]
        assert sink.destination == "asset", f"wrong destination: {sink.destination!r}"
        assert sink.asset_id == "projects/p/assets/out", f"wrong id: {sink.asset_id!r}"
        assert sink.file_format is None, f"expected no format, got {sink.file_format!r}"
        assert sink.effective_file_format is None, (
            f"an asset writes a table, so no format applies: "
            f"{sink.effective_file_format!r}"
        )

    def test_zonal_histogram_through_backend_widens(self, tmp_path, monkeypatch):
        """The sync path applies the sampler's `_reshape`, so a histogram widens."""
        hist = ZonalHistogram(scale_m=30, class_band="landcover")
        gee = _make_gee(tmp_path, export_via="url", sampler=hist, geometry=_POLYS)
        monkeypatch.setattr(type(hist), "sample", lambda self, image, geometry: "fc")
        groups_frame = gpd.GeoDataFrame(
            {
                "groups": [
                    [
                        {"class": 1, "count": 3, "sum": 3.0},
                        {"class": 2, "count": 1, "sum": 1.0},
                    ]
                ]
            },
            geometry=_POLYS.geometry,
            crs=4326,
        )

        class _FakeDownloader:
            def to_gdf(self, fc, crs=4326):
                return groups_frame

        monkeypatch.setattr(
            "earthlens.gee.vector.download.FeatureDownloader", _FakeDownloader
        )
        out = gee._sample_payload(
            "img", _WHEN, _WHEN, _WHEN, SimpleNamespace(id="X"), ["landcover"]
        )
        assert "groups" not in out.columns
        assert {"1", "2"} <= {str(column) for column in out.columns}


class TestVectorDownload:
    """download() returns the vector list directly and skips materialise."""

    def test_download_returns_the_vector_list(self, tmp_path, monkeypatch):
        """A sampler run returns its GeoDataFrames without re-reading paths."""
        gee = _make_gee(
            tmp_path, export_via="url", sampler=_FakeSampler(), geometry=_POLYS
        )
        monkeypatch.setattr(gee, "_open_client", lambda: object())
        monkeypatch.setattr(
            gee, "_download_dataset", lambda asset, bands, progress: ["gdf-a"]
        )
        assert gee.download(progress_bar=False) == ["gdf-a"]

    def test_download_materialize_override_with_a_sampler_is_refused(self, tmp_path):
        """The download(materialize=True) override is refused too, like the ctor."""
        gee = _make_gee(
            tmp_path, export_via="url", sampler=_FakeSampler(), geometry=_POLYS
        )
        with pytest.raises(ValueError, match="materialize=True has no effect"):
            gee.download(progress_bar=False, materialize=True)

    def test_download_dataset_samples_each_bucket(self, tmp_path, monkeypatch):
        """With a per-bucket sampler, each composited bucket is sampled in turn."""
        gee = _make_gee(
            tmp_path, export_via="url", sampler=_FakeSampler(), geometry=_POLYS
        )
        var_info = SimpleNamespace(id="USGS/SRTMGL1_003", get_band=lambda band: None)
        unit = RasterWorkUnit("img", _WHEN, _WHEN, _WHEN)
        monkeypatch.setattr(
            gee, "_catalog", SimpleNamespace(get_dataset=lambda asset: var_info)
        )
        monkeypatch.setattr(gee, "_clamp_window_to_extent", lambda vi: (_WHEN, _WHEN))
        monkeypatch.setattr(gee, "_build_collection", lambda *a: "coll")
        monkeypatch.setattr(gee, "_composite", lambda *a: [unit])
        monkeypatch.setattr(gee, "_sample_payload", lambda *a: "sampled")
        result = gee._download_dataset("USGS/SRTMGL1_003", ["elevation"], False)
        assert result == ["sampled"]

    def test_a_stack_sampler_gets_the_whole_bucket_list_once(
        self, tmp_path, monkeypatch
    ):
        """A `consumes_stack` sampler is handed every bucket image in one call."""
        sampler = TimeSeriesSampler(scale_m=30)
        gee = _make_gee(tmp_path, export_via="url", sampler=sampler, geometry=_PTS)
        var_info = SimpleNamespace(id="USGS/SRTMGL1_003", get_band=lambda band: None)
        units = [
            RasterWorkUnit(_StampImage("A"), _WHEN, _WHEN, _WHEN),
            RasterWorkUnit(_StampImage("B"), _WHEN, _WHEN, _WHEN),
        ]
        seen = {}
        monkeypatch.setattr(
            TimeSeriesSampler,
            "backend_to_gdf",
            lambda self, image, geometry, crs=4326: (
                seen.update(image=image),
                "table",
            )[1],
        )
        monkeypatch.setattr(
            gee, "_catalog", SimpleNamespace(get_dataset=lambda asset: var_info)
        )
        monkeypatch.setattr(gee, "_clamp_window_to_extent", lambda vi: (_WHEN, _WHEN))
        monkeypatch.setattr(gee, "_build_collection", lambda *a: "coll")
        monkeypatch.setattr(gee, "_composite", lambda *a: units)
        result = gee._download_dataset("USGS/SRTMGL1_003", ["elevation"], False)
        # One result for the whole stack, and the sampler saw the list of images.
        assert result == ["table"]
        assert [image.name for image in seen["image"]] == ["A", "B"]

    def test_stack_images_are_stamped_with_their_bucket_time(
        self, tmp_path, monkeypatch
    ):
        """Each composited bucket is stamped with its `system:time_start`.

        The compositing reducer strips per-scene time, so without this the
        time-series would read a null timestamp for every bucket.
        """
        gee = _make_gee(
            tmp_path,
            export_via="url",
            sampler=TimeSeriesSampler(scale_m=30),
            geometry=_PTS,
        )
        when_a, when_b = dt.datetime(2024, 1, 1), dt.datetime(2024, 2, 1)
        img_a, img_b = _StampImage("A"), _StampImage("B")
        units = [
            RasterWorkUnit(img_a, when_a, when_a, when_a),
            RasterWorkUnit(img_b, when_b, when_b, when_b),
        ]
        var_info = SimpleNamespace(id="X", get_band=lambda band: None)
        monkeypatch.setattr(
            gee, "_catalog", SimpleNamespace(get_dataset=lambda asset: var_info)
        )
        monkeypatch.setattr(gee, "_clamp_window_to_extent", lambda vi: (when_a, when_b))
        monkeypatch.setattr(gee, "_build_collection", lambda *a: "coll")
        monkeypatch.setattr(gee, "_composite", lambda *a: units)
        monkeypatch.setattr(
            TimeSeriesSampler,
            "backend_to_gdf",
            lambda self, image, geometry, crs=4326: "table",
        )
        gee._download_dataset("X", ["b"], False)
        expected_a = int(when_a.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        expected_b = int(when_b.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        assert img_a.props["system:time_start"] == expected_a
        assert img_b.props["system:time_start"] == expected_b

    def test_a_stack_sampler_with_no_buckets_returns_empty(self, tmp_path, monkeypatch):
        """A consumes_stack sampler over an empty bucket list yields no results."""
        gee = _make_gee(
            tmp_path,
            export_via="url",
            sampler=TimeSeriesSampler(scale_m=30),
            geometry=_PTS,
        )
        var_info = SimpleNamespace(id="USGS/SRTMGL1_003", get_band=lambda band: None)
        monkeypatch.setattr(
            gee, "_catalog", SimpleNamespace(get_dataset=lambda asset: var_info)
        )
        monkeypatch.setattr(gee, "_clamp_window_to_extent", lambda vi: (_WHEN, _WHEN))
        monkeypatch.setattr(gee, "_build_collection", lambda *a: "coll")
        monkeypatch.setattr(gee, "_composite", lambda *a: [])
        assert gee._download_dataset("USGS/SRTMGL1_003", ["elevation"], False) == []
