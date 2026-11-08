"""Tests for `ExportConfig` and `EngineConfig`, the strategies' validated slices."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee.configs import DEFAULT_HTTP_TIMEOUT_S, EngineConfig, ExportConfig
from earthlens.gee.reducers import Percentile

from .test_backend import fake_ee, make_gee  # noqa: F401 - registers the fixtures


class TestExportConfigSinkPrerequisites:
    """Each asynchronous sink needs one destination field, checked up front."""

    def test_url_needs_nothing(self):
        """The synchronous default has no destination to supply."""
        assert ExportConfig().export_via == "url"

    @pytest.mark.parametrize(
        ("export_via", "field", "message"),
        [
            ("drive", "drive_folder", "requires drive_folder"),
            ("gcs", "gcs_bucket", "requires gcs_bucket"),
            ("asset", "asset_id", "requires asset_id"),
        ],
    )
    def test_each_sink_requires_its_destination(self, export_via, field, message):
        """Missing it raises at construction, not at download time."""
        with pytest.raises(ValidationError, match=message):
            ExportConfig(export_via=export_via)
        assert ExportConfig(export_via=export_via, **{field: "x"}).export_via == (
            export_via
        )

    def test_an_unknown_sink_is_rejected(self):
        """The sink vocabulary is closed."""
        with pytest.raises(ValidationError):
            ExportConfig(export_via="ftp")

    def test_an_empty_destination_counts_as_missing(self):
        """An empty string is not a folder."""
        with pytest.raises(ValidationError, match="requires drive_folder"):
            ExportConfig(export_via="drive", drive_folder="")


class TestExportConfigValues:
    """Defaults and the fields added for later tasks."""

    def test_defaults(self):
        """The shipped defaults, so a change to them is a deliberate diff."""
        config = ExportConfig()
        assert config.cog is False
        assert config.resample == "nearest"
        assert config.auto_split is False
        assert config.wait_for_export is True
        assert config.materialize is False
        assert config.http_timeout == DEFAULT_HTTP_TIMEOUT_S

    def test_nodata_and_dtype_default_to_unset(self):
        """Both are opt-in: unset means today's behaviour, unchanged."""
        config = ExportConfig()
        assert config.nodata is None
        assert config.dtype is None

    def test_nodata_can_be_declared(self):
        """The caller states the fill value rather than inheriting the driver's."""
        assert ExportConfig(nodata=-9999).nodata == -9999

    @pytest.mark.parametrize("timeout", [0, -1, -300.0])
    def test_a_non_positive_timeout_is_rejected(self, timeout):
        """A zero or negative timeout would fail every fetch immediately."""
        with pytest.raises(ValidationError):
            ExportConfig(http_timeout=timeout)

    def test_a_small_positive_timeout_is_accepted(self):
        """The bound is exclusive at zero, not a floor on the whole range."""
        assert ExportConfig(http_timeout=0.001).http_timeout == 0.001

    def test_nodata_accepts_a_float(self):
        """A float fill value is as valid as an integer one."""
        assert ExportConfig(nodata=-9999.0).nodata == -9999.0

    def test_dtype_can_be_pinned(self):
        """The caller states the output type rather than inheriting the driver's."""
        assert ExportConfig(dtype="int16").dtype == "int16"

    @pytest.mark.parametrize("export_via", ["url", "drive", "gcs", "asset"])
    def test_only_the_url_sink_is_the_default(self, export_via):
        """Every name in the vocabulary is constructible with its destination."""
        extra = {
            "drive": {"drive_folder": "f"},
            "gcs": {"gcs_bucket": "b"},
            "asset": {"asset_id": "projects/p/assets/f"},
        }.get(export_via, {})
        assert ExportConfig(export_via=export_via, **extra).export_via == export_via

    def test_is_frozen(self):
        """A strategy cannot edit the config it was handed."""
        config = ExportConfig()
        with pytest.raises(ValidationError):
            config.cog = True

    def test_rejects_an_unknown_field(self):
        """A mistyped keyword fails rather than being silently ignored."""
        with pytest.raises(ValidationError):
            ExportConfig(export_vai="url")

    def test_summary_reads_as_one_line(self):
        """It appears in logs, so it must stay readable."""
        text = str(ExportConfig(export_via="drive", drive_folder="f"))
        assert text.startswith("ExportConfig(")
        assert "drive" in text


class TestEngineConfig:
    """How the pixels are computed, and the question eligibility turns on."""

    def test_defaults(self):
        """`auto` in EPSG:4326 at the asset's native resolution."""
        config = EngineConfig()
        assert config.engine == "auto"
        assert config.crs == "EPSG:4326"
        assert config.scale is None
        assert config.filters == ()

    def test_the_optional_hooks_default_to_unset(self):
        """A bare config asks for nothing beyond a plain read."""
        config = EngineConfig()
        assert config.reducer is None
        assert config.cloud_mask is None
        assert config.property_filter is None
        assert config.region is None
        assert config.discover_extent is False

    def test_filters_are_coerced_to_a_tuple(self):
        """A list reaches the engine as the validated immutable form."""
        config = EngineConfig(filters=[lambda collection: collection])
        assert isinstance(config.filters, tuple)
        assert len(config.filters) == 1

    def test_grid_inputs_report_a_missing_scale(self):
        """`None` means the asset's native resolution, and must survive the pair."""
        assert EngineConfig().grid_inputs == ("EPSG:4326", None)

    def test_grid_inputs_are_held_once(self):
        """`crs`/`scale` live here, not duplicated onto `ExportConfig`."""
        config = EngineConfig(crs="EPSG:32636", scale=10.0)
        assert config.grid_inputs == ("EPSG:32636", 10.0)
        assert not hasattr(ExportConfig(), "crs"), (
            "duplicating the grid inputs is what leaves the derivation behind"
        )

    def test_no_compute_needed_by_default(self):
        """A bare request is a raw read, so the reader can serve it."""
        assert EngineConfig().needs_server_side_compute is False

    def test_a_cloud_mask_needs_compute(self):
        """A per-image hook is a graph the reader cannot execute."""
        assert EngineConfig(cloud_mask=lambda image: image).needs_server_side_compute

    def test_filters_need_compute(self):
        """So is a collection-level filter."""
        assert EngineConfig(filters=(lambda c: c,)).needs_server_side_compute

    def test_accepts_a_resolved_reducer_object(self):
        """A caller can pass `Percentile(q=95)` rather than a string."""
        config = EngineConfig(reducer=Percentile(q=95))
        assert config.reducer.q == 95.0

    def test_accepts_a_reducer_spec_string(self):
        """The string form still works, resolved later by `_composite`."""
        assert EngineConfig(reducer="percentile:90").reducer == "percentile:90"

    def test_high_volume_is_off_by_default(self):
        """Its trade-off is real, so it is opt-in per engine."""
        assert EngineConfig().high_volume is False

    def test_is_frozen(self):
        """An engine cannot edit the config it was handed."""
        config = EngineConfig()
        with pytest.raises(ValidationError):
            config.engine = "ee"

    def test_rejects_an_unknown_field(self):
        """A mistyped keyword fails at construction."""
        with pytest.raises(ValidationError):
            EngineConfig(enigne="ee")


class TestBackendComposesThem:
    """The backend builds both from its own kwargs and keeps the public attrs."""

    def test_configs_mirror_the_public_attributes(self, make_gee):
        """Each config field agrees with the attribute callers still read."""
        gee = make_gee(
            export_via="drive",
            drive_folder="f",
            cog=True,
            resample="average",
            crs="EPSG:32636",
            scale=10.0,
        )
        assert gee._export_config.export_via == gee.export_via
        assert gee._export_config.drive_folder == gee.drive_folder
        assert gee._export_config.cog == gee.cog
        assert gee._export_config.resample == gee.resample
        assert gee._engine_config.crs == gee.crs
        assert gee._engine_config.scale == gee.scale
        assert gee._engine_config.engine == gee.engine

    def test_filters_reach_the_engine_config_as_a_tuple(self, make_gee):
        """The validated tuple, not the raw iterable the caller passed."""
        gee = make_gee(filters=[lambda c: c])
        assert isinstance(gee._engine_config.filters, tuple)
        assert len(gee._engine_config.filters) == 1

    def test_the_sink_prerequisite_still_raises_from_the_constructor(self, make_gee):
        """The message is the backend's own, preserved through the move."""
        with pytest.raises(ValueError, match="requires drive_folder"):
            make_gee(export_via="drive")
