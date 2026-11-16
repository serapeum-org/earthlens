"""Tests for the NHC operational best-track (ATCF btk) fetch + parse module."""

from __future__ import annotations

import pandas as pd
import pytest
import requests

from earthlens.tropycal import nhc

pytestmark = pytest.mark.tropycal


def _atcf_line(
    *,
    time: str,
    lat: str,
    lon: str,
    vmax: int,
    mslp: int,
    type_: str = "HU",
    threshold: int = 0,
    radii: tuple[int, int, int, int] = (0, 0, 0, 0),
    name: str = "",
    prefix: str = "EP",
    number: str = "12",
    track_type: str = "BEST",
    minute: str = "",
) -> str:
    """Build one comma-delimited ATCF best-track line with 28 fields."""
    fields = [""] * 28
    fields[0] = prefix
    fields[1] = number
    fields[2] = time
    fields[3] = minute
    fields[4] = track_type
    fields[5] = "0"
    fields[6] = lat
    fields[7] = lon
    fields[8] = str(vmax)
    fields[9] = str(mslp)
    fields[10] = type_
    fields[11] = str(threshold)
    fields[12] = "NEQ"
    fields[13], fields[14], fields[15], fields[16] = (str(r) for r in radii)
    fields[27] = name
    return ", ".join(fields)


class _FakeResponse:
    """Stand-in for a `requests` response exposing `text` + `raise_for_status`."""

    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class _FakeGetter:
    """A `get` double returning canned responses keyed by URL."""

    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float | None = None) -> _FakeResponse:
        self.calls.append(url)
        if url not in self._responses:
            raise requests.ConnectionError(f"no route to {url}")
        return self._responses[url]


class TestParseBtkFile:
    """Tests for parse_btk_file (folding, parsing, naming)."""

    def test_folds_thresholds_into_one_record(self):
        """The 34/50/64-kt rows of a fix fold into one record's radii columns."""
        text = "\n".join(
            [
                _atcf_line(
                    time="2026090218",
                    lat="133N",
                    lon="1537W",
                    vmax=140,
                    mslp=927,
                    threshold=34,
                    radii=(80, 60, 60, 80),
                    name="LOWELL",
                ),
                _atcf_line(
                    time="2026090218",
                    lat="133N",
                    lon="1537W",
                    vmax=140,
                    mslp=927,
                    threshold=50,
                    radii=(40, 35, 30, 40),
                    name="LOWELL",
                ),
                _atcf_line(
                    time="2026090218",
                    lat="133N",
                    lon="1537W",
                    vmax=140,
                    mslp=927,
                    threshold=64,
                    radii=(30, 25, 20, 30),
                    name="LOWELL",
                ),
            ]
        )
        frame = nhc.parse_btk_file(text, storm_id="EP122026", basin="east_pacific")
        assert len(frame) == 1
        row = frame.iloc[0]
        assert row["vmax"] == 140
        assert row["mslp"] == 927
        assert (row["r34_ne"], row["r34_se"], row["r34_sw"], row["r34_nw"]) == (
            80.0,
            60.0,
            60.0,
            80.0,
        )
        assert (row["r64_ne"], row["r64_se"], row["r64_sw"], row["r64_nw"]) == (
            30.0,
            25.0,
            20.0,
            30.0,
        )
        assert row["id"] == "EP122026"
        assert row["name"] == "LOWELL"
        assert row["wmo_basin"] == "east_pacific"

    def test_latlon_hemisphere_and_tenths(self):
        """Lat/lon parse as tenths of a degree with S/W negated."""
        text = _atcf_line(
            time="2026082518",
            lat="101N",
            lon="1367W",
            vmax=15,
            mslp=1008,
            type_="DB",
        )
        frame = nhc.parse_btk_file(text, storm_id="EP122026", basin="east_pacific")
        assert frame.iloc[0]["lat"] == pytest.approx(10.1)
        assert frame.iloc[0]["lon"] == pytest.approx(-136.7)

    def test_weak_fix_has_nan_radii(self):
        """A threshold-0 fix (weak storm) leaves every radii column NaN."""
        text = _atcf_line(
            time="2026082518",
            lat="101N",
            lon="1367W",
            vmax=15,
            mslp=1008,
            type_="DB",
            threshold=0,
        )
        frame = nhc.parse_btk_file(text, storm_id="EP122026", basin="east_pacific")
        assert frame[list(nhc.RADII_COLUMNS)].isna().all(axis=None)

    def test_name_prefers_assigned_over_placeholder(self):
        """The assigned name wins over a numbered genesis / INVEST label."""
        text = "\n".join(
            [
                _atcf_line(
                    time="2026082518",
                    lat="101N",
                    lon="1367W",
                    vmax=15,
                    mslp=1008,
                    type_="DB",
                    name="GENESIS021",
                ),
                _atcf_line(
                    time="2026090218",
                    lat="133N",
                    lon="1537W",
                    vmax=140,
                    mslp=927,
                    threshold=34,
                    radii=(80, 60, 60, 80),
                    name="LOWELL",
                ),
                _atcf_line(
                    time="2026090300",
                    lat="140N",
                    lon="1550W",
                    vmax=120,
                    mslp=940,
                    name="INVEST",
                ),
            ]
        )
        frame = nhc.parse_btk_file(text, storm_id="EP122026", basin="east_pacific")
        assert set(frame["name"]) == {"LOWELL"}

    def test_skips_non_best_rows(self):
        """Forecast-aid (non-BEST) rows are ignored."""
        text = "\n".join(
            [
                _atcf_line(
                    time="2026090218",
                    lat="133N",
                    lon="1537W",
                    vmax=140,
                    mslp=927,
                    track_type="OFCL",
                ),
                _atcf_line(
                    time="2026090218",
                    lat="133N",
                    lon="1537W",
                    vmax=140,
                    mslp=927,
                    track_type="BEST",
                    name="LOWELL",
                ),
            ]
        )
        frame = nhc.parse_btk_file(text, storm_id="EP122026", basin="east_pacific")
        assert len(frame) == 1

    def test_malformed_time_row_dropped(self):
        """A row whose time field is not YYYYMMDDHH is skipped."""
        text = _atcf_line(
            time="BADTIME",
            lat="133N",
            lon="1537W",
            vmax=140,
            mslp=927,
        )
        frame = nhc.parse_btk_file(text, storm_id="EP122026", basin="east_pacific")
        assert frame.empty

    def test_empty_file_returns_empty_frame(self):
        """A file with no usable rows yields an empty DataFrame."""
        assert nhc.parse_btk_file("", storm_id="EP122026", basin="east_pacific").empty


class TestFetchBtkFrames:
    """Tests for fetch_btk_frames (listing, year filter, resilience)."""

    def _listing(self) -> str:
        """A directory listing naming AL/EP files across two seasons."""
        names = ["bal012026.dat", "bep112026.dat", "bep122026.dat", "bep012025.dat"]
        return "".join(f'<a href="{n}">{n}</a>\n' for n in names)

    def _storm_response(self, name: str) -> _FakeResponse:
        """A one-fix btk response for a named EP file."""
        return _FakeResponse(
            _atcf_line(
                time="2026090218",
                lat="133N",
                lon="1537W",
                vmax=120,
                mslp=940,
                threshold=64,
                radii=(30, 25, 20, 30),
                name="LOWELL",
            )
        )

    def test_selects_basin_and_year(self):
        """Only the requested basin's files for the requested year are fetched."""
        base = nhc.BTK_BASE_URL
        getter = _FakeGetter(
            {
                base: _FakeResponse(self._listing()),
                f"{base}bep112026.dat": self._storm_response("bep112026.dat"),
                f"{base}bep122026.dat": self._storm_response("bep122026.dat"),
            }
        )
        frames = nhc.fetch_btk_frames("east_pacific", range(2026, 2027), get=getter)
        assert len(frames) == 2
        assert {f["id"].iloc[0] for f in frames} == {"EP112026", "EP122026"}
        assert not any("bal012026" in url for url in getter.calls)
        assert not any("bep012025" in url for url in getter.calls)

    def test_listing_failure_returns_empty(self):
        """A directory listing that cannot be read yields no frames (logged)."""
        getter = _FakeGetter({})
        assert nhc.fetch_btk_frames("east_pacific", [2026], get=getter) == []

    def test_one_bad_file_is_skipped(self):
        """A single storm file that errors is skipped; the rest still return."""
        base = nhc.BTK_BASE_URL
        getter = _FakeGetter(
            {
                base: _FakeResponse(self._listing()),
                f"{base}bep112026.dat": _FakeResponse("", status=404),
                f"{base}bep122026.dat": self._storm_response("bep122026.dat"),
            }
        )
        frames = nhc.fetch_btk_frames("east_pacific", [2026], get=getter)
        assert [f["id"].iloc[0] for f in frames] == ["EP122026"]

    def test_zero_radii_row_leaves_nan(self):
        """A 34-kt row whose four radii are all 0 leaves those columns NaN."""
        text = _atcf_line(
            time="2026090218",
            lat="133N",
            lon="1537W",
            vmax=65,
            mslp=985,
            threshold=34,
            radii=(0, 0, 0, 0),
            name="LOWELL",
        )
        frame = nhc.parse_btk_file(text, storm_id="EP122026", basin="east_pacific")
        assert frame[["r34_ne", "r34_se", "r34_sw", "r34_nw"]].isna().all(axis=None)

    def test_invalid_calendar_date_dropped(self):
        """A 10-digit timestamp that is not a real date is skipped."""
        text = _atcf_line(
            time="2026139999",
            lat="133N",
            lon="1537W",
            vmax=140,
            mslp=927,
        )
        frame = nhc.parse_btk_file(text, storm_id="EP122026", basin="east_pacific")
        assert frame.empty


class TestParsers:
    """Tests for the private ATCF field parsers."""

    @pytest.mark.parametrize(
        "token, expected",
        [
            ("133N", 13.3),
            ("1537W", -153.7),
            ("0859W", -85.9),
            ("101S", -10.1),
            ("50E", 5.0),
        ],
    )
    def test_parse_latlon_valid(self, token, expected):
        """Valid lat/lon tokens parse as tenths of a degree, S/W negated.

        Args:
            token: The ATCF lat/lon token.
            expected: The signed decimal degrees it should yield.
        """
        assert nhc._parse_latlon(token) == pytest.approx(expected)

    @pytest.mark.parametrize("token", ["", "N", "1234X", "ABCN", "   "])
    def test_parse_latlon_malformed_is_none(self, token):
        """A too-short, bad-hemisphere, or non-numeric token yields None.

        Args:
            token: The malformed ATCF lat/lon token.
        """
        assert nhc._parse_latlon(token) is None

    @pytest.mark.parametrize(
        "token, expected",
        [(" 42 ", 42), ("0", 0), ("", None), ("abc", None), ("  ", None)],
    )
    def test_to_int(self, token, expected):
        """Integer fields parse, blank/non-numeric map to None.

        Args:
            token: The ATCF integer field.
            expected: The parsed int, or None.
        """
        assert nhc._to_int(token) == expected


def _only_forecast_body() -> _FakeResponse:
    """A btk response whose only rows are non-BEST (so it parses empty)."""
    return _FakeResponse(
        _atcf_line(
            time="2026090218",
            lat="133N",
            lon="1537W",
            vmax=140,
            mslp=927,
            track_type="OFCL",
        )
    )


class TestFetchEmptyParse:
    """A fetched file that parses to no rows is dropped, not returned."""

    def test_empty_parse_is_skipped(self):
        """A file with only forecast rows is fetched but contributes no frame."""
        base = nhc.BTK_BASE_URL
        listing = "".join(
            f'<a href="{n}">{n}</a>\n' for n in ("bep112026.dat", "bep122026.dat")
        )
        getter = _FakeGetter(
            {
                base: _FakeResponse(listing),
                f"{base}bep112026.dat": _only_forecast_body(),
                f"{base}bep122026.dat": _FakeResponse(
                    _atcf_line(
                        time="2026090218",
                        lat="133N",
                        lon="1537W",
                        vmax=120,
                        mslp=940,
                        threshold=64,
                        radii=(30, 25, 20, 30),
                        name="LOWELL",
                    )
                ),
            }
        )
        frames = nhc.fetch_btk_frames("east_pacific", [2026], get=getter)
        assert [f["id"].iloc[0] for f in frames] == ["EP122026"]
        assert f"{base}bep112026.dat" in getter.calls


_ORIG_PARSE = nhc.parse_btk_file


def _parse_failing_for_ep11(text, *, storm_id, basin):
    """Delegate to the real parser, but raise for storm EP112026 (test seam)."""
    if storm_id == "EP112026":
        raise ValueError("synthetic parse failure")
    return _ORIG_PARSE(text, storm_id=storm_id, basin=basin)


class TestFetchParseResilience:
    """A storm file that fails to PARSE is skipped, like a fetch failure (M2)."""

    def test_parse_error_is_skipped(self, monkeypatch):
        """A parse exception for one file does not abort the basin's others."""
        base = nhc.BTK_BASE_URL
        listing = "".join(
            f'<a href="{n}">{n}</a>\n' for n in ("bep112026.dat", "bep122026.dat")
        )
        good = _atcf_line(
            time="2026090218",
            lat="133N",
            lon="1537W",
            vmax=120,
            mslp=940,
            threshold=64,
            radii=(30, 25, 20, 30),
            name="LOWELL",
        )
        getter = _FakeGetter(
            {
                base: _FakeResponse(listing),
                f"{base}bep112026.dat": _FakeResponse("anything"),
                f"{base}bep122026.dat": _FakeResponse(good),
            }
        )
        monkeypatch.setattr(nhc, "parse_btk_file", _parse_failing_for_ep11)
        frames = nhc.fetch_btk_frames("east_pacific", [2026], get=getter)
        assert [f["id"].iloc[0] for f in frames] == ["EP122026"]


# Verbatim lines from the real NHC operational best track for East Pacific
# storm EP12 (Lowell), fetched from
# https://ftp.nhc.noaa.gov/atcf/btk/bep122026.dat on 2026-11-16: the genesis
# fix plus the 140 kt peak fix's three threshold rows. Real ATCF b-deck rows
# carry ~37 fields (far more than the synthetic fixtures above), so this pins
# the parser's hard-coded field indices against the actual format (M1).
_REAL_BTK_SAMPLE = 'EP, 12, 2026082518,   , BEST,   0, 101N, 1367W,  15,    0, DB,   0,    ,    0,    0,    0,    0,    0,    0,   0,   0,   0,   E,   0,    ,   0,   0, GENESIS021,  ,  0,    ,    0,    0,    0,    0, genesis-num, 021, \nEP, 12, 2026090218,   , BEST,   0, 133N, 1537W, 140,  927, HU,  34, NEQ,   80,   60,   60,   80, 1010,  240,  10, 170,   0,   E,   0,    ,   0,   0,     LOWELL, D, 12, NEQ,  180,  130,  115,  180, genesis-num, 021, \nEP, 12, 2026090218,   , BEST,   0, 133N, 1537W, 140,  927, HU,  50, NEQ,   40,   35,   30,   40, 1010,  240,  10, 170,   0,   E,   0,    ,   0,   0,     LOWELL, D, 12, NEQ,  180,  130,  115,  180, genesis-num, 021, \nEP, 12, 2026090218,   , BEST,   0, 133N, 1537W, 140,  927, HU,  64, NEQ,   30,   25,   20,   30, 1010,  240,  10, 170,   0,   E,   0,    ,   0,   0,     LOWELL, D, 12, NEQ,  180,  130,  115,  180, genesis-num, 021, '


class TestRealAtcfSample:
    """parse_btk_file decodes a verbatim real NHC b-deck snippet (M1)."""

    def test_decodes_real_fields(self):
        """Hand-read lat/lon/vmax/mslp/radii/name match the decoded record."""
        frame = nhc.parse_btk_file(
            _REAL_BTK_SAMPLE, storm_id="EP122026", basin="east_pacific"
        )
        assert len(frame) == 2
        peak = frame.loc[frame["vmax"].idxmax()]
        assert peak["vmax"] == 140
        assert peak["mslp"] == 927
        assert round(float(peak["lat"]), 1) == 13.3
        assert round(float(peak["lon"]), 1) == -153.7
        assert peak["type"] == "HU"
        assert peak["name"] == "LOWELL"
        assert [peak["r34_ne"], peak["r34_se"], peak["r34_sw"], peak["r34_nw"]] == [
            80.0,
            60.0,
            60.0,
            80.0,
        ]
        assert [peak["r50_ne"], peak["r50_se"], peak["r50_sw"], peak["r50_nw"]] == [
            40.0,
            35.0,
            30.0,
            40.0,
        ]
        assert [peak["r64_ne"], peak["r64_se"], peak["r64_sw"], peak["r64_nw"]] == [
            30.0,
            25.0,
            20.0,
            30.0,
        ]
        genesis = frame.loc[frame["vmax"].idxmin()]
        assert round(float(genesis["lat"]), 1) == 10.1
        assert round(float(genesis["lon"]), 1) == -136.7
        assert genesis["type"] == "DB"
        assert genesis[["r34_ne", "r34_se", "r34_sw", "r34_nw"]].isna().all()


class TestInvestGuard:
    """Cyclone numbers >= 90 (INVEST / genesis areas) are not surfaced (L3)."""

    def test_invest_number_ignored(self):
        """A b<basin>90<yyyy> listing entry is neither fetched nor returned."""
        base = nhc.BTK_BASE_URL
        storm = _atcf_line(
            time="2026090218",
            lat="133N",
            lon="1537W",
            vmax=120,
            mslp=940,
            threshold=64,
            radii=(30, 25, 20, 30),
            name="LOWELL",
        )
        listing = "".join(
            f'<a href="{n}">{n}</a>\n' for n in ("bep902026.dat", "bep122026.dat")
        )
        getter = _FakeGetter(
            {
                base: _FakeResponse(listing),
                f"{base}bep902026.dat": _FakeResponse(storm),
                f"{base}bep122026.dat": _FakeResponse(storm),
            }
        )
        frames = nhc.fetch_btk_frames("east_pacific", [2026], get=getter)
        assert [f["id"].iloc[0] for f in frames] == ["EP122026"]
        assert not any("bep902026" in url for url in getter.calls)


class TestStormNameGate:
    """_is_storm_name accepts hyphenated names but not placeholders (L2)."""

    @pytest.mark.parametrize(
        "token, expected",
        [
            ("LOWELL", True),
            ("TWO-E", True),
            ("SIX-E", True),
            ("GENESIS021", False),
            ("INVEST", False),
            ("", False),
            ("   ", False),
        ],
    )
    def test_gate(self, token, expected):
        """Alphabetic (optionally hyphenated) non-placeholder tokens register.

        Args:
            token: The ATCF field-27 candidate.
            expected: Whether it should count as a storm name.
        """
        assert nhc._is_storm_name(token) is expected


class TestSubHourFix:
    """Two BEST rows sharing a synoptic hour but differing by minute (L1)."""

    def test_sub_hour_special_fix_kept_distinct(self):
        """A :15 landfall fix is not folded into the :00 synoptic fix."""
        synoptic = _atcf_line(
            time="2026090212",
            lat="150N",
            lon="1600W",
            vmax=130,
            mslp=935,
            threshold=34,
            radii=(70, 50, 50, 70),
            name="LOWELL",
        )
        landfall = _atcf_line(
            time="2026090212",
            minute="15",
            lat="151N",
            lon="1602W",
            vmax=140,
            mslp=927,
            threshold=34,
            radii=(80, 60, 60, 80),
            name="LOWELL",
        )
        frame = nhc.parse_btk_file(
            chr(10).join([synoptic, landfall]),
            storm_id="EP122026",
            basin="east_pacific",
        )
        assert len(frame) == 2
        assert sorted(frame["vmax"].tolist()) == [130, 140]
