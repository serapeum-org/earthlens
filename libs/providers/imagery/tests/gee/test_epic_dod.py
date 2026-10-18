"""The Epic 1 definition-of-done items that had no test of their own.

Each class here pins one promise the epic made and the first pass did not check:
the two reducer vocabularies and the `ee.Reducer` bridge (1.2), the sink owning
its own write and its own prerequisite (1.4), the datacube a multi-bucket
download can hand back (1.8), and the grid answering where it is in lat/lon
(1.12).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from earthlens.gee import backend as backend_module
from earthlens.gee.grid import NATIVE_CRS, OutputGrid, bbox_to_latlon
from earthlens.gee.reducers import (
    REDUCERS,
    Count,
    First,
    Mean,
    Mosaic,
    Percentile,
    Reducer,
    StdDev,
    Variance,
    collection_reducers,
    resolve_reducer,
    sampling_reducers,
)
from earthlens.gee.sinks import (
    SINKS,
    AssetSink,
    DriveSink,
    GcsSink,
    Sink,
    UrlSink,
    resolve_sink,
)
from earthlens.gee.work import Artifact, DownloadResult

from .test_backend import (  # noqa: F401 - fixtures
    _FakePyramidsDataset,
    fake_ee,
    fake_reader,
    make_gee,
)

ASSET = "USGS/SRTMGL1_003"
START = dt.datetime(2000, 2, 11)
END = dt.datetime(2000, 2, 13)


class _SinkWithoutWrite(Sink):
    """A sink that forgot its transport, so the ABC must refuse to build it."""

    name = "no-write"


class _ProbeSink(Sink):
    """A throwaway destination, to prove registration is all a sink needs."""

    name = "probe"

    def write(self, backend, image, **kwargs):
        """Stand in for a transport; what is under test is reaching it."""
        return "probed"


@pytest.fixture
def probe_sink():
    """Register `_ProbeSink` for one test, then take it back out."""
    SINKS[_ProbeSink.name] = _ProbeSink
    yield _ProbeSink
    del SINKS[_ProbeSink.name]


def _collect_warnings(monkeypatch):
    """Collect the backend's warnings instead of emitting them."""
    collected: list[str] = []
    monkeypatch.setattr(
        backend_module.logger,
        "warning",
        lambda msg, *args, **kwargs: collected.append(msg),
    )
    return collected


def _record_merge(sink: list, sources, target) -> None:
    """Stand in for `merge_rasters`, recording the call and writing the mosaic."""
    sink.append({"sources": list(sources), "target": str(target)})
    Path(target).write_bytes(b"merged")


def _gee(make_gee, **overrides):
    """A GEE over one small SRTM read."""
    params = {"variables": {ASSET: ["elevation"]}, "scale": 90.0}
    params.update(overrides)
    return make_gee(**params)


class TestTheTwoReducerVocabularies:
    """One registry, two capabilities — because the two sets are not the same."""

    def test_the_collection_set_is_the_band_preserving_seven(self):
        """What `GEE(reducer=...)` accepts, unchanged by widening the registry."""
        assert sorted(collection_reducers()) == [
            "max",
            "mean",
            "median",
            "min",
            "mode",
            "mosaic",
            "sum",
        ]

    def test_the_sampling_set_is_what_ee_reducer_can_build(self):
        """What a region reduction accepts — the pre-epic whitelist, exactly."""
        assert sorted(sampling_reducers()) == [
            "count",
            "first",
            "max",
            "mean",
            "median",
            "min",
            "mode",
            "stdDev",
            "sum",
            "variance",
        ]

    def test_mosaic_is_the_difference(self):
        """It is a collection method with no `ee.Reducer` factory at all."""
        assert "mosaic" in collection_reducers()
        assert "mosaic" not in sampling_reducers()

    def test_the_region_reducers_are_the_other_difference(self):
        """They are registry members so there is one vocabulary, not two lists."""
        for name in ("first", "stdDev", "variance", "count"):
            assert name in REDUCERS, name
            assert name in sampling_reducers(), name
            assert name not in collection_reducers(), name

    def test_both_sets_are_derived_from_the_registry(self):
        """A reducer added to the registry cannot be forgotten by either set."""
        assert collection_reducers() | sampling_reducers() <= set(REDUCERS)


class TestAsEeReducer:
    """The bridge to the region path, which the epic asked for and lacked."""

    @pytest.mark.parametrize(
        ("spec", "factory"),
        [
            ("mean", "mean"),
            ("median", "median"),
            ("min", "min"),
            ("max", "max"),
            ("mode", "mode"),
            ("sum", "sum"),
            ("first", "first"),
            ("stdDev", "stdDev"),
            ("variance", "variance"),
            ("count", "count"),
        ],
    )
    def test_each_reducer_names_its_factory(self, spec, factory):
        """Declared once per class, so the two paths cannot disagree."""
        assert resolve_reducer(spec).ee_factory == factory

    def test_mosaic_refuses_rather_than_returning_none(self):
        """A `None` here would only fail at the call site instead."""
        mosaic = Mosaic()
        with pytest.raises(ValueError, match="no ee.Reducer equivalent"):
            mosaic.as_ee_reducer()

    def test_the_refusal_names_the_region_vocabulary(self):
        """So the message points at something that would work."""
        mosaic = Mosaic()
        with pytest.raises(ValueError, match="stdDev"):
            mosaic.as_ee_reducer()

    def test_a_percentile_builds_a_parameterised_reducer(self, monkeypatch):
        """The reducer no bare whitelist name could express."""
        import ee

        calls: list[list[float]] = []
        monkeypatch.setattr(
            ee,
            "Reducer",
            SimpleNamespace(percentile=lambda qs: calls.append(qs) or "p"),
        )
        assert Percentile(q=90).as_ee_reducer() == "p"
        assert calls == [[90.0]]

    def test_every_registry_member_is_one_capability_or_the_other(self):
        """A reducer that is neither would be unreachable from both paths."""
        for name, cls in REDUCERS.items():
            assert cls.collection_capable or cls.ee_factory is not None, name


class TestTheRegionReducersStayOutOfTheCollectionPath:
    """`a collection-only call on a sampling-only reducer raises` — the DoD item."""

    @pytest.mark.parametrize("cls", [First, StdDev, Variance, Count])
    def test_calling_one_on_a_collection_raises(self, cls):
        """The backstop for reaching the object directly."""
        reducer = cls()
        collection = SimpleNamespace()
        with pytest.raises(ValueError, match="not images in a collection"):
            reducer(collection)

    @pytest.mark.parametrize("name", ["first", "stdDev", "variance", "count"])
    def test_the_constructor_refuses_one(self, make_gee, fake_reader, name):
        """Refused where every other config error is: at construction."""
        with pytest.raises(ValueError, match="reduces pixels in a region"):
            _gee(make_gee, reducer=name)

    def test_the_refusal_names_the_alternatives(self, make_gee, fake_reader):
        """Including the percentile spelling, which is not a registry name."""
        with pytest.raises(ValueError, match="percentile:<q>"):
            _gee(make_gee, reducer="stdDev")

    def test_a_collection_reducer_is_still_accepted(self, make_gee, fake_reader):
        """The check must not narrow what used to work."""
        assert _gee(make_gee, reducer="median").reducer == "median"

    def test_a_reducer_object_is_checked_too(self, make_gee, fake_reader):
        """`reducer=` takes objects, so the capability check has to see them."""
        reducer = StdDev()
        with pytest.raises(ValueError, match="reduces pixels in a region"):
            _gee(make_gee, reducer=reducer)


class TestSamplingReachesTheRegistry:
    """`percentile now reachable from both paths`, and `mosaic` refused cleanly."""

    def test_mosaic_is_refused_with_a_message(self):
        """A sampler refuses `mosaic` (no `ee.Reducer`) at construction."""
        from pydantic import ValidationError

        from earthlens.gee.sampling.point import PointSampler

        with pytest.raises(ValidationError, match="mosaic"):
            PointSampler(scale_m=30, reducer="mosaic")

    def test_an_unknown_name_is_still_refused(self):
        """The pre-epic message, unchanged, now from the registry."""
        with pytest.raises(ValueError, match="unsupported reducer 'p95'"):
            resolve_reducer("p95")

    def test_a_percentile_spec_resolves(self, monkeypatch):
        """Reachable through the registry, bridged to `ee.Reducer.percentile`."""
        import ee

        monkeypatch.setattr(
            ee, "Reducer", SimpleNamespace(percentile=lambda qs: ("p", tuple(qs)))
        )
        assert resolve_reducer("percentile:90").as_ee_reducer() == ("p", (90.0,))

    def test_a_percentile_without_its_argument_says_so(self):
        """The registry's message, which names the spelling."""
        with pytest.raises(ValueError, match="percentile:<q>"):
            resolve_reducer("percentile")

    def test_the_whitelist_excludes_mosaic(self):
        """The regression in one line: `mosaic` is not a sampling reducer."""
        assert "mosaic" not in sampling_reducers()


class TestTheSinkOwnsItsWrite:
    """Task 1.4's `_api` delegates to `sink.write` / `validate_config`."""

    def test_every_sink_implements_write(self):
        """It is abstract on the ABC, so a new sink cannot forget it."""
        for sink in (UrlSink(), DriveSink(), GcsSink(), AssetSink()):
            assert callable(sink.write)

    def test_a_sink_without_a_write_cannot_be_built(self):
        """What makes "cannot forget it" true rather than a convention."""
        with pytest.raises(TypeError, match="abstract"):
            _SinkWithoutWrite()

    def test_the_url_sink_writes_a_path(self, make_gee, fake_reader):
        """The synchronous sink fetches now and returns the file."""
        gee = _gee(make_gee, engine="ee")
        var_info = gee.catalog.get_dataset(ASSET)
        collection = gee._build_collection(var_info, ["elevation"], START, END)
        ((_when, image, _bs, _be),) = gee._composite(collection, var_info, START, END)
        written = gee._sink.write(
            gee,
            image,
            var_info=var_info,
            scale=90.0,
            bands=["elevation"],
            region=gee._ee_region(),
            prefix="direct",
        )
        assert isinstance(written, Path)
        assert written.is_file()

    def test_the_url_sink_routes_the_pixel_engine(self, make_gee, fake_reader):
        """`engine=` only ever meant anything to this sink, so it decides."""
        gee = _gee(make_gee, engine="pixels")
        gee.download(progress_bar=False)
        assert len(gee.client.compute_pixels_calls) == 1

    def test_a_batch_sink_returns_its_destination(self, make_gee, fake_reader):
        """And queues the export rather than pulling pixels."""
        gee = _gee(make_gee, export_via="drive", drive_folder="out")
        (destination,) = gee.download(progress_bar=False)
        assert destination.startswith("drive://")
        assert gee.client.export_image.calls

    @pytest.mark.parametrize(
        ("name", "field"),
        [("drive", "drive_folder"), ("gcs", "gcs_bucket"), ("asset", "asset_id")],
    )
    def test_validate_config_refuses_a_missing_prerequisite(self, name, field):
        """The sink refuses itself, so the constructor names no destination."""
        sink = resolve_sink(name)
        with pytest.raises(ValueError, match=f"requires {field}"):
            sink.validate_config({})

    def test_validate_config_accepts_a_supplied_prerequisite(self):
        """And says nothing when the destination is there."""
        assert DriveSink().validate_config({"drive_folder": "out"}) is None

    @pytest.mark.parametrize("empty", ["", None])
    def test_an_empty_prerequisite_is_refused_like_a_missing_one(self, empty):
        """A key that is present but blank addresses nothing either."""
        sink = DriveSink()
        with pytest.raises(ValueError, match="requires drive_folder"):
            sink.validate_config({"drive_folder": empty})

    def test_the_url_sink_needs_nothing(self):
        """`required_config` is `None`, so there is nothing to refuse."""
        assert UrlSink().validate_config({}) is None

    def test_the_asset_hint_travels_with_the_sink(self):
        """The hint used to live in a dict in `backend.py`, keyed by field name."""
        sink = AssetSink()
        with pytest.raises(ValueError, match="parent folder asset"):
            sink.validate_config({})

    def test_the_constructor_still_refuses_through_the_sink(self, make_gee):
        """Same error text as before, now raised by the sink that needs it."""
        with pytest.raises(ValueError, match="requires drive_folder"):
            _gee(make_gee, export_via="drive")


class TestTheResultStacksIntoADatacube:
    """Task 1.8's `.collection()`."""

    def test_it_builds_from_the_written_files(self, monkeypatch):
        """pyramids has no constructor for in-memory datasets; the files are it."""
        from pyramids import dataset as pyramids_dataset

        calls: list[tuple] = []
        monkeypatch.setattr(
            pyramids_dataset.DatasetCollection,
            "from_files",
            classmethod(lambda cls, files, **kw: calls.append((files, kw)) or "cube"),
        )
        result = DownloadResult(
            artifacts=(
                Artifact(path=Path("a_elevation_20000211.tif")),
                Artifact(path=Path("a_elevation_20000212.tif")),
            )
        )
        assert result.collection() == "cube"
        (files, kwargs) = calls[0]
        assert [Path(name).name for name in files] == [
            "a_elevation_20000211.tif",
            "a_elevation_20000212.tif",
        ]
        assert kwargs["date_regex"] == r"\d{8}"
        assert kwargs["date_format"] == "%Y%m%d"

    def test_the_date_pattern_can_be_overridden(self, monkeypatch):
        """Files named by something else are still stackable."""
        from pyramids import dataset as pyramids_dataset

        calls: list[dict] = []
        monkeypatch.setattr(
            pyramids_dataset.DatasetCollection,
            "from_files",
            classmethod(lambda cls, files, **kw: calls.append(kw) or "cube"),
        )
        DownloadResult(artifacts=(Artifact(path=Path("a.tif")),)).collection(
            date_regex=r"\d{4}-\d{2}-\d{2}", date_format="%Y-%m-%d"
        )
        assert calls[0]["date_regex"] == r"\d{4}-\d{2}-\d{2}"

    def test_only_the_written_files_are_stacked(self, monkeypatch):
        """A mixed result can still stack: a destination is not a file."""
        from pyramids import dataset as pyramids_dataset

        calls: list[list[str]] = []
        monkeypatch.setattr(
            pyramids_dataset.DatasetCollection,
            "from_files",
            classmethod(lambda cls, files, **kw: calls.append(files) or "cube"),
        )
        result = DownloadResult(
            artifacts=(
                Artifact(path=Path("a_elevation_20000211.tif")),
                Artifact(destination="drive://folder/a_elevation_20000212"),
            )
        )
        assert result.collection() == "cube"
        assert [Path(name).name for name in calls[0]] == ["a_elevation_20000211.tif"]

    def test_a_batch_result_says_why_it_cannot(self):
        """Destinations are not local files, so there is nothing to stack."""
        result = DownloadResult(artifacts=(Artifact(destination="drive://f/p"),))
        with pytest.raises(ValueError, match="no written files"):
            result.collection()


class TestTheGridKnowsWhereItIs:
    """Task 1.12's `latlon_bbox()`."""

    CAIRO = (31.2, 29.9, 31.3, 30.0)

    def test_a_geographic_grid_answers_with_its_own_bbox(self):
        """No transform, so no round-trip error either."""
        grid = OutputGrid.for_metre_grid(self.CAIRO, crs=NATIVE_CRS, scale=90.0)
        assert grid.latlon_bbox() == self.CAIRO

    def test_a_projected_grid_is_brought_back(self):
        """The envelope in lat/lon, densified rather than corner-transformed."""
        grid = OutputGrid.for_metre_grid(
            (330_000.0, 3_310_000.0, 340_000.0, 3_320_000.0),
            crs="EPSG:32636",
            scale=90.0,
        )
        min_lon, min_lat, max_lon, max_lat = grid.latlon_bbox()
        assert 31.0 < min_lon < max_lon < 31.5
        assert 29.8 < min_lat < max_lat < 30.1

    def test_the_helper_round_trips_a_window(self):
        """`bbox_to_latlon` is the inverse of what the request path uses."""
        from earthlens.gee.grid import bbox_to_crs

        projected = bbox_to_crs(self.CAIRO, "EPSG:32636")
        for got, want in zip(bbox_to_latlon(projected, "EPSG:32636"), self.CAIRO):
            assert abs(got - want) < 0.01

    def test_a_latlon_window_is_returned_unchanged(self):
        """The common case pays nothing: no pyproj, no bow."""
        assert bbox_to_latlon(self.CAIRO, NATIVE_CRS) == self.CAIRO

    def test_the_discovery_aoi_describes_the_read_window(self, make_gee, fake_reader):
        """The invariant `_eedai_latlon_aoi` asserts in prose and nothing checked.

        Scene discovery queries in lat/lon while the read windows in the output
        CRS, so the two have to describe the same ground. The backend derives
        the discovery AOI from the original geometry rather than from the grid,
        because the inverse transform of a projected window loses a little
        ground at the edges (pyramids-eo#97) - this pins how little.
        """
        gee = _gee(make_gee, crs="EPSG:32636")
        bbox, _cutline = gee._eedai_window()
        grid = OutputGrid.for_metre_grid(bbox, crs=gee.crs, scale=90.0)
        for got, want in zip(grid.latlon_bbox(), gee._eedai_latlon_aoi()):
            assert abs(got - want) < 0.01


class TestTheReducerAbcStaysOneThing:
    """A reducer is still a `collection -> image` callable, whatever it declares."""

    def test_the_convenience_reducers_are_callable(self):
        """Unchanged: the seven dispatch to the collection method by name."""
        assert Mean()(SimpleNamespace(mean=lambda: "reduced")) == "reduced"

    def test_every_registry_member_is_a_reducer(self):
        """Including the region-only ones, which is what makes it one vocabulary."""
        for cls in REDUCERS.values():
            assert issubclass(cls, Reducer)

    def test_a_region_reducer_is_not_callable_as_a_composite(self):
        """It is a `Reducer`, but calling it is the error, not the silence."""
        reducer = StdDev()
        collection = SimpleNamespace(stdDev=lambda: "reduced")
        with pytest.raises(ValueError):
            reducer(collection)


class TestTheIgnoredArgumentNoticesAreHonest:
    """A notice about nothing is as bad as silence about something.

    Both arguments are honoured at different points - `nodata` on the
    `computePixels` request body, which is only built for the synchronous sink,
    and `high_volume` on the session's host, which is chosen whatever the sink -
    so one gate for both let `engine="pixels", export_via="drive"` accept a
    `nodata` that nothing would read.
    """

    def test_nodata_warns_on_the_sink_that_cannot_carry_it(
        self, make_gee, fake_reader, monkeypatch
    ):
        """An asset export has no file format, so no fill value can be declared."""
        collected = _collect_warnings(monkeypatch)
        _gee(
            make_gee,
            engine="pixels",
            export_via="asset",
            asset_id="projects/p/assets/f",
            nodata=-9999,
        )
        assert [message for message in collected if "nodata" in message]

    def test_the_notice_names_where_it_would_be_honoured(
        self, make_gee, fake_reader, monkeypatch
    ):
        """So a caller can see which engine or sink to pick instead."""
        collected = _collect_warnings(monkeypatch)
        _gee(
            make_gee,
            engine="ee",
            export_via="asset",
            asset_id="projects/p/assets/f",
            nodata=-9999,
        )
        (message,) = [m for m in collected if "nodata" in m]
        assert "engine='pixels'" in message
        assert "drive" in message

    def test_nodata_is_silent_on_a_file_based_batch_export(
        self, make_gee, fake_reader, monkeypatch
    ):
        """Earth Engine's own GeoTIFF option carries it there."""
        collected = _collect_warnings(monkeypatch)
        _gee(
            make_gee,
            engine="ee",
            export_via="drive",
            drive_folder="out",
            nodata=-9999,
        )
        assert not [message for message in collected if "nodata" in message]

    def test_nodata_is_silent_where_it_is_honoured(
        self, make_gee, fake_reader, monkeypatch
    ):
        """The pixel engine on the synchronous sink does read it."""
        collected = _collect_warnings(monkeypatch)
        _gee(make_gee, engine="pixels", nodata=-9999)
        assert not [message for message in collected if "nodata" in message]

    def test_high_volume_is_silent_on_a_batch_sink(
        self, make_gee, fake_reader, monkeypatch
    ):
        """It picks the host the session opens on, which happens either way."""
        collected = _collect_warnings(monkeypatch)
        _gee(
            make_gee,
            engine="pixels",
            export_via="drive",
            drive_folder="out",
            high_volume=True,
        )
        assert not [message for message in collected if "high_volume" in message]


class TestTheCogNoticeIsHonestToo:
    """`cog=` reaches the paths that write the raster themselves, and no other."""

    def test_it_is_silent_on_the_pixel_path(self, make_gee, fake_reader, monkeypatch):
        """That path writes its own file, so the flag is honoured there."""
        collected = _collect_warnings(monkeypatch)
        gee = _gee(make_gee, engine="pixels", cog=True)
        gee.download(progress_bar=False)
        assert not [message for message in collected if "cog=True" in message]

    def test_it_still_warns_on_get_download_url(
        self, make_gee, fake_reader, monkeypatch
    ):
        """Where the flag genuinely does nothing."""
        collected = _collect_warnings(monkeypatch)
        gee = _gee(make_gee, engine="ee", cog=True)
        gee.download(progress_bar=False)
        assert [message for message in collected if "cog=True" in message]

    def test_it_still_warns_for_a_batch_export(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A queued export writes the file Earth Engine's way, not ours."""
        collected = _collect_warnings(monkeypatch)
        gee = _gee(
            make_gee, engine="pixels", export_via="drive", drive_folder="out", cog=True
        )
        gee.download(progress_bar=False)
        assert [message for message in collected if "cog=True" in message]


class TestATiledPixelFetchKeepsItsCog:
    """`cog=True` honoured per tile and then lost in the stitch is still lost."""

    WIDE = {"lat_lim": [9.9, 10.0], "lon_lim": [-180.0, 180.0], "scale": 1000.0}

    def test_the_mosaic_is_rewritten_as_a_cog(self, make_gee, fake_reader, monkeypatch):
        """`merge_rasters` writes a plain GeoTIFF, so the mosaic is converted."""
        merged: list[dict] = []
        monkeypatch.setattr(
            backend_module,
            "merge_rasters",
            lambda sources, target, **kwargs: _record_merge(merged, sources, target),
        )
        _FakePyramidsDataset.reset()
        gee = _gee(make_gee, engine="pixels", auto_split=True, cog=True, **self.WIDE)
        (out,) = gee.download(progress_bar=False)
        assert out.is_file()
        assert any(
            path.endswith(".merged.tif")
            for path in _FakePyramidsDataset.read_file_calls
        ), "the merged raster was never re-read, so no COG was written"

    def test_the_staged_mosaic_is_not_left_behind(
        self, make_gee, fake_reader, monkeypatch
    ):
        """The staging file is the COG's input, not an output."""
        merged: list[dict] = []
        monkeypatch.setattr(
            backend_module,
            "merge_rasters",
            lambda sources, target, **kwargs: _record_merge(merged, sources, target),
        )
        gee = _gee(make_gee, engine="pixels", auto_split=True, cog=True, **self.WIDE)
        (out,) = gee.download(progress_bar=False)
        assert not (out.parent / f"{out.stem}.merged.tif").exists()

    def test_without_the_flag_the_mosaic_is_written_once(
        self, make_gee, fake_reader, monkeypatch
    ):
        """No staging, no re-read: the url path's behaviour is unchanged."""
        merged: list[dict] = []
        monkeypatch.setattr(
            backend_module,
            "merge_rasters",
            lambda sources, target, **kwargs: _record_merge(merged, sources, target),
        )
        _FakePyramidsDataset.reset()
        gee = _gee(make_gee, engine="pixels", auto_split=True, **self.WIDE)
        gee.download(progress_bar=False)
        assert not any(
            path.endswith(".merged.tif")
            for path in _FakePyramidsDataset.read_file_calls
        )


class TestARegisteredSinkReachesTheBackend:
    """Task 1.4's acceptance criterion, end to end rather than at the registry.

    `resolve_sink` accepted a newly registered sink, but `ExportConfig` declared
    `export_via` as a closed `Literal` of the four shipped names and is
    validated at runtime - so the seam stopped at the config, and "adding a
    destination is a subclass plus a registry entry" was false for anything
    that reached one.
    """

    def test_the_config_accepts_it(self, probe_sink):
        """The vocabulary is the registry's, not a restated literal."""
        from earthlens.gee.configs import ExportConfig

        assert ExportConfig(export_via="probe").export_via == "probe"

    def test_an_unregistered_name_is_still_refused(self):
        """Widening to `str` must not mean accepting anything."""
        from pydantic import ValidationError

        from earthlens.gee.configs import ExportConfig

        with pytest.raises(ValidationError, match="export_via must be one of"):
            ExportConfig(export_via="nope")

    def test_the_backend_writes_through_it(self, make_gee, fake_reader, probe_sink):
        """One subclass, one registry entry, no edit to the export path."""
        gee = _gee(make_gee, export_via="probe")
        (written,) = gee.download(progress_bar=False)
        assert written == "probed"

    def test_the_prerequisite_message_has_one_source(self, probe_sink):
        """The asset hint lived in two places and had already drifted a space."""
        from pydantic import ValidationError

        from earthlens.gee.configs import ExportConfig
        from earthlens.gee.sinks import AssetSink

        sink = AssetSink()
        with pytest.raises(ValueError) as from_sink:
            sink.validate_config({})
        with pytest.raises(ValidationError) as from_config:
            ExportConfig(export_via="asset")
        assert str(from_sink.value) in str(from_config.value)
