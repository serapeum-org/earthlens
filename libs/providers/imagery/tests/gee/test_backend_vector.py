"""Tests for the GEE backend's vector/tabular output path (Task 3.12)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from earthlens.gee.backend import GEE
from earthlens.gee.sampling.region import RegionSampler
from earthlens.gee.sampling.timeseries import TimeSeriesSampler
from earthlens.gee.work import RasterWorkUnit

_WHEN = dt.datetime(2024, 1, 1)
_POLYS = gpd.GeoDataFrame(
    geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])], crs=4326
)


class _FakeSampler:
    """A duck-typed sampler recording its sample() calls."""

    def __init__(self, output_kind: str = "vector") -> None:
        self.output_kind = output_kind
        self.calls: list[tuple] = []

    def sample(self, image, geometry):
        self.calls.append((image, geometry))
        return "fake-fc"


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
        gee = _make_gee(
            tmp_path, sampler=TimeSeriesSampler(scale_m=30), geometry=_POLYS
        )
        assert gee.OUTPUT_KIND == "tabular"

    def test_a_sampler_without_geometry_is_refused(self, tmp_path):
        """A sampler needs something to sample over."""
        with pytest.raises(ValueError, match="needs a geometry"):
            _make_gee(tmp_path, sampler=RegionSampler(scale_m=30))


class TestSampleBucket:
    """_sample_bucket routes the sampler's collection by the sink kind."""

    def test_url_sink_returns_a_geodataframe(self, tmp_path, monkeypatch):
        """A synchronous sink pulls the collection into a GeoDataFrame."""
        captured = {}

        class _FakeDownloader:
            def to_gdf(self, fc):
                captured["fc"] = fc
                return "fake-gdf"

        monkeypatch.setattr(
            "earthlens.gee.vector.download.FeatureDownloader", _FakeDownloader
        )
        gee = _make_gee(
            tmp_path, export_via="url", sampler=_FakeSampler(), geometry=_POLYS
        )
        var_info = SimpleNamespace(id="USGS/SRTMGL1_003")
        unit = RasterWorkUnit("img", _WHEN, _WHEN, _WHEN)
        result = gee._sample_bucket(unit, var_info, ["elevation"])
        assert result == "fake-gdf"
        assert captured["fc"] == "fake-fc"

    def test_batch_sink_returns_a_taskinfo_via_tablesink(self, tmp_path, monkeypatch):
        """A batch sink queues a TableSink export and returns its TaskInfo."""
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
        unit = RasterWorkUnit("img", _WHEN, _WHEN, _WHEN)
        result = gee._sample_bucket(unit, var_info, ["elevation"])
        assert result == "fake-taskinfo"
        assert built["destination"] == "drive" and built["folder"] == "exports"
        assert built["file_format"] == "GeoJSON"  # vector keeps geometry
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
            sampler=_FakeSampler(output_kind="tabular"),
            geometry=_POLYS,
        )
        gee._sample_bucket(
            RasterWorkUnit("img", _WHEN, _WHEN, _WHEN),
            SimpleNamespace(id="X"),
            ["b"],
        )
        assert built["file_format"] == "CSV"


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

    def test_download_dataset_samples_each_bucket(self, tmp_path, monkeypatch):
        """With a sampler, each composited bucket is sampled, not exported."""
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
        monkeypatch.setattr(gee, "_sample_bucket", lambda u, vi, b: "sampled")
        result = gee._download_dataset("USGS/SRTMGL1_003", ["elevation"], False)
        assert result == ["sampled"]
