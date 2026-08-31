"""Tests for the ERDDAP catalog-tooling handlers (`earthlens.erddap.cli`).

Moved out of core's CLI test suite when the ERDDAP refresh/coverage/emit/validate
handlers moved into this distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import earthlens.erddap.cli as erddap_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.refresh import _flatten, refresh_one, supported_providers
from earthlens.cli.stanza import emit_stanza

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the erddap backend."""
    return next(b for b in list_backends() if b.provider == "erddap")


class TestRefresher:
    """Tests for the ERDDAP `allDatasets` crawler."""

    @staticmethod
    def _all_datasets(*ids):
        """A faked ERDDAP allDatasets.json body (with the meta-row first)."""
        rows = [["allDatasets"]] + [[i] for i in ids]
        return {"table": {"columnNames": ["datasetID"], "rows": rows}}

    def test_dataset_ids_drops_meta_row_and_dedupes(self, monkeypatch):
        """The synthetic `allDatasets` row is excluded; ids are sorted + unique."""
        monkeypatch.setattr(
            erddap_cli, "get_json", lambda url, **kw: self._all_datasets("b", "a", "a")
        )
        assert erddap_cli._dataset_ids("https://x/erddap/") == ["a", "b"]

    def test_grouped_queries_each_curated_server_once(self, monkeypatch):
        """Each distinct curated server_url is hit once at its allDatasets table."""
        calls = []

        def fake(url, **kw):
            calls.append(url)
            return self._all_datasets("NOAA_DHW", "cwwcNDBCMet")

        monkeypatch.setattr(erddap_cli, "get_json", fake)
        grouped = erddap_cli.refresher(load_catalog(_info()))
        assert len(grouped) == 2, (
            "the shipped catalog references two servers (coastwatch + uhslc)"
        )
        assert all(u.endswith("/tabledap/allDatasets.json?datasetID") for u in calls), (
            f"unexpected endpoint(s): {calls}"
        )
        assert _flatten(grouped) == ["NOAA_DHW", "cwwcNDBCMet"]

    def test_erddap_is_supported(self):
        """erddap is a registered live refresher (via discovery)."""
        assert "erddap" in supported_providers()

    def test_unreachable_server_aborts_without_partial_write(self, monkeypatch):
        """A server fetch failure surfaces as `error` and writes nothing."""

        def boom(url, **kw):
            raise RuntimeError("503 server down")

        monkeypatch.setattr(erddap_cli, "get_json", boom)
        outcome = refresh_one(_info(), write=True)
        assert outcome.status == "error", "an unreachable server aborts the crawl"
        assert outcome.written == "", "nothing is written on a failed crawl"


_TABLEDAP_DDS = """Dataset {
  Sequence {
    Float64 time;
    Float32 ATMP;
    Float32 WTMP;
    String station;
  } s;
} s;
"""

_GRIDDAP_DDS = """Dataset {
  Float64 time[time = 10];
  Float32 latitude[latitude = 20];
  Float32 longitude[longitude = 30];
  GRID {
    ARRAY:
      Float32 chlorophyll[time = 10][latitude = 20][longitude = 30];
    MAPS:
      Float64 time[time = 10];
  } chlorophyll;
} erdX;
"""


class TestVariablesFor:
    """Tests for the ERDDAP variable-lister used by the catalog audit."""

    def test_tabledap_dds_yields_served_columns(self, monkeypatch):
        """A tabledap `.dds` yields its served columns with casing preserved."""
        calls: list[str] = []
        monkeypatch.setattr(
            erddap_cli,
            "get_text",
            lambda url: calls.append(url) or _TABLEDAP_DDS,
        )
        record = SimpleNamespace(
            server_url="https://x/erddap/",
            protocol="tabledap",
            dataset_id="cwwcNDBCMet",
        )
        served = erddap_cli.variables_for(record)
        assert "WTMP" in served and "wtmp" not in served, "server casing preserved"
        assert calls == ["https://x/erddap/tabledap/cwwcNDBCMet.dds"]

    def test_griddap_dds_yields_grid_variable_and_dimensions(self, monkeypatch):
        """A griddap `.dds` yields the grid variable plus its dimensions."""
        monkeypatch.setattr(erddap_cli, "get_text", lambda url: _GRIDDAP_DDS)
        record = SimpleNamespace(
            server_url="https://x/erddap", protocol="griddap", dataset_id="erdX"
        )
        served = erddap_cli.variables_for(record)
        assert "chlorophyll" in served
        assert {"time", "latitude", "longitude"} <= served

    def test_non_dds_body_raises(self, monkeypatch):
        """A 200 non-DDS body raises, not parses to an empty (mass-drift) set."""
        monkeypatch.setattr(
            erddap_cli, "get_text", lambda url: "<html>under maintenance</html>"
        )
        record = SimpleNamespace(
            server_url="https://x/erddap", protocol="tabledap", dataset_id="cwwcNDBCMet"
        )
        with pytest.raises(ValueError, match="did not return a DDS"):
            erddap_cli.variables_for(record)


class TestCoverage:
    """Tests for the ERDDAP `audit --coverage` classifier."""

    def test_classify_each_bucket(self):
        """Each (structure, curation) combination maps to the right bucket."""
        curated = {"NOAA_DHW"}
        assert erddap_cli._classify("NOAA_DHW", "grid", curated) == "DONE"
        assert erddap_cli._classify("testGridWav", "grid", curated) == "thin"
        assert erddap_cli._classify("someGrid", "grid", curated) == "addressable"
        assert erddap_cli._classify("someTable", "table", curated) == "table"
        assert erddap_cli._classify("vanished", None, curated) == "missing"

    def test_coverage_counts_and_todo(self, monkeypatch):
        """Coverage buckets the available index and lists addressable griddap."""
        from earthlens.erddap.catalog import Dataset

        row = Dataset(
            server_url="https://x/erddap",
            dataset_id="NOAA_DHW",
            protocol="griddap",
            variables=["v"],
        )
        catalog = SimpleNamespace(
            available_datasets=["NOAA_DHW", "g1", "t1", "testX"],
            datasets={"NOAA_DHW": row},
        )
        monkeypatch.setattr(
            erddap_cli,
            "get_json",
            lambda url, **kw: {
                "table": {
                    "columnNames": ["datasetID", "dataStructure"],
                    "rows": [
                        ["allDatasets", "table"],
                        ["NOAA_DHW", "grid"],
                        ["g1", "grid"],
                        ["t1", "table"],
                        ["testX", "grid"],
                    ],
                }
            },
        )
        counts, todo = erddap_cli.coverage(catalog)
        assert counts == {
            "DONE": 1,
            "addressable": 1,
            "thin": 1,
            "table": 1,
            "missing": 0,
        }
        assert todo == ["g1"]

    def test_coverage_empty_index_raises(self):
        """An empty available index asks the user to refresh first."""
        catalog = SimpleNamespace(available_datasets=[], datasets={})
        with pytest.raises(ValueError, match="refresh erddap"):
            erddap_cli.coverage(catalog)


class TestEmitter:
    """Tests for the ERDDAP emitter (seeds from `/info`, network mocked)."""

    @staticmethod
    def _info_table(*, dims, variables, title="T", license_="L"):
        """A faked ERDDAP `/info` table — dimension + variable + NC_GLOBAL rows."""
        rows = [["dimension", d, "", "double", ""] for d in dims]
        rows += [["variable", v, "", "double", ""] for v in variables]
        rows += [
            ["attribute", "NC_GLOBAL", "title", "String", title],
            ["attribute", "NC_GLOBAL", "license", "String", license_],
        ]
        return {"table": {"rows": rows}}

    def test_seeds_griddap_row_from_info(self, monkeypatch):
        """A dimensioned dataset seeds a full griddap row with dim_names."""
        monkeypatch.setattr(
            erddap_cli,
            "get_json",
            lambda url, **kw: self._info_table(
                dims=["time", "latitude", "longitude"],
                variables=["sst", "sst_mask"],
                title="My SST",
            ),
        )
        result = emit_stanza(_info(), "myGrid", server="https://x/erddap")
        assert result.status == "ok"
        assert result.row == {
            "server_url": "https://x/erddap",
            "dataset_id": "myGrid",
            "protocol": "griddap",
            "dim_names": ["time", "latitude", "longitude"],
            "variables": ["sst", "sst_mask"],
            "title": "My SST",
            "license_note": "L",
        }

    def test_seeds_tabledap_row_without_dim_names(self, monkeypatch):
        """A dimensionless dataset seeds a tabledap row (no dim_names key)."""
        monkeypatch.setattr(
            erddap_cli,
            "get_json",
            lambda url, **kw: self._info_table(dims=[], variables=["station", "WTMP"]),
        )
        result = emit_stanza(_info(), "myTable", server="https://x/erddap")
        assert result.status == "ok"
        assert result.row["protocol"] == "tabledap"
        assert "dim_names" not in result.row
        assert result.row["variables"] == ["station", "WTMP"]

    def test_defaults_to_curated_servers(self, monkeypatch):
        """With no --server, the id is looked up on the catalog's curated servers."""
        calls = []

        def fake(url, **kw):
            calls.append(url)
            return self._info_table(dims=["time"], variables=["v"])

        monkeypatch.setattr(erddap_cli, "get_json", fake)
        result = emit_stanza(_info(), "myGrid")
        assert result.status == "ok"
        assert urlparse(result.row["server_url"]).hostname == "coastwatch.pfeg.noaa.gov"
        assert any("/info/myGrid/index.json" in u for u in calls)

    def test_not_found_reports_error(self, monkeypatch):
        """An id absent from every candidate server is a clear error."""

        def boom(url, **kw):
            raise RuntimeError("404 Not Found")

        monkeypatch.setattr(erddap_cli, "get_json", boom)
        result = emit_stanza(_info(), "ghost", server="https://x/erddap")
        assert result.status == "error"
        assert "not found" in result.detail.lower()


class TestValidator:
    """The ERDDAP offline lint flags the cross-row problems the model can't."""

    @staticmethod
    def _catalog(**row_overrides):
        """A one-row fake catalog whose row carries the given overrides."""
        from earthlens.erddap.catalog import Dataset

        fields = dict(
            server_url="https://example.org/erddap",
            dataset_id="d",
            protocol="tabledap",
            variables=["a"],
        )
        fields.update(row_overrides)
        return SimpleNamespace(datasets={"d": Dataset(**fields)})

    def test_clean_row_has_no_issues(self):
        """A well-formed row lints clean and is counted."""
        checked, issues = erddap_cli.validator(self._catalog())
        assert checked == 1, f"expected 1 row checked, got {checked}"
        assert issues == [], f"clean row should have no issues, got {issues}"

    def test_non_http_server_url_flagged(self):
        """A server_url that is not http(s) is flagged."""
        _, issues = erddap_cli.validator(self._catalog(server_url="ftp://x/erddap"))
        assert any("http(s)" in i for i in issues), f"server_url not flagged: {issues}"

    def test_empty_griddap_dim_names_flagged(self):
        """A griddap row with empty dim_names is flagged."""
        _, issues = erddap_cli.validator(
            self._catalog(protocol="griddap", dim_names=[])
        )
        assert any("dim_names" in i for i in issues), f"dim_names not flagged: {issues}"

    def test_flux_variable_not_in_variables_flagged(self):
        """A flux_variables entry absent from the row's variables is flagged."""
        _, issues = erddap_cli.validator(
            self._catalog(protocol="griddap", variables=["a"], flux_variables=["b"])
        )
        assert any("flux_variables" in i for i in issues), (
            f"flux typo not flagged: {issues}"
        )
