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
) -> str:
    """Build one comma-delimited ATCF best-track line with 28 fields."""
    fields = [""] * 28
    fields[0] = prefix
    fields[1] = number
    fields[2] = time
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
