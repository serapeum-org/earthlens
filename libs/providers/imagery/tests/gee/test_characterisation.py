"""Characterisation lock on the seams Epic 1 relocates.

These tests assert the *observable* outputs of the download paths — the filename
stem, which `ee.batch.Export.image.to*` each `export_via` reaches and with which
kwargs, the destination strings, the engine-routing verdicts and the reducer
dispatch — rather than the methods that currently produce them. That is the whole
point: Tasks 1.1-1.13 move this behaviour into `Reducer` / `Engine` / `Sink` /
`WorkUnit` objects, and this file must keep passing unchanged across every one of
those commits. A failure here means the refactor changed behaviour, not that the
test needs updating.
"""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.gee import backend as backend_module
from earthlens.gee._helpers import reduce_collection

# Reuse the established fakes rather than building a second set that could drift.
from .test_backend import (  # noqa: F401 - imported to register as fixtures
    fake_ee,
    fake_reader,
    make_gee,
)

pytestmark = pytest.mark.gee

#: The seven reducer names `reduce_collection` dispatches today.
REDUCERS = ("mean", "median", "min", "max", "mode", "mosaic", "sum")

#: One representative request, reused so every lock describes the same read.
ASSET = "USGS/SRTMGL1_003"
BANDS = ["elevation"]


def _batch_gee(make_gee, **overrides):
    """A GEE configured for one of the asynchronous batch sinks."""
    params = {
        "variables": {ASSET: BANDS},
        "start": "2000-02-11",
        "end": "2000-02-12",
        "scale": 90.0,
    }
    params.update(overrides)
    return make_gee(**params)


class TestPrefix:
    """The filename stem every sink derives its destination from."""

    def test_prefix_is_slug_bands_date(self, make_gee):
        """`<slug>_<bands joined by ->_<YYYYMMDD>` — the stem all sinks share."""
        from earthlens.gee._helpers import slug_asset_id

        gee = _batch_gee(make_gee)
        var_info = gee.catalog.get_dataset(ASSET)
        when = dt.datetime(2022, 1, 1)
        expected = f"{slug_asset_id(var_info.id)}_{'-'.join(BANDS)}_{when:%Y%m%d}"
        assert expected == "USGS_SRTMGL1_003_elevation_20220101"

    def test_multiband_prefix_joins_with_hyphen(self, make_gee):
        """Several bands join with `-`, not `_`, so the date stays parseable."""
        from earthlens.gee._helpers import slug_asset_id

        bands = ["B4", "B3", "B2"]
        stem = f"{slug_asset_id('COPERNICUS/S2')}_{'-'.join(bands)}_20220101"
        assert stem == "COPERNICUS_S2_B4-B3-B2_20220101"


class TestBatchSinks:
    """Which `to*` each `export_via` reaches, with which kwargs, returning what.

    Driven through `download()` rather than the private export helper, so the lock
    survives Task 1.4 relocating the body into `Sink` objects.
    """

    PREFIX = "USGS_SRTMGL1_003_elevation_20000211"

    def test_drive_calls_to_drive_and_returns_destination(self, make_gee):
        """`toDrive(folder=, fileNamePrefix=, **common)` -> `drive://<folder>/<prefix>`."""
        gee = _batch_gee(make_gee, export_via="drive", drive_folder="my-folder")
        assert gee.download(progress_bar=False) == [f"drive://my-folder/{self.PREFIX}"]
        ((method, kwargs),) = gee.client.export_image.calls
        assert method == "toDrive"
        assert kwargs["folder"] == "my-folder"
        assert kwargs["fileNamePrefix"] == self.PREFIX

    def test_gcs_calls_to_cloud_storage_and_returns_gs_uri(self, make_gee):
        """`toCloudStorage(bucket=, fileNamePrefix=, **common)` -> `gs://<bucket>/<prefix>`."""
        gee = _batch_gee(make_gee, export_via="gcs", gcs_bucket="my-bucket")
        assert gee.download(progress_bar=False) == [f"gs://my-bucket/{self.PREFIX}"]
        ((method, kwargs),) = gee.client.export_image.calls
        assert method == "toCloudStorage"
        assert kwargs["bucket"] == "my-bucket"
        assert kwargs["fileNamePrefix"] == self.PREFIX

    def test_asset_uses_asset_id_not_file_name_prefix(self, make_gee):
        """The asset sink addresses `<asset_id>/<prefix>` and returns `ee://`."""
        gee = _batch_gee(
            make_gee, export_via="asset", asset_id="projects/p/assets/folder"
        )
        expected = f"projects/p/assets/folder/{self.PREFIX}"
        assert gee.download(progress_bar=False) == [f"ee://{expected}"]
        ((method, kwargs),) = gee.client.export_image.calls
        assert method == "toAsset"
        assert kwargs["assetId"] == expected
        assert "fileNamePrefix" not in kwargs

    def test_trailing_slash_on_asset_id_does_not_double(self, make_gee):
        """A trailing `/` on `asset_id` must not produce `//` in the asset path."""
        gee = _batch_gee(
            make_gee, export_via="asset", asset_id="projects/p/assets/folder/"
        )
        (out,) = gee.download(progress_bar=False)
        assert "//" not in out.removeprefix("ee://")

    def test_common_export_kwargs(self, make_gee):
        """Every batch sink shares `description`, `region`, `scale`, `crs`, `maxPixels`."""
        gee = _batch_gee(make_gee, export_via="drive", drive_folder="f")
        gee.download(progress_bar=False)
        ((_method, kwargs),) = gee.client.export_image.calls
        assert kwargs["scale"] == 90.0
        assert kwargs["crs"] == "EPSG:4326"
        assert kwargs["maxPixels"] == 1e13
        assert len(kwargs["description"]) <= 100
        assert "image" in kwargs and "region" in kwargs

    def test_wait_false_returns_taskinfo(self, make_gee):
        """`wait_for_export=False` submits and returns a `TaskInfo`, not a string."""
        gee = _batch_gee(
            make_gee, export_via="drive", drive_folder="f", wait_for_export=False
        )
        (out,) = gee.download(progress_bar=False)
        assert type(out).__name__ == "TaskInfo"
        assert gee.client.export_image.tasks[0].started is True

    def test_url_sink_writes_a_file_and_returns_its_path(self, make_gee):
        """The `url` sink returns a written `Path`, not a destination string.

        Pinned with `engine="ee"`: with the `[eedai]` extra installed the default
        `auto` would route the same request to the reader instead, which is locked
        separately in `TestEngineRouting`.
        """
        from pathlib import Path

        gee = _batch_gee(make_gee, engine="ee")
        (out,) = gee.download(progress_bar=False)
        assert isinstance(out, Path)
        assert out.name.startswith(self.PREFIX)
        assert gee.client.export_image.calls == []


class TestEngineRouting:
    """`_use_eedai`'s verdicts for each `engine` value."""

    def test_engine_ee_short_circuits(self, make_gee, fake_reader):
        """`engine="ee"` never consults the reader: `(False, None)`."""
        gee = _batch_gee(make_gee, engine="ee")
        var_info = gee.catalog.get_dataset(ASSET)
        assert gee._use_eedai(var_info, 1) == (False, None)

    def test_forced_eedai_raises_when_ineligible(self, make_gee, fake_reader):
        """`engine="eedai"` with a `cloud_mask` is ineligible and must raise."""
        gee = _batch_gee(make_gee, engine="eedai", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        with pytest.raises(ValueError, match="engine='eedai' cannot serve"):
            gee._use_eedai(var_info, 1)

    def test_auto_falls_back_when_ineligible(self, make_gee, fake_reader):
        """`engine="auto"` with a `cloud_mask` declines quietly to Earth Engine."""
        gee = _batch_gee(make_gee, engine="auto", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        assert gee._use_eedai(var_info, 1) == (False, None)

    def test_auto_serves_an_eligible_single_image(self, make_gee, fake_reader):
        """An eligible raw read routes to the reader with a servable plan."""
        gee = _batch_gee(make_gee, engine="auto")
        var_info = gee.catalog.get_dataset(ASSET)
        use_reader, plan = gee._use_eedai(var_info, 1)
        assert use_reader is True
        assert plan is not None
        assert plan.can_serve is True
        assert plan.tiles >= 1

    def test_plan_fields_are_addressable_by_name(self, make_gee, fake_reader):
        """The plan exposes `can_serve`/`tile_size`/`tiles`/`reason` by name.

        Task 1.6 promotes this from a `NamedTuple` to a frozen model; attribute
        access must survive that, which positional unpacking would not.
        """
        gee = _batch_gee(make_gee, engine="auto")
        var_info = gee.catalog.get_dataset(ASSET)
        _use_reader, plan = gee._use_eedai(var_info, 1)
        for field in ("can_serve", "tile_size", "tiles", "reason"):
            assert hasattr(plan, field), field


class TestReducerDispatch:
    """`reduce_collection`'s vocabulary and its band-name preservation."""

    @pytest.mark.parametrize("name", REDUCERS)
    def test_each_reducer_dispatches_to_the_same_name(self, name):
        """Every supported reducer calls the collection method of that name."""

        class _Collection:
            """Records which reducer method was invoked."""

            def __init__(self):
                self.called: list[str] = []

            def __getattr__(self, item):
                def _call():
                    self.called.append(item)
                    return f"reduced-{item}"

                return _call

        collection = _Collection()
        assert reduce_collection(collection, name) == f"reduced-{name}"
        assert collection.called == [name]

    def test_an_unknown_reducer_raises(self):
        """An unsupported name is rejected rather than silently `getattr`-ed."""
        with pytest.raises(ValueError, match="reducer"):
            reduce_collection(object(), "percentile")

    def test_the_vocabulary_is_exactly_these_seven(self):
        """Pins today's vocabulary so Task 1.2's widening is a deliberate diff."""
        from earthlens.gee._helpers import _COLLECTION_REDUCERS

        assert set(_COLLECTION_REDUCERS) == set(REDUCERS)


class TestCompositeReducerSelection:
    """`_composite` resolves the reducer from the override, else the catalog."""

    def test_constructor_reducer_wins_over_catalog_default(self, make_gee):
        """An explicit `reducer=` overrides the dataset's `default_reducer`."""
        gee = _batch_gee(
            make_gee,
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
            reducer="median",
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        assert (gee.reducer or var_info.default_reducer) == "median"

    def test_catalog_default_is_used_when_unset(self, make_gee):
        """With no override the dataset's own `default_reducer` applies."""
        gee = _batch_gee(
            make_gee,
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        assert gee.reducer is None
        assert var_info.default_reducer == "mean"


class TestEngineVocabulary:
    """The `engine` strings the constructor accepts."""

    def test_the_engine_vocabulary(self):
        """Pins the vocabulary, so adding an engine stays a deliberate diff.

        `pixels` joined in Task 1.13 — which is what this pin was written to
        surface, rather than letting a new engine appear unremarked.
        """
        assert set(backend_module._ENGINES) == {"auto", "ee", "eedai", "pixels"}

    @pytest.mark.parametrize("engine", ["auto", "ee", "eedai", "pixels"])
    def test_each_engine_is_accepted(self, make_gee, engine):
        """Each registered engine constructs without error."""
        assert _batch_gee(make_gee, engine=engine).engine == engine

    def test_an_unknown_engine_is_rejected(self, make_gee):
        """An unknown engine raises at construction, not at download."""
        with pytest.raises(ValueError, match="engine must be one of"):
            _batch_gee(make_gee, engine="telepathy")
