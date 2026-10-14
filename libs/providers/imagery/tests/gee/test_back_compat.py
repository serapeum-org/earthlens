"""Task 1.11: everything that worked before Epic 1 must still work.

Epic 1 moved ten abstractions out of `backend.py`. The promise was that it is
additive — the string constructor params keep working and resolve to strategy
objects, and every symbol a caller could previously import still resolves. This
file is that promise as a test, so a later cleanup cannot quietly drop a seam.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures

ASSET = "USGS/SRTMGL1_003"


class TestStringParamsStillWork:
    """`reducer=`, `engine=` and `export_via=` stay strings on the public API."""

    @pytest.mark.parametrize(
        "reducer", ["mean", "median", "min", "max", "mode", "mosaic", "sum"]
    )
    def test_every_reducer_name_is_accepted(self, make_gee, reducer):
        """The seven names a caller could pass before."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
            reducer=reducer,
        )
        assert gee.reducer == reducer

    @pytest.mark.parametrize("engine", ["auto", "ee", "eedai"])
    def test_every_engine_name_is_accepted(self, make_gee, engine, fake_reader):
        """The three names a caller could pass before."""
        assert make_gee(engine=engine).engine == engine

    @pytest.mark.parametrize(
        ("export_via", "extra"),
        [
            ("url", {}),
            ("drive", {"drive_folder": "f"}),
            ("gcs", {"gcs_bucket": "b"}),
            ("asset", {"asset_id": "projects/p/assets/f"}),
        ],
    )
    def test_every_sink_name_is_accepted(self, make_gee, export_via, extra):
        """The four names a caller could pass before."""
        assert make_gee(export_via=export_via, **extra).export_via == export_via

    def test_a_reducer_object_is_also_accepted_now(self, make_gee):
        """The widening: the string still works, and an object works too."""
        from earthlens.gee.reducers import Percentile

        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
            reducer=Percentile(q=90),
        )
        assert gee.reducer.q == 90.0


class TestPublicAttributesSurvive:
    """The ~25 constructor attributes callers read are untouched."""

    ATTRS = (
        "scale",
        "crs",
        "reducer",
        "export_via",
        "drive_folder",
        "gcs_bucket",
        "asset_id",
        "region",
        "http_timeout",
        "auto_split",
        "discover_extent",
        "wait_for_export",
        "cloud_mask",
        "filters",
        "engine",
        "cog",
        "resample",
        "property_filter",
    )

    @pytest.mark.parametrize("attr", ATTRS)
    def test_each_attribute_is_still_readable(self, make_gee, attr):
        """Folding them into configs must not remove the public surface."""
        assert hasattr(make_gee(), attr), attr

    def test_the_configs_agree_with_the_attributes(self, make_gee):
        """So a caller reading either sees the same thing."""
        gee = make_gee(crs="EPSG:32636", scale=10.0, cog=True)
        assert gee._engine_config.crs == gee.crs
        assert gee._engine_config.scale == gee.scale
        assert gee._export_config.cog == gee.cog


class TestMovedSymbolsStillResolve:
    """Every symbol that moved out of `backend.py` keeps its old import path."""

    def test_eedai_plan_is_still_importable_from_backend(self):
        """It was in `backend.__all__`, so the name must stay."""
        from earthlens.gee import backend

        assert backend.EedaiPlan is not None
        assert "EedaiPlan" in backend.__all__

    def test_the_engine_vocabulary_is_still_readable(self):
        """`_ENGINES` became a derived view, not a deleted name."""
        from earthlens.gee import backend

        assert "auto" in backend._ENGINES

    def test_the_reducer_vocabulary_is_still_readable(self):
        """`_COLLECTION_REDUCERS` likewise."""
        from earthlens.gee import _helpers

        assert "mean" in _helpers._COLLECTION_REDUCERS

    def test_the_sampling_whitelist_is_still_readable(self):
        """And `_REDUCER_WHITELIST`."""
        from earthlens.gee import sampling

        assert "first" in sampling._REDUCER_WHITELIST

    def test_reduce_collection_still_dispatches(self):
        """The helper is a shim now, with unchanged behaviour."""
        from earthlens.gee._helpers import reduce_collection

        class _Collection:
            def mean(self):
                return "reduced"

        assert reduce_collection(_Collection(), "mean") == "reduced"

    def test_the_monkeypatch_seams_are_still_attributes(self):
        """`eedai_available` and `credentials_for` are patched by ~40 tests.

        Both were removed as "unused" by a lint pass during this epic and had to
        be restored. They are attributes of `backend` on purpose.
        """
        from earthlens.gee import backend

        assert callable(backend.eedai_available)
        assert callable(backend.credentials_for)


class TestDownloadContract:
    """`download()` returns what it always did unless asked otherwise."""

    def test_the_default_return_is_a_list_of_paths(self, make_gee):
        """The pre-Epic-1 contract, unchanged."""
        result = make_gee(engine="ee").download(progress_bar=False)
        assert isinstance(result, list)
        assert all(isinstance(entry, Path) for entry in result)

    def test_a_batch_sink_still_returns_destination_strings(self, make_gee):
        """Also unchanged."""
        gee = make_gee(export_via="drive", drive_folder="ee_out")
        (out,) = gee.download(progress_bar=False)
        assert out.startswith("drive://")

    def test_composite_still_unpacks_as_a_four_tuple(self, make_gee):
        """Work units are tuple-compatible, so existing callers keep working."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset(ASSET)
        collection = gee._build_collection(
            var_info, ["elevation"], dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 12)
        )
        for when, image, start, end in gee._composite(
            collection, var_info, dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 12)
        ):
            assert isinstance(when, dt.datetime)
            assert image is not None
            assert isinstance(start, dt.datetime)
            assert isinstance(end, dt.datetime)

    def test_use_eedai_still_returns_the_pair(self, make_gee, fake_reader):
        """`(use_reader, plan)` — the shape the characterisation suite pinned."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset(ASSET)
        use_reader, plan = gee._use_eedai(var_info, 1)
        assert isinstance(use_reader, bool)
        assert plan is None or plan.can_serve is not None

    def test_eedai_credentials_still_returns_the_credential(
        self, make_gee, fake_reader
    ):
        """The adapter holds it now, but the accessor is unchanged."""
        gee = make_gee()
        assert gee._eedai_credentials() is gee._eedai_reader().credential


class TestRoundOneFixesStayFixed:
    """Each of these was a verified defect; a regression must fail loudly."""

    def test_a_percentile_reducer_declines_to_earth_engine(self, make_gee, fake_reader):
        """It reached the reader verbatim and aborted after fetching scenes."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
            reducer="percentile:90",
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(
            var_info, 1, dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)
        )
        assert plan is not None
        assert not plan.can_serve
        assert "vocabulary" in plan.reason

    def test_the_decline_never_reaches_the_reader(self, make_gee, fake_reader):
        """The point of declining early: no scene discovery, no credential."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
            reducer="percentile:90",
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(
            var_info, 1, dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)
        )
        assert fake_reader.calls == []

    def test_the_remedy_names_the_reducers_the_reader_knows(self):
        """A size remedy for a reducer problem was the mismatch to avoid."""
        from earthlens.gee.backend import _eedai_remedy

        remedy = _eedai_remedy("reducer='percentile' is not in the reader's vocabulary")
        assert "median" in remedy
        assert "smaller bbox" not in remedy

    def test_a_reducer_instance_still_hits_the_mosaic_guard(self, make_gee):
        """An instance is never a member of a set of strings."""
        from earthlens.gee.reducers import Mosaic

        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
            reducer=Mosaic(),
        )
        assert gee._reducer_name(gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")) == (
            "mosaic"
        )

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("median", "median"),
            ("percentile:90", "percentile"),
            (None, "mean"),
        ],
    )
    def test_the_reducer_name_is_resolved_from_every_spelling(
        self, make_gee, spec, expected
    ):
        """The guard and the reader-forward both need the name, not the form."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
            reducer=spec,
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        assert gee._reducer_name(var_info) == expected

    def test_the_no_scenes_marker_is_one_literal(self):
        """Two copies matched by prefix would let the bucket-skip drift."""
        from earthlens.gee import backend
        from earthlens.gee.engines import NO_SCENES

        assert backend._EEDAI_NO_SCENES is NO_SCENES
