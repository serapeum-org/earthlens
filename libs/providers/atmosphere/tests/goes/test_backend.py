"""Unit tests for the GOES backend (no network — stubbed unsigned S3)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from earthlens.goes.backend import (
    end_is_date_only,
    enumerate_hours,
    expand_bare_date_end,
    normalize_channel,
)

from earthlens.goes import GOES

from .conftest import FakeS3

pytestmark = pytest.mark.goes

HOUR_C = "ABI-L2-MCMIPC/2026/184/12/"
HOUR_M = "ABI-L2-MCMIPM/2026/184/12/"
HOUR_RAD = "ABI-L1b-RadC/2026/184/12/"


def _mcmipc(scan: str) -> str:
    """Build a CONUS MCMIP key with the given 14-digit scan-start token."""
    return f"{HOUR_C}OR_ABI-L2-MCMIPC-M6_G19_s{scan}_e1_c1.nc"


class TestEnumerateHours:
    """Tests for enumerate_hours."""

    def test_floors_and_spans_inclusive(self):
        """The window is floored to the hour and spans start..end inclusive."""
        hours = enumerate_hours(
            dt.datetime(2026, 7, 3, 12, 5), dt.datetime(2026, 7, 3, 14, 1)
        )
        assert [h.hour for h in hours] == [12, 13, 14], "three hour buckets"

    def test_single_hour(self):
        """A sub-hour window yields exactly one hour bucket."""
        hours = enumerate_hours(
            dt.datetime(2026, 7, 3, 12, 3), dt.datetime(2026, 7, 3, 12, 47)
        )
        assert hours == [dt.datetime(2026, 7, 3, 12, 0)], "one floored hour"

    def test_inverted_window_raises(self):
        """start after end raises ValueError."""
        with pytest.raises(ValueError, match="is after end"):
            enumerate_hours(dt.datetime(2026, 7, 3, 13), dt.datetime(2026, 7, 3, 12))


class TestEndIsDateOnly:
    """Tests for end_is_date_only."""

    @pytest.mark.parametrize(
        "end, expected",
        [
            ("2026-07-03", True),
            ("2026-07-04 00:00", False),
            ("2026-07-04 12:30", False),
            ("2026-07-04T00:00:00", False),
            # A month-name format still reads as a bare date — the 't' in
            # "Oct"/"September" must not be taken for an ISO time separator.
            ("2026-Oct-03", True),
            ("03-September-2026", True),
            (dt.date(2026, 7, 3), True),
            (dt.datetime(2026, 7, 3), False),
            (1234567890, False),
        ],
    )
    def test_detects_bare_date_from_input(self, end, expected):
        """A bare date is date-only; an explicit time (even midnight) is not."""
        assert end_is_date_only(end) is expected, f"{end!r} -> {expected}"


class TestExpandBareDateEnd:
    """Tests for expand_bare_date_end."""

    def test_date_only_expands_to_end_of_day(self):
        """A date-only end bound expands to the last microsecond of the day."""
        expanded = expand_bare_date_end(dt.datetime(2026, 7, 3), date_only=True)
        assert expanded == dt.datetime(2026, 7, 3, 23, 59, 59, 999999), "end of day"

    def test_explicit_midnight_untouched(self):
        """An explicit-midnight end bound is NOT expanded (M1 regression)."""
        end = dt.datetime(2026, 7, 4)
        assert expand_bare_date_end(end, date_only=False) == end, "midnight respected"


class TestNormalizeChannel:
    """Tests for normalize_channel."""

    @pytest.mark.parametrize(
        "token, expected",
        [
            ("C2", "C02"),
            ("2", "C02"),
            ("02", "C02"),
            ("cmi_c13", "C13"),
            ("C16", "C16"),
        ],
    )
    def test_normalises_spellings(self, token, expected):
        """Several channel spellings collapse to the canonical C<nn> token."""
        assert normalize_channel(token) == expected, f"{token} -> {expected}"

    def test_no_digit_returns_upper(self):
        """A selector with no channel number is upper-cased (matches nothing)."""
        assert normalize_channel("rgb") == "RGB", "no digit -> upper-cased input"


class TestConstruction:
    """Tests for GOES.__init__ and the abstract hooks."""

    def test_defaults_resolve_bucket_and_prefix(self, make_goes):
        """The default request resolves the east bucket and CONUS prefix."""
        goes = make_goes()
        assert goes._bucket == "noaa-goes19", "east -> noaa-goes19"
        assert goes._prefix() == "ABI-L2-MCMIPC", "product group + C suffix"

    def test_default_domain_used_when_omitted(self, make_goes):
        """Omitting domain uses the product's default_domain (SST -> F)."""
        goes = make_goes(dataset="abi-l2-sst", domain=None)
        assert goes._domain_key == "F", "SST publishes only the Full Disk domain"

    def test_unknown_dataset_raises(self, make_goes):
        """An unknown dataset raises ValueError from the catalog."""
        with pytest.raises(ValueError, match="not in the GOES catalog"):
            make_goes(dataset="abi-l2-nope")

    def test_unknown_satellite_raises(self, make_goes):
        """An unknown satellite raises ValueError from the catalog."""
        with pytest.raises(ValueError, match="not a known GOES satellite"):
            make_goes(satellite="mars")

    def test_domain_not_published_raises(self, make_goes):
        """Requesting a domain the product does not publish raises ValueError."""
        with pytest.raises(ValueError, match="is not published by product"):
            make_goes(dataset="abi-l2-sst", domain="C")

    def test_str_variable_coerced_to_list(self, make_goes):
        """A single string variable is coerced to a one-element list."""
        goes = make_goes(dataset="abi-l1b-rad", variables="C02")
        assert goes._channels == ["C02"], "string channel normalised into a list"

    def test_channels_only_for_band_split(self, make_goes):
        """A combined product ignores variables for channel filtering."""
        goes = make_goes(dataset="abi-l2-mcmip", variables=["CMI_C13"])
        assert goes._channels == [], "mcmip is combined -> no channel filter"

    def test_initialize_is_noop(self, make_goes):
        """_initialize returns None (the buckets are anonymous)."""
        assert make_goes()._initialize() is None, "no auth handshake"

    def test_check_input_dates_builds_hour_index(self, make_goes):
        """_check_input_dates captures the window and its hour buckets."""
        goes = make_goes(start="2026-07-03 12:00", end="2026-07-03 13:30")
        assert len(goes.time.dates) == 2, "two hour buckets (12 and 13)"
        assert goes.time.resolution == "h", "hourly resolution label"

    def test_inverted_window_rejected_at_construction(self, make_goes):
        """Constructing GOES with start after end raises ValueError (via the hooks)."""
        with pytest.raises(ValueError, match="is after end"):
            make_goes(start="2026-07-03 13:00", end="2026-07-03 12:00")


class TestSearch:
    """Tests for GOES._search enumeration + filtering."""

    def test_plans_in_window_granules(self, make_goes, patch_client):
        """Only granules whose scan-start lands in the window are planned."""
        goes = make_goes()
        patch_client(
            goes,
            FakeS3(
                pages={HOUR_C: [_mcmipc("20261841201180"), _mcmipc("20261841240000")]}
            ),
        )
        products = goes._search()
        assert [p.id for p in products] == [
            "OR_ABI-L2-MCMIPC-M6_G19_s20261841201180_e1_c1.nc"
        ], "the 12:40 granule is outside the 12:00-12:30 window"

    def test_metadata_carries_bucket_and_scan_start(self, make_goes, patch_client):
        """Each planned product carries its bucket, product key and scan-start."""
        goes = make_goes()
        patch_client(goes, FakeS3(pages={HOUR_C: [_mcmipc("20261841201180")]}))
        meta = goes._search()[0].metadata
        assert meta["bucket"] == "noaa-goes19", "bucket recorded"
        assert meta["scan_start"] == dt.datetime(2026, 7, 3, 12, 1, 18), "parsed time"

    def test_mesoscale_subsector_filter(self, make_goes, patch_client):
        """Domain M1 keeps only the M1 subsector granules (M2 is filtered out)."""
        goes = make_goes(dataset="abi-l2-mcmip", domain="M1")
        keys = [
            f"{HOUR_M}OR_ABI-L2-MCMIPM1-M6_G19_s20261841205000_e1_c1.nc",
            f"{HOUR_M}OR_ABI-L2-MCMIPM2-M6_G19_s20261841205300_e1_c1.nc",
        ]
        patch_client(goes, FakeS3(pages={HOUR_M: keys}))
        planned = [p.id for p in goes._search()]
        assert planned == ["OR_ABI-L2-MCMIPM1-M6_G19_s20261841205000_e1_c1.nc"], (
            "only the M1 subsector is kept"
        )

    def test_channel_filter_for_band_split(self, make_goes, patch_client):
        """A band-split product with variables keeps only the requested channels."""
        goes = make_goes(dataset="abi-l1b-rad", domain="C", variables=["C02"])
        keys = [
            f"{HOUR_RAD}OR_ABI-L1b-RadC-M6C01_G19_s20261841201180_e1_c1.nc",
            f"{HOUR_RAD}OR_ABI-L1b-RadC-M6C02_G19_s20261841201180_e1_c1.nc",
        ]
        patch_client(goes, FakeS3(pages={HOUR_RAD: keys}))
        planned = [p.id for p in goes._search()]
        assert planned == ["OR_ABI-L1b-RadC-M6C02_G19_s20261841201180_e1_c1.nc"], (
            "C01 is filtered out; only the requested C02 remains"
        )

    def test_channel_filter_disambiguates_c02_from_c12(self, make_goes, patch_client):
        """The channel filter matches C02 exactly, never C12 / C20 (L1)."""
        goes = make_goes(dataset="abi-l1b-rad", domain="C", variables=["C02"])
        keys = [
            f"{HOUR_RAD}OR_ABI-L1b-RadC-M6C02_G19_s20261841201180_e1_c1.nc",
            f"{HOUR_RAD}OR_ABI-L1b-RadC-M6C12_G19_s20261841201180_e1_c1.nc",
        ]
        patch_client(goes, FakeS3(pages={HOUR_RAD: keys}))
        planned = [p.id for p in goes._search()]
        assert planned == ["OR_ABI-L1b-RadC-M6C02_G19_s20261841201180_e1_c1.nc"], (
            "C12 must not be matched when C02 is requested"
        )

    def test_mesoscale_and_channel_filters_together(self, make_goes, patch_client):
        """A band-split M1 request keeps only the M1-subsector + requested-channel key."""
        goes = make_goes(dataset="abi-l1b-rad", domain="M1", variables=["C13"])
        hour = "ABI-L1b-RadM/2026/184/12/"
        keys = [
            f"{hour}OR_ABI-L1b-RadM1-M6C13_G19_s20261841205000_e1_c1.nc",  # keep
            f"{hour}OR_ABI-L1b-RadM2-M6C13_G19_s20261841205300_e1_c1.nc",  # wrong subsector
            f"{hour}OR_ABI-L1b-RadM1-M6C07_G19_s20261841206000_e1_c1.nc",  # wrong channel
        ]
        patch_client(goes, FakeS3(pages={hour: keys}))
        planned = [p.id for p in goes._search()]
        assert planned == ["OR_ABI-L1b-RadM1-M6C13_G19_s20261841205000_e1_c1.nc"], (
            "both the subsector and the channel filter must apply on one key"
        )

    def test_combined_product_variables_not_filtered(self, make_goes, patch_client):
        """A combined product ignores variables and keeps the whole granule."""
        goes = make_goes(dataset="abi-l2-mcmip", variables=["CMI_C13"])
        patch_client(goes, FakeS3(pages={HOUR_C: [_mcmipc("20261841201180")]}))
        assert len(goes._search()) == 1, "the single multi-band granule is kept"

    def test_unparseable_key_skipped(self, make_goes, patch_client):
        """A listed key with no scan-start token is skipped, not planned."""
        goes = make_goes()
        patch_client(goes, FakeS3(pages={HOUR_C: [f"{HOUR_C}junk.nc"]}))
        assert goes._search() == [], "no parseable scan-start -> dropped"

    def test_missing_hour_logged(self, make_goes, patch_client):
        """Empty hour prefixes are surfaced in a single summary warning (G6)."""
        from loguru import logger

        goes = make_goes()
        patch_client(goes, FakeS3(pages={}))
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            assert goes._search() == [], "empty listing -> no granules"
        finally:
            logger.remove(sink_id)
        assert any("had no granules" in m for m in messages), (
            "the missing hour must be logged, not silently skipped"
        )

    def test_missing_hours_do_not_flood_warnings(self, make_goes, patch_client):
        """A wide all-empty window emits ONE summary warning, not one per hour (R2-L1)."""
        from loguru import logger

        goes = make_goes(start="2026-01-01", end="2026-02-01", fmt="%Y-%m-%d")
        patch_client(goes, FakeS3(pages={}))
        empties = []
        sink_id = logger.add(
            lambda m: empties.append(m), level="WARNING", format="{message}"
        )
        try:
            goes._search()
        finally:
            logger.remove(sink_id)
        summary = [m for m in empties if "had no granules" in m]
        assert len(summary) == 1, "one summary warning regardless of window width"

    def test_bare_date_window_spans_whole_day(self, make_goes, patch_client):
        """A bare-date request (default fmt) returns the day's granules (H1)."""
        goes = make_goes(start="2026-07-03", end="2026-07-03", fmt="%Y-%m-%d")
        patch_client(goes, FakeS3(pages={HOUR_C: [_mcmipc("20261841201180")]}))
        planned = goes._search()
        assert len(planned) == 1, "a noon granule is inside the whole-day window"
        assert len(goes.time.dates) == 24, "the bare date enumerates all 24 hours"

    def test_explicit_midnight_end_does_not_pull_next_day(self, make_goes):
        """An explicit `HH:MM` end at midnight is a tight window, not a whole day."""
        goes = make_goes(
            start="2026-07-03 22:00", end="2026-07-04 00:00", fmt="%Y-%m-%d %H:%M"
        )
        assert goes.time.end_date == dt.datetime(2026, 7, 4, 0, 0), "midnight respected"
        assert len(goes.time.dates) == 3, "22:00, 23:00, 00:00 — not the whole next day"

    def test_wide_window_warns(self, make_goes, patch_client):
        """A window wider than the threshold logs a many-round-trip warning (M2)."""
        from loguru import logger

        goes = make_goes(start="2026-01-01", end="2026-03-01", fmt="%Y-%m-%d")
        patch_client(goes, FakeS3(pages={}))
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            goes._search()
        finally:
            logger.remove(sink_id)
        assert any("S3 LIST per hour" in m for m in messages), "wide-window warning"

    def test_two_hour_window_lists_both_prefixes(self, make_goes, patch_client):
        """A window spanning two hours lists both hour prefixes."""
        goes = make_goes(start="2026-07-03 12:00", end="2026-07-03 13:30")
        pages = {
            HOUR_C: [_mcmipc("20261841205000")],
            "ABI-L2-MCMIPC/2026/184/13/": [
                "ABI-L2-MCMIPC/2026/184/13/OR_ABI-L2-MCMIPC-M6_G19_s20261841305000_e1_c1.nc"
            ],
        }
        fake = patch_client(goes, FakeS3(pages=pages))
        assert len(goes._search()) == 2, "one granule from each hour"
        assert len(fake.listed) == 2, "both hour prefixes were listed"


class TestFetchAndDownload:
    """Tests for GOES._fetch and GOES.download."""

    def test_download_writes_granules(self, make_goes, patch_client):
        """download returns the written NetCDF paths under the output dir."""
        goes = make_goes()
        patch_client(goes, FakeS3(pages={HOUR_C: [_mcmipc("20261841201180")]}))
        paths = goes.download(progress_bar=False)
        assert [Path(p).name for p in paths] == [
            "OR_ABI-L2-MCMIPC-M6_G19_s20261841201180_e1_c1.nc"
        ], "one granule written"
        assert paths[0].read_bytes().startswith(b"netcdf:"), "streamed the fake body"

    def test_download_empty_window_returns_empty_list(self, make_goes, patch_client):
        """A window matching nothing returns an empty list without fetching."""
        goes = make_goes()
        patch_client(goes, FakeS3(pages={}))
        assert goes.download(progress_bar=False) == [], "no granules -> []"

    def test_download_rejects_aggregate(self, make_goes):
        """download(aggregate=...) raises NotImplementedError (raw granules)."""
        goes = make_goes()
        with pytest.raises(NotImplementedError, match="raw, undecoded"):
            goes.download(aggregate=object())

    def test_api_composes_search_fetch(self, make_goes, patch_client):
        """_api returns the fetched paths via the search/fetch composition."""
        goes = make_goes()
        patch_client(goes, FakeS3(pages={HOUR_C: [_mcmipc("20261841201180")]}))
        assert len(goes._api()) == 1, "one granule fetched through _api"


class TestClient:
    """Tests for GOES._client caching."""

    def test_client_built_once(self, make_goes, monkeypatch):
        """_client builds the unsigned client once and caches it."""
        calls = {"n": 0}

        def _fake_builder(region):
            calls["n"] += 1
            return FakeS3()

        monkeypatch.setattr("earthlens.goes.backend.unsigned_s3_client", _fake_builder)
        goes = make_goes()
        first = goes._client()
        second = goes._client()
        assert first is second, "same client returned"
        assert calls["n"] == 1, "the unsigned client is built exactly once"


class TestNoForbiddenImports:
    """Tests that the package never imports a decoding SDK (G2 / G5)."""

    @pytest.mark.parametrize("module", ["backend", "catalog", "_helpers", "__init__"])
    def test_no_decode_imports(self, module):
        """No goes source file imports xarray / netCDF4 / goes2go."""
        import earthlens.goes as pkg

        text = (Path(pkg.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
        banned = [
            "import xarray",
            "from xarray",
            "import netCDF4",
            "from netCDF4",
            "import goes2go",
            "from goes2go",
        ]
        for statement in banned:
            assert statement not in text, f"{module}.py must not use `{statement}`"
