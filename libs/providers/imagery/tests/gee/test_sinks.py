"""Tests for the `Sink` strategy objects the batch export is now driven by."""

from __future__ import annotations

import pytest

from earthlens.gee.sinks import (
    SINKS,
    AssetSink,
    DriveSink,
    GcsSink,
    Sink,
    UrlSink,
    resolve_sink,
)

from .test_backend import fake_ee, make_gee  # noqa: F401 - registers the fixtures


class TestRegistry:
    """What `export_via=` resolves to."""

    def test_the_four_shipped_sinks_are_registered(self):
        """The vocabulary the config validates against."""
        assert set(SINKS) == {"url", "drive", "gcs", "asset"}

    @pytest.mark.parametrize(
        ("name", "cls"),
        [
            ("url", UrlSink),
            ("drive", DriveSink),
            ("gcs", GcsSink),
            ("asset", AssetSink),
        ],
    )
    def test_each_name_resolves_to_its_class(self, name, cls):
        """A string becomes the matching sink object."""
        assert isinstance(resolve_sink(name), cls)
        assert cls.name == name

    def test_every_sink_implements_the_abc(self):
        """Each registered class is a real `Sink`."""
        for cls in SINKS.values():
            assert issubclass(cls, Sink)

    def test_an_instance_passes_through(self):
        """An already-built sink is returned unchanged."""
        sink = DriveSink()
        assert resolve_sink(sink) is sink

    def test_an_unknown_name_lists_the_alternatives(self):
        """The error names what is available."""
        with pytest.raises(ValueError, match="export_via must be one of"):
            resolve_sink("ftp")

    def test_a_non_string_is_a_type_error(self):
        """A stray object fails loudly."""
        with pytest.raises(TypeError, match="name or a Sink"):
            resolve_sink(1)

    def test_repr_names_the_class(self):
        """Sinks appear in log lines."""
        assert repr(GcsSink()) == "GcsSink()"

    def test_each_resolution_builds_a_fresh_instance(self):
        """The registry holds classes, so no state leaks between backends."""
        assert resolve_sink("url") is not resolve_sink("url")


class TestPrerequisites:
    """Each sink declares the one field it cannot work without."""

    @pytest.mark.parametrize(
        ("cls", "field"),
        [
            (DriveSink, "drive_folder"),
            (GcsSink, "gcs_bucket"),
            (AssetSink, "asset_id"),
        ],
    )
    def test_each_batch_sink_declares_its_requirement(self, cls, field):
        """The requirement lives next to the sink that has it."""
        assert cls.required_config == field

    def test_the_synchronous_sink_needs_nothing(self):
        """`url` fetches now and has no destination to configure."""
        assert UrlSink.required_config is None

    def test_only_the_url_sink_is_synchronous(self):
        """`is_batch` is what distinguishes a queued export from a fetch."""
        assert UrlSink.is_batch is False
        for cls in (DriveSink, GcsSink, AssetSink):
            assert cls.is_batch is True, cls


class TestDestinations:
    """How each sink reports where the result landed."""

    PREFIX = "USGS_SRTMGL1_003_elevation_20000211"

    def test_drive_reads_as_a_drive_uri(self, make_gee):
        """`drive://<folder>/<prefix>`."""
        gee = make_gee(export_via="drive", drive_folder="ee_out")
        assert DriveSink().destination(gee, self.PREFIX) == (
            f"drive://ee_out/{self.PREFIX}"
        )

    def test_gcs_reads_as_a_gs_uri(self, make_gee):
        """`gs://<bucket>/<prefix>`."""
        gee = make_gee(export_via="gcs", gcs_bucket="my-bucket")
        assert (
            GcsSink().destination(gee, self.PREFIX) == f"gs://my-bucket/{self.PREFIX}"
        )

    def test_asset_reads_as_an_ee_uri(self, make_gee):
        """`ee://<asset_id>/<prefix>`."""
        gee = make_gee(export_via="asset", asset_id="projects/p/assets/f")
        assert AssetSink().destination(gee, self.PREFIX) == (
            f"ee://projects/p/assets/f/{self.PREFIX}"
        )

    def test_asset_strips_a_trailing_separator(self, make_gee):
        """A trailing `/` on `asset_id` must not double in the asset path."""
        gee = make_gee(export_via="asset", asset_id="projects/p/assets/f/")
        assert "//" not in AssetSink().target_asset(gee, self.PREFIX)

    def test_the_synchronous_sink_has_no_uri(self, make_gee):
        """It returns a path, so asking for a destination is a caller error."""
        gee = make_gee()
        sink = UrlSink()
        with pytest.raises(NotImplementedError, match="writes a path"):
            sink.destination(gee, self.PREFIX)


class TestTargetKeywords:
    """Which keyword each sink addresses its destination with."""

    def test_the_file_sinks_use_their_own_keyword(self, make_gee):
        """`folder` for Drive, `bucket` for GCS — read off the sink."""
        gee = make_gee(export_via="drive", drive_folder="f")
        assert DriveSink().target(gee) == {"folder": "f"}
        gee = make_gee(export_via="gcs", gcs_bucket="b")
        assert GcsSink().target(gee) == {"bucket": "b"}

    def test_the_asset_sink_uses_asset_id_and_no_prefix(self, make_gee):
        """Each export creates an asset, not a file with a name prefix."""
        gee = make_gee(export_via="asset", asset_id="projects/p/assets/f")
        target = AssetSink().target(gee, "A_B_20220101")
        assert set(target) == {"assetId"}
        assert target["assetId"].endswith("/A_B_20220101")

    def test_each_batch_sink_names_its_export_method(self):
        """The `ee.batch.Export.image` method the backend dispatches to."""
        assert DriveSink.export_method == "toDrive"
        assert GcsSink.export_method == "toCloudStorage"
        assert AssetSink.export_method == "toAsset"

    def test_each_batch_sink_declares_its_uri_scheme(self):
        """The destination strings are built from these, so they must not drift."""
        assert DriveSink.scheme == "drive"
        assert GcsSink.scheme == "gs"
        assert AssetSink.scheme == "ee"

    @pytest.mark.parametrize(
        ("cls", "extra"),
        [
            (DriveSink, {"drive_folder": "f"}),
            (GcsSink, {"gcs_bucket": "b"}),
            (AssetSink, {"asset_id": "projects/p/assets/f"}),
        ],
    )
    def test_the_destination_starts_with_the_declared_scheme(
        self, make_gee, cls, extra
    ):
        """The reported URI and the declared scheme are one statement, not two."""
        gee = make_gee(export_via=cls.name, **extra)
        assert cls().destination(gee, "A_B_20220101").startswith(f"{cls.scheme}://")

    def test_the_asset_sink_addresses_the_same_path_it_reports(self, make_gee):
        """`target` and `destination` must not disagree about where it landed."""
        gee = make_gee(export_via="asset", asset_id="projects/p/assets/f")
        sink = AssetSink()
        target = sink.target(gee, "A_B_20220101")
        assert sink.destination(gee, "A_B_20220101") == f"ee://{target['assetId']}"


class TestBackendIsSinkDriven:
    """The `if/elif` on `export_via` is gone from `_export_via_batch`."""

    @pytest.mark.parametrize(
        ("export_via", "extra", "cls"),
        [
            ("url", {}, UrlSink),
            ("drive", {"drive_folder": "f"}, DriveSink),
            ("gcs", {"gcs_bucket": "b"}, GcsSink),
            ("asset", {"asset_id": "projects/p/assets/f"}, AssetSink),
        ],
    )
    def test_the_backend_resolves_its_sink_once(self, make_gee, export_via, extra, cls):
        """`export_via=` becomes an object at construction."""
        gee = make_gee(export_via=export_via, **extra)
        assert isinstance(gee._sink, cls)

    def test_the_export_call_comes_from_the_sink(self, make_gee):
        """The method and keyword the task was built with are the sink's."""
        gee = make_gee(export_via="gcs", gcs_bucket="my-bucket")
        gee.download(progress_bar=False)
        ((method, kwargs),) = gee.client.export_image.calls
        assert method == GcsSink.export_method
        assert GcsSink.target_kwarg in kwargs

    def test_adding_a_sink_needs_no_backend_edit(self):
        """The acceptance criterion: Epic 3/6 sinks are subclasses, not branches."""

        class _Probe(Sink):
            """A throwaway sink proving registration is enough."""

            name = "probe"

            def write(self, backend, image, **kwargs):
                """Stand in for a transport; the registry is what is under test."""
                return "probed"

        SINKS["probe"] = _Probe
        try:
            assert isinstance(resolve_sink("probe"), _Probe)
        finally:
            del SINKS["probe"]
