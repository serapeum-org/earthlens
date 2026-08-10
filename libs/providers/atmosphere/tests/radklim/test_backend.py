"""Unit tests for the RADKLIM / RADOLAN backend (no network)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from earthlens.base import SpatialExtent
from earthlens.radklim import GERMANY_ENVELOPE, RADKLIM
from earthlens.radklim.backend import _inclusive_end, _is_missing, _period_start_year

from .conftest import FakeHttp, _MissingResp

pytestmark = [pytest.mark.radklim, pytest.mark.unit]

_GERMANY_LAT = [47.0, 55.0]
_GERMANY_LON = [6.0, 15.0]


def _make(tmp_path, dataset="radklim-yw", *, http=None, **kwargs):
    """Build a RADKLIM over Germany with a same-year window."""
    params = dict(
        start="2024-06-01",
        end="2024-06-02",
        variables={dataset: []},
        lat_lim=list(_GERMANY_LAT),
        lon_lim=list(_GERMANY_LON),
        path=str(tmp_path),
        client=http if http is not None else FakeHttp(),
    )
    params.update(kwargs)
    return RADKLIM(**params)


class TestConstruction:
    """Tests for RADKLIM.__init__ and the hooks."""

    def test_output_kind_raster(self, tmp_path):
        """RADKLIM declares raster output."""
        assert _make(tmp_path).OUTPUT_KIND == "raster"

    def test_empty_variables_raises(self, tmp_path):
        """An empty product selection is rejected."""
        with pytest.raises(ValueError, match="non-empty product selection"):
            _make(tmp_path, variables={})

    def test_unknown_product_raises(self, tmp_path):
        """An unknown dataset key is rejected by the catalog."""
        with pytest.raises(ValueError, match="not in the RADKLIM catalog"):
            _make(tmp_path, variables={"nope": []})

    def test_list_form_variables(self, tmp_path):
        """A bare list of product keys is accepted like a dict."""
        b = RADKLIM(
            start="2024-06-01",
            end="2024-06-02",
            variables=["radklim-yw"],
            lat_lim=list(_GERMANY_LAT),
            lon_lim=list(_GERMANY_LON),
            path=str(tmp_path),
            client=FakeHttp(),
        )
        assert [p.product for p in b._products] == ["radklim-yw"]

    def test_create_grid_returns_extent(self, tmp_path):
        """_create_grid wraps the bbox in a SpatialExtent."""
        assert isinstance(_make(tmp_path).space, SpatialExtent)

    def test_bbox_outside_germany_raises(self, tmp_path):
        """A bbox that cannot overlap Germany is rejected."""
        with pytest.raises(ValueError, match="does not overlap Germany"):
            _make(tmp_path, lat_lim=[10, 20], lon_lim=[100, 110])

    def test_whole_earth_bbox_allowed(self, tmp_path):
        """A whole-Earth bbox overlaps Germany and is accepted."""
        b = _make(tmp_path, lat_lim=[-90, 90], lon_lim=[-180, 180])
        assert isinstance(b.space, SpatialExtent)

    def test_invalid_data_format_raises(self, tmp_path):
        """A format the product is not served in is rejected at construction."""
        with pytest.raises(ValueError, match="not available for 'radklim-yw'"):
            _make(tmp_path, data_format="hdf5")


class TestFormatFor:
    """Tests for _format_for."""

    def test_default_format(self, tmp_path):
        """Without an override, the product's default format is used."""
        b = _make(tmp_path, "radolan-yw", http=FakeHttp())
        assert b._format_for(b._products[0]) == "hdf5"

    def test_override_to_binary(self, tmp_path):
        """The operational binary format is a valid override."""
        b = _make(tmp_path, "radolan-yw", data_format="bin", http=FakeHttp())
        assert b._format_for(b._products[0]) == "bin"


class TestSearchReproc:
    """Tests for the reproc (yearly archive) enumeration."""

    def test_one_archive_per_year_in_window(self, tmp_path):
        """A window spanning a year boundary enumerates both yearly archives."""
        b = _make(tmp_path, start="2023-12-30", end="2024-01-02")
        names = [p.href.rsplit("/", 1)[-1] for p in b._search()]
        assert names == [
            "YW2017.002_2023_netcdf.tar.gz",
            "YW2017.002_2024_netcdf.tar.gz",
        ], names

    def test_clamps_to_archive_first_year(self, tmp_path):
        """A window starting before 2001 is clamped to the archive's first year."""
        b = _make(tmp_path, start="1998-01-01", end="2002-01-01")
        years = [p.metadata["year"] for p in b._search()]
        assert years == [2001, 2002], years

    def test_metadata_carries_format(self, tmp_path):
        """Each reproc product carries its format in metadata."""
        p = _make(tmp_path)._search()[0]
        assert p.metadata["format"] == "nc"

    def test_clamps_end_to_current_year(self, tmp_path):
        """A window ending in the future is clamped to the current year."""
        b = _make(
            tmp_path,
            start="2024-01-01",
            end="2030-01-01",
            now=dt.datetime(2026, 6, 1),
        )
        years = [p.metadata["year"] for p in b._search()]
        assert years == [2024, 2025, 2026], years


class TestSearchOperational:
    """Tests for the operational (per-timestamp) enumeration."""

    def test_filters_listing_to_window(self, tmp_path, operational_listing):
        """Only the granules whose scan time falls in the window are kept."""
        http = FakeHttp(listing=operational_listing)
        b = _make(
            tmp_path,
            "radolan-yw",
            start="2024-01-01T12:00",
            end="2024-01-01T12:07",
            fmt="%Y-%m-%dT%H:%M",
            now=dt.datetime(2024, 1, 1, 13, 0),
            http=http,
        )
        names = [p.href.rsplit("/", 1)[-1] for p in b._search()]
        assert names == [
            "raa01-yw_10000-2401011200-dwd---bin.hdf5",
            "raa01-yw_10000-2401011205-dwd---bin.hdf5",
        ], names

    def test_binary_format_lists_bz2(self, tmp_path, operational_listing):
        """data_format='bin' enumerates the .bz2 granules."""
        http = FakeHttp(listing=operational_listing)
        b = _make(
            tmp_path,
            "radolan-yw",
            data_format="bin",
            start="2024-01-01T12:00",
            end="2024-01-01T12:07",
            fmt="%Y-%m-%dT%H:%M",
            now=dt.datetime(2024, 1, 1, 13, 0),
            http=http,
        )
        names = [p.href.rsplit("/", 1)[-1] for p in b._search()]
        assert names == ["raa01-yw_10000-2401011200-dwd---bin.bz2"], names

    def test_default_fmt_date_only_end_includes_end_day(
        self, tmp_path, operational_listing
    ):
        """A date-only end (default fmt) keeps the whole end day's granules."""
        http = FakeHttp(listing=operational_listing)
        b = _make(
            tmp_path,
            "radolan-yw",
            start="2024-01-01",
            end="2024-01-01",
            now=dt.datetime(2024, 1, 1, 13, 0),
            http=http,
        )
        names = [p.href.rsplit("/", 1)[-1] for p in b._search()]
        assert len(names) == 3, names

    def test_date_only_end_within_retention_not_expired(
        self, tmp_path, operational_listing
    ):
        """A still-retained date-only end day is not falsely reported as expired."""
        http = FakeHttp(listing=operational_listing)
        b = _make(
            tmp_path,
            "radolan-yw",
            start="2024-01-01",
            end="2024-01-01",
            now=dt.datetime(2024, 1, 2, 13, 0),
            http=http,
        )
        assert b._search(), "end day is within the ~2-day retention window"

    def test_future_window_returns_empty_without_fetch(
        self, tmp_path, operational_listing
    ):
        """A window that starts in the future returns [] and skips the listing fetch."""
        http = FakeHttp(listing=operational_listing)
        b = _make(
            tmp_path,
            "radolan-yw",
            start="2027-01-01",
            end="2027-01-02",
            now=dt.datetime(2026, 8, 10),
            http=http,
        )
        assert b._search() == []
        assert http.got == [], "no listing request for a future window"

    def test_tz_aware_now_is_normalised(self, tmp_path, operational_listing):
        """An injected tz-aware now is normalised to naive UTC (no compare error)."""
        http = FakeHttp(listing=operational_listing)
        b = _make(
            tmp_path,
            "radolan-yw",
            start="2024-01-01T12:00",
            end="2024-01-01T12:07",
            fmt="%Y-%m-%dT%H:%M",
            now=dt.datetime(2024, 1, 1, 13, 0, tzinfo=dt.timezone.utc),
            http=http,
        )
        assert b._current_time() == dt.datetime(2024, 1, 1, 13, 0)
        assert len(b._search()) == 2

    def test_retention_expired_returns_empty(self, tmp_path, operational_listing):
        """A window before the retention window returns nothing (with a warning)."""
        http = FakeHttp(listing=operational_listing)
        b = _make(
            tmp_path,
            "radolan-yw",
            start="2020-01-01",
            end="2020-01-02",
            now=dt.datetime(2024, 1, 1),
            http=http,
        )
        assert b._search() == []
        assert http.got == [], "no listing request when the window is out of retention"


class TestFetch:
    """Tests for the download / fetch path."""

    def test_download_returns_written_paths(self, tmp_path):
        """download() writes each reproc archive and returns the paths."""
        http = FakeHttp()
        b = _make(tmp_path, start="2023-01-01", end="2024-01-01", http=http)
        out = b.download(progress_bar=False)
        assert [p.name for p in out] == [
            "YW2017.002_2023_netcdf.tar.gz",
            "YW2017.002_2024_netcdf.tar.gz",
        ], out
        assert all(Path(p).exists() for p in out)

    def test_missing_granule_is_skipped(self, tmp_path):
        """A 404 granule is logged and skipped, not raised."""
        http = FakeHttp(missing=("YW2017.002_2024_netcdf.tar.gz",))
        b = _make(tmp_path, start="2023-01-01", end="2024-01-01", http=http)
        out = b.download(progress_bar=False)
        assert [p.name for p in out] == ["YW2017.002_2023_netcdf.tar.gz"], out

    def test_non_404_error_propagates(self, tmp_path):
        """A non-404 HTTP error during download is re-raised, not skipped."""
        import requests

        class _ServerError(FakeHttp):
            def download(self, url, dest, **kwargs):
                resp = type("R", (), {"status_code": 500})()
                raise requests.HTTPError(response=resp)

        b = _make(tmp_path, start="2024-01-01", end="2024-01-01", http=_ServerError())
        with pytest.raises(requests.HTTPError):
            b.download(progress_bar=False)

    def test_empty_search_returns_empty(self, tmp_path, operational_listing):
        """download() returns [] when the search finds nothing."""
        http = FakeHttp(listing=operational_listing)
        b = _make(
            tmp_path,
            "radolan-yw",
            start="2020-01-01",
            end="2020-01-02",
            now=dt.datetime(2024, 1, 1),
            http=http,
        )
        assert b.download(progress_bar=False) == []


class TestHelpers:
    """Tests for the module-level helpers."""

    @pytest.mark.parametrize(
        "period, expected",
        [("2001-01-01/", 2001), ("", None), ("na/", None)],
    )
    def test_period_start_year(self, period, expected):
        """_period_start_year reads the leading year, or None when absent."""
        assert _period_start_year(period) == expected

    @pytest.mark.parametrize(
        "end, expect_hour",
        [(dt.datetime(2024, 1, 1), 23), (dt.datetime(2024, 1, 1, 6, 30), 6)],
    )
    def test_inclusive_end(self, end, expect_hour):
        """A midnight end extends to end-of-day; a timed end is unchanged."""
        assert _inclusive_end(end).hour == expect_hour

    def test_is_missing_true_for_404(self):
        """_is_missing is True only for a 404 response."""
        import requests

        err = requests.HTTPError(response=_MissingResp())
        assert _is_missing(err) is True

    def test_is_missing_false_without_response(self):
        """_is_missing is False when the error carries no 404 response."""
        import requests

        assert _is_missing(requests.HTTPError()) is False
