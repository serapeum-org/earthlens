"""Tests for the WorldPop REST query helpers (faked HTTP, no network)."""

from __future__ import annotations

import pytest
import requests

from earthlens.worldpop.rest import (
    BASE_URL,
    files_for_year,
    global_files_for_year,
    global_records,
    record_citation,
    record_files,
    rest_records,
)
from tests.worldpop.conftest import _FakeResponse, pop_records

pytestmark = pytest.mark.worldpop


def test_rest_records_hits_alias_subalias_iso3(monkeypatch):
    """rest_records GETs /rest/data/{alias}/{subalias}?iso3= and returns data."""
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _FakeResponse(json_data={"data": pop_records()})

    monkeypatch.setattr(requests, "get", fake_get)
    records = rest_records("pop", "wpgp", "KEN")
    assert seen["url"] == f"{BASE_URL}/pop/wpgp"
    assert seen["params"] == {"iso3": "KEN"}
    assert len(records) == 21


def test_rest_records_raises_on_http_error(monkeypatch):
    """A non-2xx response propagates as HTTPError."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse())
    with pytest.raises(requests.HTTPError):
        rest_records("pop", "wpgp", "KEN")


def test_files_for_year_matches_popyear():
    """files_for_year returns the record whose popyear matches."""
    files = files_for_year(pop_records(), 2010)
    assert files == [
        "https://data.worldpop.org/GIS/Population/Global_2000_2020/2010/KEN/ken_ppp_2010.tif"
    ]


def test_files_for_year_latest_when_none():
    """year=None selects the latest available popyear."""
    files = files_for_year(pop_records(), None)
    assert files[0].endswith("ken_ppp_2020.tif")


def test_files_for_year_missing_raises_with_available():
    """A missing year raises listing the available years."""
    with pytest.raises(ValueError, match=r"not available; have \[2000"):
        files_for_year(pop_records(), 1990)


def test_files_for_year_empty_records_raises():
    """An empty record list raises a clear error."""
    with pytest.raises(ValueError, match="no records"):
        files_for_year([], 2020)


def test_files_for_year_drops_non_geotiff():
    """Non-raster companion files (e.g. ASCII-XYZ zips) are filtered out."""
    records = [
        {
            "popyear": "2020",
            "files": [
                "https://x/bdi_ppp_2020_1km_ASCII_XYZ.zip",
                "https://x/bdi_ppp_2020_1km_Aggregated.tif",
            ],
        }
    ]
    assert files_for_year(records, 2020) == [
        "https://x/bdi_ppp_2020_1km_Aggregated.tif"
    ]


def test_files_for_year_no_geotiff_raises():
    """A record with only non-raster files raises a clear error."""
    records = [{"popyear": "2020", "files": ["https://x/readme.zip"]}]
    with pytest.raises(ValueError, match="no GeoTIFF"):
        files_for_year(records, 2020)


def _fake_global(monkeypatch, summary, detail_files):
    """Patch requests.get to serve a global listing + a `?id=` detail record."""

    def fake_get(url, params=None, timeout=None):
        if params and "id" in params:
            return _FakeResponse(json_data={"data": {"id": params["id"], "files": detail_files}})
        return _FakeResponse(json_data={"data": summary})

    monkeypatch.setattr(requests, "get", fake_get)


def test_global_records_lists_summary(monkeypatch):
    """global_records returns the per-year summary list (no iso3)."""
    summary = [{"id": "1", "popyear": "2000"}, {"id": "2", "popyear": "2001"}]
    _fake_global(monkeypatch, summary, ["https://x/ppp_2000_1km_Aggregated.tif"])
    assert len(global_records("pop", "wpgp1km")) == 2


def test_record_files_filters_tif(monkeypatch):
    """record_files resolves the ?id= detail and keeps only GeoTIFFs."""
    _fake_global(
        monkeypatch,
        [{"id": "1", "popyear": "2000"}],
        ["https://x/a.zip", "https://x/ppp_2000_1km_Aggregated.tif"],
    )
    assert record_files("pop", "wpgp1km", "1") == [
        "https://x/ppp_2000_1km_Aggregated.tif"
    ]


def test_global_files_for_year_matches_popyear(monkeypatch):
    """global_files_for_year picks the year's record then resolves its files."""
    summary = [{"id": "1", "popyear": "2000"}, {"id": "2", "popyear": "2001"}]
    _fake_global(monkeypatch, summary, ["https://x/ppp_2000_1km_Aggregated.tif"])
    files = global_files_for_year("pop", "wpgp1km", 2000)
    assert files == ["https://x/ppp_2000_1km_Aggregated.tif"]


def test_global_files_for_year_missing_raises(monkeypatch):
    """An unavailable global year raises listing the available years."""
    _fake_global(monkeypatch, [{"id": "1", "popyear": "2000"}], ["https://x/m.tif"])
    with pytest.raises(ValueError, match="is not available"):
        global_files_for_year("pop", "wpgp1km", 1990)


def test_global_files_for_year_archive_only_raises(monkeypatch):
    """A record whose files are archives (no GeoTIFF) raises a clear error."""
    _fake_global(monkeypatch, [{"id": "1", "popyear": "2000"}], ["https://x/proj.zip"])
    with pytest.raises(ValueError, match="no GeoTIFF"):
        global_files_for_year("pop", "wpgp1km", 2000)


def test_record_citation_returns_first():
    """record_citation returns the first record's citation text."""
    assert record_citation(pop_records()).startswith("WorldPop")


def test_record_citation_none_when_absent():
    """record_citation returns None when no record carries a citation."""
    assert record_citation([{"popyear": "2020", "files": []}]) is None


def test_rest_records_uses_session_when_given(monkeypatch):
    """rest_records uses a passed Session's get when provided."""
    calls = {"n": 0}

    class _Session:
        def get(self, url, params=None, timeout=None):
            calls["n"] += 1
            return _FakeResponse(json_data={"data": pop_records()})

    rest_records("pop", "wpgp", "KEN", session=_Session())
    assert calls["n"] == 1


def test_files_for_year_undated_record():
    """An undated record (covariate) returns its files regardless of year."""
    records = [{"files": ["https://x/ken_viirs_100m_2012.tif"]}]  # no popyear
    assert files_for_year(records, 2099) == ["https://x/ken_viirs_100m_2012.tif"]
