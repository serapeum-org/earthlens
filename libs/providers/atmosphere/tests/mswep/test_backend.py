"""Unit tests for `earthlens.mswep.backend`."""

from __future__ import annotations

import warnings

import pytest

from earthlens.biodiversity import LicenseWarning
from earthlens.mswep.backend import CADENCES, MSWEP, _safe_destination

pytestmark = [pytest.mark.mswep, pytest.mark.unit]


class TestSafeDestination:
    """The Drive-derived download path is validated against traversal."""

    def test_normal_path_joins_under_root(self, tmp_path):
        """A well-formed folder chain joins beneath the output root."""
        dest = _safe_destination(tmp_path, "MSWEP_V315/Past/Daily", "2020116.nc")
        assert dest == tmp_path / "MSWEP_V315" / "Past" / "Daily" / "2020116.nc"

    @pytest.mark.parametrize("evil", ["..", "MSWEP/../secret", "a\\b"])
    def test_traversal_component_is_rejected(self, tmp_path, evil):
        """A `..` or embedded-separator component from Drive is refused."""
        with pytest.raises(ValueError, match="unsafe path component"):
            _safe_destination(tmp_path, evil, "2020116.nc")


def root_folder_id(share, product="mswep", version=None):
    """Return the fake share's version-root folder id — what a user is given."""
    name = (
        "MSWX_V100"
        if product == "mswx"
        else "MSWEP_V280"
        if version == "2.80"
        else "MSWEP_V315"
    )
    return share.path_id(name)


@pytest.fixture
def build(share, tmp_path):
    """Return a factory building an `MSWEP` against the fake share."""

    def _build(**kwargs):
        kwargs.setdefault("start", "2020-04-25")
        kwargs.setdefault("end", "2020-04-26")
        kwargs.setdefault("temporal_resolution", "daily")
        kwargs.setdefault(
            "folder_id",
            root_folder_id(
                share, kwargs.get("product", "mswep"), kwargs.get("version")
            ),
        )
        return MSWEP(service=share, path=tmp_path, **kwargs)

    return _build


class TestLazyAuth:
    """Authentication is deferred to first client use, not construction."""

    def test_client_opens_lazily_not_at_construction(self, build, share):
        """The Drive client opens on first `client` access, then caches."""
        source = build()
        assert "_client_obj" not in source.__dict__  # not opened at construction
        assert source.client is share  # opens now, returns the live service
        assert "_client_obj" in source.__dict__  # cached thereafter


def _quiet_download(source):
    """Download while suppressing the always-emitted licence warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LicenseWarning)
        return source.download(progress_bar=False)


class TestMswxRealtimeRouting:
    """Auto-routed MSWX that `Past` cannot serve fails loud, not empty."""

    def test_shadowed_realtime_variant_identifies_nrt_for_mswx(self, build):
        """MSWX auto-routing shadows the open-ended NRT stream."""
        source = build(
            start="2026-08-01", end="2026-08-01", product="mswx", variables=["Temp"]
        )
        assert source._shadowed_realtime_variant() == ("Past", "NRT")

    def test_mswep_auto_routing_shadows_nothing(self, build):
        """MSWEP's dated split shadows no open-ended stream (Past_nogauge is bounded)."""
        source = build(start="2020-05-01", end="2020-05-01")
        assert source._shadowed_realtime_variant() is None

    def test_explicit_variant_is_never_flagged(self, build):
        """Naming a variant opts out of the shadow check."""
        source = build(
            start="2026-08-01",
            end="2026-08-01",
            product="mswx",
            variables=["Temp"],
            variant="NRT",
        )
        assert source._shadowed_realtime_variant() is None

    def test_auto_routed_mswx_gap_raises_naming_nrt(self, build):
        """A variant=None MSWX request Past can't serve raises, pointing at NRT."""
        source = build(
            start="2026-08-01", end="2026-08-01", product="mswx", variables=["Temp"]
        )
        with pytest.raises(ValueError, match="NRT"):
            _quiet_download(source)

    def test_auto_routed_mswep_gap_returns_empty(self, build):
        """A genuinely-absent MSWEP granule still returns [] (no false NRT hint)."""
        assert _quiet_download(build(start="2020-05-01", end="2020-05-01")) == []


class TestContract:
    """The `AbstractDataSource` surface."""

    def test_output_kind_is_raster(self, build):
        """Every granule is a gridded product."""
        assert build().OUTPUT_KIND == "raster"

    def test_does_not_support_aggregate(self, build):
        """The backend ships raw granules and never reduces them."""
        assert build().SUPPORTS_AGGREGATE is False

    def test_aggregate_is_refused_centrally(self, build):
        """A non-`None` aggregate is refused by the base wrapper."""
        with pytest.raises(NotImplementedError, match="aggregate"):
            build().download(progress_bar=False, aggregate=object())

    def test_aggregate_none_is_absorbed(self, build):
        """`aggregate=None` still works for callers that forward it."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            assert build().download(progress_bar=False, aggregate=None)

    def test_spatial_extent_defaults_to_global(self, build):
        """Granules are global, so the default bbox is the whole grid."""
        space = build().space
        assert (space.west, space.east) == (-180.0, 180.0)

    def test_date_axis_expands_per_granule(self, build):
        """A two-day daily window yields two timesteps."""
        assert len(build().time.dates) == 2

    def test_every_cadence_is_mapped(self):
        """Each catalog resolution has a pandas offset alias."""
        assert set(CADENCES) == {"hourly", "3hourly", "daily", "monthly"}


class TestDownload:
    """The happy paths."""

    def test_returns_written_paths(self, build):
        """A daily window returns one path per published granule."""
        paths = _quiet_download(build())
        assert [p.name for p in paths] == ["2020116.nc", "2020117.nc"]

    def test_files_land_on_disk(self, build):
        """The granules are actually written, with their bytes."""
        paths = _quiet_download(build())
        assert all(p.exists() for p in paths)
        assert paths[0].read_bytes() == b"CDF fake"

    def test_no_partial_files_remain(self, build, tmp_path):
        """The atomic write leaves no `.part` sibling behind."""
        _quiet_download(build())
        assert list(tmp_path.rglob("*.part")) == []

    def test_output_mirrors_the_share_layout(self, build, tmp_path):
        """Granules land under `<root>/<variant>/<temporal>/`, as rclone writes them."""
        paths = _quiet_download(build())
        assert paths[0].relative_to(tmp_path).as_posix() == (
            "MSWEP_V315/Past/Daily/2020116.nc"
        )

    def test_mswx_output_includes_the_variable_level(self, build, tmp_path):
        """An MSWX granule keeps its variable folder on disk."""
        paths = _quiet_download(
            build(
                start="2007-05-13",
                end="2007-05-13",
                product="mswx",
                variables=["Temp"],
            )
        )
        assert paths[0].relative_to(tmp_path).as_posix() == (
            "MSWX_V100/Past/Temp/Daily/2007133.nc"
        )

    def test_variables_sharing_a_granule_name_do_not_collide(self, share, tmp_path):
        """Two MSWX variables for one date write to distinct paths.

        Granule names repeat across variables, so a flat output directory
        would make the second request reuse the first file.
        """
        share.add_tree(
            share.path_id("MSWX_V100/Past"), {"Pres": {"Daily": ["2007133.nc"]}}
        )
        source = MSWEP(
            start="2007-05-13",
            end="2007-05-13",
            product="mswx",
            variables=["Temp", "Pres"],
            temporal_resolution="daily",
            folder_id=share.path_id("MSWX_V100"),
            service=share,
            path=tmp_path,
        )
        paths = _quiet_download(source)
        assert len(paths) == 2
        assert len(set(paths)) == 2
        assert len(share.media_calls) == 2

    def test_monthly_uses_the_yyyymm_stem(self, build):
        """A monthly request names granules `YYYYMM.nc`."""
        paths = _quiet_download(
            build(start="2020-04-01", end="2020-04-30", temporal_resolution="monthly")
        )
        assert [p.name for p in paths] == ["202004.nc"]

    def test_hourly_uses_the_yyyydoy_hh_stem(self, build):
        """An hourly request names granules `YYYYDOY.HH.nc`."""
        paths = _quiet_download(
            build(start="2020-04-25", end="2020-04-26", temporal_resolution="hourly")
        )
        assert [p.name for p in paths] == ["2020116.18.nc"]

    def test_empty_window_returns_empty_list(self, build):
        """Nothing published in the window yields `[]`, never `None`."""
        assert _quiet_download(build(start="1990-01-01", end="1990-01-02")) == []


class TestPathShapes:
    """MSWEP is flat; MSWX carries a variable level."""

    def test_mswx_descends_through_the_variable_folder(self, build):
        """An MSWX request resolves `<root>/<variant>/<variable>/<temporal>`."""
        paths = _quiet_download(
            build(
                start="2007-05-13",
                end="2007-05-13",
                product="mswx",
                variables=["Temp"],
            )
        )
        assert [p.name for p in paths] == ["2007133.nc"]

    def test_unknown_mswx_variable_raises(self, build):
        """A variable the catalog does not carry is rejected."""
        source = build(
            start="2007-05-13", end="2007-05-13", product="mswx", variables=["Nope"]
        )
        with pytest.raises(ValueError, match="not a mswx variable"):
            _quiet_download(source)

    def test_confirmed_mswx_variable_resolves(self, share, tmp_path):
        """A variable folder confirmed in the share resolves and downloads."""
        share.add_tree(
            share.path_id("MSWX_V100/Past"), {"SWd": {"Daily": ["2007133.nc"]}}
        )
        source = MSWEP(
            start="2007-05-13",
            end="2007-05-13",
            product="mswx",
            variables=["SWd"],
            temporal_resolution="daily",
            folder_id=share.path_id("MSWX_V100"),
            service=share,
            path=tmp_path,
        )
        assert [p.name for p in _quiet_download(source)] == ["2007133.nc"]

    def test_mswx_without_variables_raises(self, build):
        """MSWX shards by variable, so selecting none is an error, not an empty result."""
        source = build(
            start="2007-05-13", end="2007-05-13", product="mswx", variables=[]
        )
        with pytest.raises(ValueError, match="at least one must be requested"):
            _quiet_download(source)

    def test_unknown_mswep_variable_raises(self, build):
        """A bogus MSWEP variable is rejected rather than silently ignored."""
        source = build(variables=["temperature"])
        with pytest.raises(ValueError, match="not a mswep variable"):
            _quiet_download(source)

    def test_unknown_product_raises(self, build):
        """An unknown product key is rejected at construction."""
        with pytest.raises(ValueError, match="not in the MSWEP catalog"):
            build(product="nope")


class TestForecastVariants:
    """Ensemble forecast streams (`Mid` / `Long`)."""

    def _forecast(self, share, tmp_path, **kwargs):
        """Build an MSWX `Mid` forecast request against the fake share."""
        kwargs.setdefault("start", "2026-08-01")
        kwargs.setdefault("end", "2026-08-02")
        kwargs.setdefault("variant", "Mid")
        kwargs.setdefault("init", "2026-08-01")
        kwargs.setdefault("variables", ["Temp"])
        kwargs.setdefault("temporal_resolution", "daily")
        return MSWEP(
            product="mswx",
            folder_id=share.path_id("MSWX_V100"),
            service=share,
            path=tmp_path,
            **kwargs,
        )

    def test_forecast_downloads_the_members(self, share, tmp_path):
        """A forecast request fetches one granule per member per valid day."""
        paths = _quiet_download(self._forecast(share, tmp_path, members=[1, 2]))
        assert len(paths) == 4  # 2 members x 2 valid days
        assert len(set(paths)) == 4

    def test_forecast_layout_is_init_member_temporal(self, share, tmp_path):
        """Granules mirror `<variant>/<variable>/<init>/<member>/<temporal>/`."""
        paths = _quiet_download(self._forecast(share, tmp_path, members=[1]))
        assert paths[0].relative_to(tmp_path).as_posix() == (
            "MSWX_V100/Mid/Temp/20260801_00/01/Daily/2026213.nc"
        )

    def test_all_members_by_default_skips_the_absent(self, share, tmp_path):
        """`members=None` enumerates all 30; only the two present are fetched."""
        paths = _quiet_download(self._forecast(share, tmp_path))
        # fake share carries members 01 and 02 only, x 2 valid days
        assert {p.parent.parent.name for p in paths} == {"01", "02"}

    def test_forecast_requires_init(self, share, tmp_path):
        """A forecast variant with no init= is refused at construction."""
        with pytest.raises(ValueError, match="requires init="):
            self._forecast(share, tmp_path, init=None)

    def test_init_on_analysis_variant_raises(self, share, tmp_path):
        """`init=` on an analysis variant is a mistake, not silently ignored."""
        with pytest.raises(ValueError, match="applies only to a forecast"):
            MSWEP(
                start="2020-04-25",
                end="2020-04-25",
                temporal_resolution="daily",
                variant="Past",
                init="2020-04-25",
                folder_id=share.path_id("MSWEP_V315"),
                service=share,
                path=tmp_path,
            )

    def test_member_out_of_range_raises(self, share, tmp_path):
        """A member beyond the stream's ensemble size is rejected."""
        source = self._forecast(share, tmp_path, members=[99])
        with pytest.raises(ValueError, match="out of range 1..30"):
            _quiet_download(source)

    def test_empty_stream_returns_empty(self, share, tmp_path):
        """`Long` holds no data in the share, so a request yields `[]`."""
        source = self._forecast(
            share, tmp_path, variant="Long", members=[1], variables=["Temp"]
        )
        assert _quiet_download(source) == []

    def test_unparseable_init_raises(self, share, tmp_path):
        """A bad init= string is rejected at construction with a clear message."""
        with pytest.raises(ValueError, match="could not parse init"):
            self._forecast(share, tmp_path, init="not-a-date")

    def test_iso_init_is_accepted(self, share, tmp_path):
        """An ISO date for init resolves to the `YYYYMMDD_00` folder."""
        paths = _quiet_download(
            self._forecast(share, tmp_path, init="2026-08-01", members=[1])
        )
        assert "/20260801_00/" in paths[0].as_posix()


class TestVariantRouting:
    """Date-determined variant selection (`G10`)."""

    def test_historical_dates_route_to_past(self, build):
        """A 2020 window resolves under `Past`."""
        assert build()._variant_for(__import__("datetime").date(2020, 4, 25)) == "Past"

    def test_recent_dates_route_to_nrt(self, build):
        """A 2025 window resolves under `NRT`."""
        assert build()._variant_for(__import__("datetime").date(2025, 6, 1)) == "NRT"

    def test_window_straddling_the_cutover_spans_both(self, build):
        """A window crossing 2024/2025 fetches from both variants."""
        paths = _quiet_download(build(start="2024-12-31", end="2025-01-01"))
        assert [p.name for p in paths] == ["2025001.nc"]

    def test_explicit_variant_outside_its_window_raises(self, build):
        """Asking `Past` for a 2025 date names the variant that can serve it."""
        source = build(start="2025-06-01", end="2025-06-01", variant="Past")
        with pytest.raises(ValueError, match="Use variant='NRT'"):
            _quiet_download(source)

    def test_explicit_variant_inside_its_window_is_honoured(self, build):
        """A valid explicit variant is used as given."""
        paths = _quiet_download(
            build(start="2025-01-01", end="2025-01-01", variant="NRT")
        )
        assert [p.name for p in paths] == ["2025001.nc"]

    def test_unknown_variant_raises_at_construction(self, build):
        """A misspelled variant fails before any network work."""
        with pytest.raises(ValueError, match="not a mswep variant"):
            build(variant="Passt")

    def test_date_before_the_record_raises(self, build):
        """A pre-1979 date is covered by no variant."""
        source = build(start="1970-01-01", end="1970-01-01")
        with pytest.raises(ValueError, match="no mswep variant covers"):
            _quiet_download(source)


class TestMissingGranules:
    """A gap is logged and skipped, never silently dropped."""

    def test_missing_granule_is_skipped_not_raised(self, build):
        """An absent granule shortens the result rather than failing."""
        paths = _quiet_download(build(start="2020-04-25", end="2020-04-27"))
        assert [p.name for p in paths] == ["2020116.nc", "2020117.nc"]

    def test_historical_gap_is_not_blamed_on_nrt_latency(self, build, loguru_messages):
        """A `Past` gap says the granule is absent, not merely unpublished."""
        _quiet_download(build(start="2020-04-25", end="2020-04-27"))
        text = "".join(loguru_messages)
        assert "absent from the share" in text
        assert "NRT latency" not in text

    def test_nrt_gap_explains_the_latency(self, build, loguru_messages):
        """An `NRT` gap cites the ~2 h publication latency."""
        _quiet_download(build(start="2025-01-01", end="2025-01-02", variant="NRT"))
        assert "NRT latency" in "".join(loguru_messages)

    def test_absent_folder_is_skipped_with_a_warning(self, build, loguru_messages):
        """A variant folder missing from the share does not crash the request."""
        paths = _quiet_download(build(variant="Past_nogauge"))
        assert paths == []
        assert "is not in the share" in "".join(loguru_messages)


class TestLicence:
    """CC-BY-NC obligations ride along with every request."""

    def test_download_emits_a_license_warning(self, build):
        """Every request warns, since the data is non-commercial only."""
        with pytest.warns(LicenseWarning):
            build().download(progress_bar=False)

    def test_warning_carries_the_required_citation(self, build):
        """The message names the citation the licence requires."""
        with pytest.warns(LicenseWarning, match="Wang"):
            build().download(progress_bar=False)

    def test_warning_names_the_licence(self, build):
        """The message states the licence explicitly."""
        with pytest.warns(LicenseWarning, match="CC-BY-NC"):
            build().download(progress_bar=False)


class TestEnumerationEfficiency:
    """Granules are resolved by name, never by folder enumeration."""

    def test_a_day_of_hourly_costs_one_name_query(self, build, share):
        """A day of hourly granules resolves in a single chunked query."""
        source = build(
            start="2020-04-25", end="2020-04-26", temporal_resolution="hourly"
        )
        before = len(share.list_calls)
        _quiet_download(source)
        name_queries = [
            call for call in share.list_calls[before:] if "2020116.18.nc" in call["q"]
        ]
        assert len(name_queries) == 1

    def test_folder_chain_is_cached_across_groups(self, build, share):
        """The structural walk happens once, not per granule."""
        source = build(start="2020-04-25", end="2020-04-26")
        _quiet_download(source)
        folder_lookups = [call for call in share.list_calls if "mimeType" in call["q"]]
        # One share-root listing plus one lookup per level (variant, temporal).
        assert len(folder_lookups) <= 3

    def test_large_request_warns_about_bulk(self, build, loguru_messages):
        """A window past the threshold points the user at rclone."""
        source = build(
            start="2020-01-01", end="2020-12-31", temporal_resolution="hourly"
        )
        _quiet_download(source)
        assert "rclone" in "".join(loguru_messages)


class TestNoDecode:
    """The backend ships raw granules; decoding is pyramids' job."""

    def test_package_never_imports_xarray(self):
        """No module under `earthlens.mswep` may import xarray or netCDF4."""
        import pathlib

        import earthlens.mswep as package

        root = pathlib.Path(package.__file__).parent
        for module in root.glob("*.py"):
            text = module.read_text(encoding="utf-8")
            assert "import xarray" not in text, module.name
            assert "import netCDF4" not in text, module.name
