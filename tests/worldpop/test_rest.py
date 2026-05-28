"""Tests for the WorldPop REST query helpers (faked HTTP, no network)."""

from __future__ import annotations

import pytest
import requests

from earthlens.worldpop.rest import (
    BASE_URL,
    files_for_year,
    record_citation,
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
    assert files == ["https://data.worldpop.org/GIS/Population/Global_2000_2020/2010/KEN/ken_ppp_2010.tif"]


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
