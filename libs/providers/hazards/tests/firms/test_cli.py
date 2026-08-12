"""Tests for the FIRMS catalog-tooling handlers (`earthlens.firms.cli`).

Moved out of core's CLI test suite when the FIRMS refresh/probe/validate handlers
moved into this distribution (issue #863).
"""

from __future__ import annotations

import types

import pytest

import earthlens.firms.cli as firms_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the firms backend."""
    return next(b for b in list_backends() if b.provider == "firms")


class TestRefresher:
    """Tests for the FIRMS (data_availability) lister."""

    def test_lists_sensor_ids_excluding_burned_area(self, monkeypatch):
        """firms refresh parses data_id and drops the burned-area products."""
        monkeypatch.setattr(
            firms_cli,
            "get_text",
            lambda url: (
                "data_id,min_date,max_date\n"
                "VIIRS_SNPP_NRT,2020,2026\nBA_MODIS,2000,2026\nMODIS_NRT,2019,2026\n"
            ),
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "firms refresh ran"
        assert outcome.live_count == 2, "BA_MODIS excluded"

    def test_non_csv_body_is_error(self, monkeypatch):
        """A non-CSV body (bad key / quota) reports 'error', not raised."""
        monkeypatch.setattr(firms_cli, "get_text", lambda url: "Invalid MAP_KEY")
        assert refresh_one(_info()).status == "error", "bad body captured"

    def test_refresh_error_scrubs_key(self, monkeypatch):
        """A FIRMS HTTP error (URL holds the key) is reported with the key masked."""
        monkeypatch.setenv("FIRMS_MAP_KEY", "TOPSECRETKEY")

        def boom(url):
            raise RuntimeError(f"404 Client Error for url: {url}")

        monkeypatch.setattr(firms_cli, "get_text", boom)
        outcome = refresh_one(_info())
        assert outcome.status == "error", "error captured"
        assert "TOPSECRETKEY" not in outcome.detail, "map key scrubbed from detail"


class TestProber:
    """Tests for the FIRMS CSV-column prober."""

    def test_columns_and_inferred_dtypes(self, monkeypatch):
        """firms probe reads the CSV header and infers each column's dtype."""
        monkeypatch.setattr(
            firms_cli,
            "_csv_lines",
            lambda code: ["latitude,frp,satellite", "1.5,10,N"],
        )
        result = probe_dataset(_info(), "VIIRS_SNPP_NRT")
        assert result.status == "ok", "firms probe ran"
        assert result.assets["latitude"]["dtype"] == "float", "float inferred"
        assert result.assets["satellite"]["dtype"] == "str", "str inferred"

    def test_parses_header_and_first_row(self, monkeypatch):
        """The CSV header columns map to dtypes inferred from the first row."""
        monkeypatch.setattr(
            firms_cli,
            "_csv_lines",
            lambda code: ["latitude,confidence,sat", "1.5,90,N"],
        )
        dataset = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), dataset)
        assert result.assets["latitude"]["dtype"] == "float", "float column inferred"
        assert result.assets["confidence"]["dtype"] == "int", "int column inferred"

    def test_empty_csv_yields_empty_schema(self, monkeypatch):
        """An empty CSV sample yields an empty schema, not an error."""
        monkeypatch.setattr(firms_cli, "_csv_lines", lambda code: [])
        dataset = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), dataset)
        assert result.status == "ok" and result.assets == {}, "empty -> {}"

    def test_csv_lines_helper_fetches_with_key(self, monkeypatch):
        """_csv_lines requests the area CSV and splits the body into lines."""
        monkeypatch.setenv("FIRMS_MAP_KEY", "K")

        def fake_get(url, timeout=None):
            assert "/K/" in url, f"map key embedded in URL: {url}"
            return types.SimpleNamespace(text="a,b\n1,2", raise_for_status=lambda: None)

        monkeypatch.setattr(firms_cli.requests, "get", fake_get)
        assert firms_cli._csv_lines("VIIRS") == ["a,b", "1,2"]

    def test_csv_lines_error_scrubs_key(self, monkeypatch):
        """A failed FIRMS request raises with the map key masked out."""
        import requests as real_requests

        monkeypatch.setenv("FIRMS_MAP_KEY", "TOPSECRETKEY")

        def boom(url, timeout=None):
            raise real_requests.RequestException(f"500 Server Error for url: {url}")

        monkeypatch.setattr(firms_cli.requests, "get", boom)
        with pytest.raises(RuntimeError) as exc:
            firms_cli._csv_lines("VIIRS_SNPP_NRT")
        assert "TOPSECRETKEY" not in str(exc.value), "map key scrubbed from the error"
        assert "***" in str(exc.value), "key replaced with a mask"
