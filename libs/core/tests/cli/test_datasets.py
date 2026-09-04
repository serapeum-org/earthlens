"""Command-level tests for the `earthlens datasets …` group."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import earthlens.stac.cli as stac_cli
from earthlens.cli import curate as curate_mod
from earthlens.cli import datasets as datasets_mod
from earthlens.cli import refresh as refresh_mod
from earthlens.cli.app import app
from earthlens.cli.refresh import CoverageOutcome
from earthlens.cli.table import build_table
from earthlens.gee import _hydrate as hydrate_mod

pytestmark = pytest.mark.cli

runner = CliRunner()


class TestWhere:
    """Tests for `datasets where`."""

    def test_finds_dataset(self):
        """A known dataset is found and exits zero."""
        result = runner.invoke(app, ["datasets", "where", "era5", "-p", "s3"])
        assert result.exit_code == 0, f"where failed: {result.output}"
        assert "era5" in result.output, "the matched id is shown"

    def test_ids_only_output(self):
        """--ids-only emits the tab-separated provider/id pair."""
        result = runner.invoke(
            app, ["datasets", "where", "era5", "-p", "s3", "--ids-only"]
        )
        assert "s3\tera5" in result.output, "pipeable id line emitted"

    def test_json_output_is_valid(self):
        """--json emits a parseable array of the matches."""
        result = runner.invoke(app, ["datasets", "where", "era5", "-p", "s3", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["dataset_id"] == "era5", "json carries the match"

    def test_exact_narrows(self):
        """--exact keeps only the literal id match."""
        result = runner.invoke(
            app, ["datasets", "where", "era5", "-p", "s3", "--exact", "--ids-only"]
        )
        assert result.output.strip() == "s3\tera5", "only the exact id remains"

    def test_no_match_exits_nonzero(self):
        """A miss exits non-zero so pipelines can branch on it."""
        result = runner.invoke(
            app, ["datasets", "where", "definitely-not-real", "-p", "s3"]
        )
        assert result.exit_code == 1, "no match -> exit 1"

    def test_unknown_provider_rejected(self):
        """An unknown --provider is a usage error."""
        result = runner.invoke(app, ["datasets", "where", "x", "-p", "bogus"])
        assert result.exit_code == 2, "BadParameter -> exit 2"

    def test_did_you_mean_on_typo(self):
        """A near-miss (transposed) query exits non-zero with a suggestion."""
        result = runner.invoke(app, ["datasets", "where", "chrip-daily", "-p", "chc"])
        assert result.exit_code == 1, "typo still misses"
        assert "Did you mean" in result.output, "suggests a close dataset id"

    def test_did_you_mean_suggests_curated_only(self):
        """Under --include-available, suggestions come from curated ids only."""
        curated = {row.dataset_id for row in build_table(providers=["overture"]).rows}
        result = runner.invoke(
            app,
            ["datasets", "where", "buildingz", "-p", "overture", "--include-available"],
        )
        assert result.exit_code == 1, "typo still misses"
        assert "Did you mean" in result.output, "suggestion offered"
        suggested = result.output.split("Did you mean:", 1)[1].rstrip("?\n ")
        tokens = [tok.strip() for tok in suggested.split(",") if tok.strip()]
        assert tokens and all(tok in curated for tok in tokens), "only curated ids"

    def test_include_available_widens_the_search(self):
        """--include-available can surface ids absent from the curated set."""
        curated = runner.invoke(
            app, ["datasets", "where", "building", "-p", "overture", "--ids-only"]
        )
        widened = runner.invoke(
            app,
            [
                "datasets",
                "where",
                "building",
                "-p",
                "overture",
                "--include-available",
                "--ids-only",
            ],
        )
        widened_lines = [ln for ln in widened.output.splitlines() if ln.strip()]
        curated_lines = [ln for ln in curated.output.splitlines() if ln.strip()]
        assert len(widened_lines) >= len(curated_lines), "available widens results"

    def test_conflicting_output_modes_rejected(self):
        """--json and --ids-only together is a usage error."""
        result = runner.invoke(
            app,
            ["datasets", "where", "era5", "-p", "s3", "--json", "--ids-only"],
        )
        assert result.exit_code == 2, "conflicting modes -> exit 2"


class TestSearch:
    """Tests for `datasets search`."""

    def test_count_is_numeric(self):
        """--count prints just an integer."""
        result = runner.invoke(app, ["datasets", "search", "-p", "s3", "--count"])
        assert result.output.strip().isdigit(), f"not a count: {result.output!r}"

    def test_filter_narrows_count(self):
        """A facet filter reduces (or equals) the unfiltered count."""
        total = runner.invoke(app, ["datasets", "search", "-p", "s3", "--count"])
        filtered = runner.invoke(
            app,
            [
                "datasets",
                "search",
                "-p",
                "s3",
                "--filter",
                "cadence=monthly",
                "--count",
            ],
        )
        assert int(filtered.output) <= int(total.output), "filter cannot grow results"

    def test_facets_only_shows_distribution(self):
        """--facets-only prints the per-facet value table."""
        result = runner.invoke(app, ["datasets", "search", "-p", "s3", "--facets-only"])
        assert "FACET" in result.output, "facet distribution table shown"

    def test_bad_filter_rejected(self):
        """A malformed --filter is a usage error."""
        result = runner.invoke(
            app, ["datasets", "search", "-p", "s3", "--filter", "nope=1"]
        )
        assert result.exit_code == 2, "unknown facet -> exit 2"

    def test_limit_caps_rows(self):
        """--limit caps the JSON result length."""
        result = runner.invoke(
            app, ["datasets", "search", "-p", "s3", "-n", "1", "--json"]
        )
        assert len(json.loads(result.output)) <= 1, "limit respected"

    def test_count_json_emits_object(self):
        """--count --json emits a {"count": N} object, not a bare number."""
        result = runner.invoke(
            app, ["datasets", "search", "-p", "s3", "--count", "--json"]
        )
        payload = json.loads(result.output)
        assert set(payload) == {"count"} and payload["count"] > 0, "single count key"

    def test_facets_only_json_emits_per_facet(self):
        """--facets-only --json emits a {facet: [{value, count}]} object."""
        result = runner.invoke(
            app, ["datasets", "search", "-p", "s3", "--facets-only", "--json"]
        )
        payload = json.loads(result.output)
        assert [v["value"] for v in payload["provider"]] == ["s3"], "provider facet"
        assert payload["provider"][0]["count"] > 0, "count carried"

    def test_facets_only_table_still_default(self):
        """--facets-only without --json still prints the Rich table."""
        result = runner.invoke(app, ["datasets", "search", "-p", "s3", "--facets-only"])
        assert "FACET" in result.output and "{" not in result.output

    @pytest.mark.parametrize("mode", ["--count", "--facets-only"])
    def test_ids_only_rejected_with_terminal_modes(self, mode):
        """--ids-only is rejected alongside the scalar/aggregate output modes."""
        result = runner.invoke(
            app, ["datasets", "search", "-p", "s3", mode, "--ids-only"]
        )
        assert result.exit_code == 2, f"{mode} + --ids-only should be rejected"


class TestList:
    """Tests for `datasets list`."""

    def test_compact_default(self):
        """The default list shows provider and id columns."""
        result = runner.invoke(app, ["datasets", "list", "-p", "s3"])
        assert result.exit_code == 0, f"list failed: {result.output}"
        assert "DATASET ID" in result.output, "id column present"

    def test_full_adds_columns(self):
        """--full adds the cadence column."""
        result = runner.invoke(app, ["datasets", "list", "-p", "s3", "--full"])
        assert "CADENCE" in result.output, "full view adds cadence"

    def test_json_lists_every_dataset(self):
        """--json emits one object per dataset in the scoped provider."""
        result = runner.invoke(app, ["datasets", "list", "-p", "s3", "--json"])
        payload = json.loads(result.output)
        assert payload and all(r["provider"] == "s3" for r in payload), "all s3"


class TestRefresh:
    """Tests for `datasets refresh` (the one online command; network mocked)."""

    def test_unsupported_provider_exits_zero(self):
        """A provider with no live endpoint reports unsupported, exit 0."""
        result = runner.invoke(app, ["datasets", "refresh", "gdacs"])
        assert result.exit_code == 0, f"refresh failed: {result.output}"
        assert "unsupported" in result.output, "gdacs reported unsupported"

    def test_unknown_provider_rejected(self):
        """An unknown selector token is a usage error."""
        result = runner.invoke(app, ["datasets", "refresh", "bogus"])
        assert result.exit_code == 2, "unknown provider -> exit 2"

    def test_all_covers_every_backend(self, monkeypatch):
        """'all' refreshes every backend (stac stubbed, rest unsupported).

        Replaces the whole refresher registry with a single offline stac
        stub so `refresh all` stays fully offline — the other refreshers
        reach live HTTP / FTP / SDK sources `_get_json` alone can't mock.
        """
        monkeypatch.setattr(
            refresh_mod, "_REFRESHERS", {"stac": lambda catalog: {"stac": []}}
        )
        result = runner.invoke(app, ["datasets", "refresh", "all", "--json"])
        payload = json.loads(result.output)
        assert len(payload) == 61, "one outcome per backend"
        assert any(o["provider"] == "stac" for o in payload), "stac included"

    def test_stac_json_reports_new_ids(self, monkeypatch):
        """A live id absent from the bundle shows up as new (mocked)."""
        monkeypatch.setattr(
            stac_cli,
            "get_json",
            lambda url: {"collections": [{"id": "new-z"}], "links": []},
        )
        result = runner.invoke(app, ["datasets", "refresh", "stac", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["provider"] == "stac" and payload[0]["status"] == "ok"
        assert "new-z" in payload[0]["new_ids"], "the new id surfaces"

    def test_show_ids_lists_new_ids(self, monkeypatch):
        """--show-ids prints each new upstream id under the table."""
        monkeypatch.setattr(
            stac_cli,
            "get_json",
            lambda url: {"collections": [{"id": "brand-new-collection"}], "links": []},
        )
        result = runner.invoke(app, ["datasets", "refresh", "stac", "--show-ids"])
        assert "new upstream ids" in result.output, "section header shown"
        assert "brand-new-collection" in result.output, "the id is listed"

    def test_tiles_regenerates_ghsl_geojson(self, tmp_path, monkeypatch):
        """--tiles regenerates the GHSL tile artefact and reports it (mocked)."""
        import geopandas as gpd
        from shapely.geometry import box

        import earthlens.ghsl._helpers as ghsl_helpers
        from earthlens.cli import refresh as refresh_mod

        frame = gpd.GeoDataFrame(
            {
                "tile_id": ["R1_C1"],
                "left": [0],
                "top": [1],
                "right": [1],
                "bottom": [0],
                "geometry": [box(0, 0, 1, 1)],
            },
            crs="ESRI:54009",
        )
        monkeypatch.setattr(
            __import__("earthlens.ghsl.cli", fromlist=["_tile_frame"]),
            "_tile_frame",
            lambda: frame,
        )
        monkeypatch.setattr(
            ghsl_helpers, "TILE_SCHEMA_PATH", tmp_path / "tile_schema.geojson"
        )
        result = runner.invoke(
            app, ["datasets", "refresh", "ghsl", "--tiles", "--json"]
        )
        payload = json.loads(result.output)
        assert payload[0]["status"] == "ok", "ghsl tile regen ran (mocked)"
        assert payload[0]["tiles"] == 1, "one tile written"

    def test_tiles_unsupported_provider(self):
        """--tiles on a non-tile provider reports unsupported, not a crash."""
        result = runner.invoke(
            app, ["datasets", "refresh", "stac", "--tiles", "--json"]
        )
        payload = json.loads(result.output)
        assert payload[0]["status"] == "unsupported", "stac has no tile artefact"

    def test_write_reports_written_path(self, tmp_path, monkeypatch):
        """--write rewrites the (temp-redirected) catalog and reports the path."""
        import shutil

        import earthlens.stac.catalog as stac_catalog

        dst = tmp_path / "catalog"
        shutil.copytree(stac_catalog.CATALOG_PATH, dst)
        monkeypatch.setattr(stac_catalog, "CATALOG_PATH", dst)
        monkeypatch.setattr(
            stac_cli,
            "get_json",
            lambda url: {"collections": [{"id": "x"}], "links": []},
        )
        result = runner.invoke(app, ["datasets", "refresh", "stac", "--write"])
        assert result.exit_code == 0, f"refresh --write failed: {result.output}"
        assert "wrote" in result.output and "_index.yaml" in result.output

    def test_write_path_intact_on_narrow_terminal(self, tmp_path, monkeypatch):
        """The written path is emitted contiguously even on a narrow terminal.

        A width that cannot fit the path forces Rich to fold it unless the line
        opts out of wrapping; the full path must survive as one unbroken span.
        """
        import io
        import shutil

        from rich.console import Console

        import earthlens.stac.catalog as stac_catalog

        dst = tmp_path / "catalog"
        shutil.copytree(stac_catalog.CATALOG_PATH, dst)
        monkeypatch.setattr(stac_catalog, "CATALOG_PATH", dst)
        monkeypatch.setattr(
            stac_cli,
            "get_json",
            lambda url: {"collections": [{"id": "x"}], "links": []},
        )
        buf = io.StringIO()
        monkeypatch.setattr(
            datasets_mod, "out_console", lambda: Console(file=buf, width=40)
        )
        result = runner.invoke(app, ["datasets", "refresh", "stac", "--write"])
        assert result.exit_code == 0, f"refresh --write failed: {result.output}"
        written = str(dst / "_index.yaml")
        assert written in buf.getvalue(), "path was folded across lines by Rich"


class TestAudit:
    """Tests for `datasets audit` (curated-vs-live drift; network mocked)."""

    def test_unsupported_provider_exits_zero(self):
        """A provider with no live endpoint reports unsupported, exit 0."""
        result = runner.invoke(app, ["datasets", "audit", "gdacs"])
        assert result.exit_code == 0, f"audit failed: {result.output}"
        assert "unsupported" in result.output, "gdacs reported unsupported"

    def test_unknown_provider_rejected(self):
        """An unknown selector token is a usage error."""
        result = runner.invoke(app, ["datasets", "audit", "bogus"])
        assert result.exit_code == 2, "unknown provider -> exit 2"

    def test_json_reports_broken(self, monkeypatch):
        """--json carries the broken/untracked drift lists."""
        monkeypatch.setattr(
            stac_cli,
            "get_json",
            lambda url: {"collections": [{"id": "only-live"}], "links": []},
        )
        result = runner.invoke(app, ["datasets", "audit", "stac", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["status"] == "ok", "audit ran"
        assert payload[0]["broken"], "curated drift surfaced"

    def test_strict_exits_nonzero_on_drift(self, monkeypatch):
        """--strict exits 1 when a curated dataset is no longer served live."""
        monkeypatch.setattr(
            stac_cli, "get_json", lambda url: {"collections": [], "links": []}
        )
        result = runner.invoke(app, ["datasets", "audit", "stac", "--strict"])
        assert result.exit_code == 1, "drift under --strict -> exit 1"

    def test_strict_exits_nonzero_on_variable_drift(self, monkeypatch):
        """--strict exits 1 when a curated variable is no longer served (#1129)."""
        import earthlens.erddap.cli as erddap_cli
        from earthlens.erddap.catalog import Dataset

        record = Dataset(
            server_url="https://x/erddap",
            dataset_id="cwwcNDBCMet",
            protocol="tabledap",
            variables=["wtmp"],
        )
        catalog = SimpleNamespace(
            datasets={"cwwcNDBCMet": record}, available_datasets=["cwwcNDBCMet"]
        )
        monkeypatch.setattr(refresh_mod, "load_catalog", lambda info: catalog)
        monkeypatch.setitem(
            refresh_mod._REFRESHERS,
            "erddap",
            lambda cat: {"https://x/erddap": ["cwwcNDBCMet"]},
        )
        monkeypatch.setattr(
            erddap_cli,
            "get_text",
            lambda url: "Dataset {\n  Sequence {\n    Float32 WTMP;\n  } s;\n} s;\n",
        )
        result = runner.invoke(app, ["datasets", "audit", "erddap", "--strict"])
        assert result.exit_code == 1, f"variable drift -> exit 1: {result.output}"
        assert "variable drift" in result.output.lower(), "drift surfaced to the user"

    def test_variable_audit_error_is_surfaced_in_default_output(self, monkeypatch):
        """A variable-fetch error prints a human-visible line, not only via --json."""
        import earthlens.erddap.cli as erddap_cli
        from earthlens.erddap.catalog import Dataset

        record = Dataset(
            server_url="https://x/erddap",
            dataset_id="cwwcNDBCMet",
            protocol="tabledap",
            variables=["wtmp"],
        )
        catalog = SimpleNamespace(
            datasets={"cwwcNDBCMet": record}, available_datasets=["cwwcNDBCMet"]
        )
        monkeypatch.setattr(refresh_mod, "load_catalog", lambda info: catalog)
        monkeypatch.setitem(
            refresh_mod._REFRESHERS,
            "erddap",
            lambda cat: {"https://x/erddap": ["cwwcNDBCMet"]},
        )

        def _boom(url):
            raise RuntimeError("404 Not Found")

        monkeypatch.setattr(erddap_cli, "get_text", _boom)
        result = runner.invoke(app, ["datasets", "audit", "erddap"])
        assert result.exit_code == 0, (
            f"a variable error alone is not drift: {result.output}"
        )
        assert "variable audit errored in erddap" in result.output, (
            f"the variable-audit error is not surfaced: {result.output}"
        )
        assert "404 Not Found" in result.output, "the reason is not surfaced"

    def test_coverage_reports_buckets(self, monkeypatch):
        """--coverage prints the curation buckets + the addressable todo list."""
        monkeypatch.setattr(
            datasets_mod,
            "coverage_one",
            lambda info: CoverageOutcome(
                info.provider,
                "ok",
                counts={
                    "DONE": 5,
                    "addressable": 2,
                    "thin": 1,
                    "table": 0,
                    "missing": 0,
                },
                todo=["NEW/ONE", "NEW/TWO"],
            ),
        )
        result = runner.invoke(
            app, ["datasets", "audit", "gee", "--coverage", "--json"]
        )
        assert result.exit_code == 0, f"coverage failed: {result.output}"
        payload = json.loads(result.output)
        assert payload[0]["counts"]["addressable"] == 2 and payload[0]["todo"]


class TestCurate:
    """Tests for `datasets curate` (stanza-emit; network mocked)."""

    def test_usgs_water_emits_yaml(self):
        """A pure-args provider prints a paste-ready datasets: stanza."""
        result = runner.invoke(
            app,
            ["datasets", "curate", "usgs_water", "00060", "--key", "discharge"],
        )
        assert result.exit_code == 0, f"curate failed: {result.output}"
        assert "datasets:" in result.output and "code: '00060'" in result.output

    def test_json_output(self):
        """--json emits the seeded row object."""
        result = runner.invoke(
            app,
            [
                "datasets",
                "curate",
                "eumetsat",
                "EO:EUM:DAT:MSG:HRSEVIRI",
                "--group",
                "MSG",
                "--minimal",
                "--json",
            ],
        )
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["row"]["collection_id"]

    def test_write_appends_to_catalog(self, tmp_path, monkeypatch):
        """curate --write inserts the row into the (temp) catalog file."""
        import importlib
        import shutil

        info = next(
            b
            for b in __import__(
                "earthlens.cli.adapter", fromlist=["list_backends"]
            ).list_backends()
            if b.provider == "usgs_water"
        )
        module = importlib.import_module(f"{info.module}.catalog")
        dst = tmp_path / "usgs_water_data_catalog.yaml"
        shutil.copy(module.CATALOG_PATH, dst)
        monkeypatch.setattr(module, "CATALOG_PATH", dst)
        module.clear_catalog_cache()
        result = runner.invoke(
            app,
            [
                "datasets",
                "curate",
                "usgs_water",
                "98765",
                "--key",
                "cli_param",
                "--write",
            ],
        )
        assert result.exit_code == 0, f"curate --write failed: {result.output}"
        assert "wrote cli_param" in result.output and "cli_param" in dst.read_text()

    def test_unsupported_provider_exits_nonzero(self):
        """A provider with no emitter reports unsupported and exits 1."""
        result = runner.invoke(app, ["datasets", "curate", "chc", "anything"])
        assert result.exit_code == 1, "unsupported -> exit 1"
        assert "unsupported" in result.output

    def test_unknown_provider_rejected(self):
        """An unknown selector token is a usage error."""
        result = runner.invoke(app, ["datasets", "curate", "bogus", "x"])
        assert result.exit_code == 2, "unknown provider -> exit 2"

    def test_fill_empty_runs_bulk_hydrate(self, monkeypatch):
        """gee --fill-empty --write drives the bulk hydrate and reports a summary."""
        monkeypatch.setattr(
            hydrate_mod,
            "bulk_hydrate_empty",
            lambda limit=None: {
                "candidates": 3,
                "hydrated": 2,
                "skipped": 1,
                "filled": ["A", "B"],
            },
        )
        result = runner.invoke(
            app, ["datasets", "curate", "gee", "--fill-empty", "--write"]
        )
        assert result.exit_code == 0, f"fill-empty failed: {result.output}"
        assert "hydrated 2" in result.output and "/ 3" in result.output

    def test_fill_empty_requires_write(self):
        """--fill-empty without --write is a usage error (it mutates the catalog)."""
        result = runner.invoke(app, ["datasets", "curate", "gee", "--fill-empty"])
        assert result.exit_code == 2, "fill-empty without --write -> exit 2"

    def test_all_and_fill_empty_together_is_rejected(self):
        """--all + --fill-empty is a usage error (separate passes, not combinable)."""
        result = runner.invoke(
            app, ["datasets", "curate", "ecmwf", "--all", "--fill-empty", "--write"]
        )
        assert result.exit_code == 2, "--all + --fill-empty -> exit 2"

    def test_fill_empty_unsupported_provider(self):
        """--fill-empty on a provider that is neither gee nor ecmwf is rejected."""
        result = runner.invoke(
            app, ["datasets", "curate", "usgs_water", "--fill-empty", "--write"]
        )
        assert result.exit_code == 2, "fill-empty unsupported -> exit 2"

    def test_missing_upstream_id_rejected(self):
        """curate without an upstream id (and no --fill-empty) is a usage error."""
        result = runner.invoke(app, ["datasets", "curate", "usgs_water"])
        assert result.exit_code == 2, "missing id -> exit 2"

    def test_all_requires_write(self):
        """--all without --write is a usage error (it mutates the catalog)."""
        result = runner.invoke(app, ["datasets", "curate", "ecmwf", "--all"])
        assert result.exit_code == 2, "--all without --write -> exit 2"

    def test_all_ecmwf_only(self):
        """--all on a non-ecmwf provider is rejected."""
        result = runner.invoke(app, ["datasets", "curate", "gee", "--all", "--write"])
        assert result.exit_code == 2, "--all non-ecmwf -> exit 2"


class TestValidate:
    """Tests for `datasets validate` (per-entry checks)."""

    def test_nwp_validates_clean(self):
        """nwp's offline structural lint passes for the bundled catalog."""
        result = runner.invoke(app, ["datasets", "validate", "nwp", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["status"] == "ok", "nwp validated"
        assert payload[0]["issues"] == [], "no structural issues"

    def test_unsupported_provider(self):
        """A catalog-backed provider reports unsupported (uses refresh/audit)."""
        result = runner.invoke(app, ["datasets", "validate", "cmems"])
        assert "unsupported" in result.output, "cmems has no validator"

    def test_usgs_water_validates_clean(self):
        """The usgs_water offline validator passes for the bundled catalog."""
        result = runner.invoke(app, ["datasets", "validate", "usgs_water", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["status"] == "ok" and payload[0]["issues"] == []

    def test_nsi_validates_clean(self):
        """The nsi offline validator passes for the bundled catalog."""
        result = runner.invoke(app, ["datasets", "validate", "nsi", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["status"] == "ok", "nsi validated"
        assert payload[0]["checked"] == 3, "three nsi sources checked"

    def test_emdat_validates_clean(self):
        """The emdat offline validator passes for the bundled catalog."""
        result = runner.invoke(app, ["datasets", "validate", "emdat", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["status"] == "ok", "emdat validated"
        assert payload[0]["issues"] == [], "no structural issues"

    def test_live_flag_runs_live_validator(self, monkeypatch):
        """--live routes through the reachability validator (mocked)."""
        from earthlens.cli import validate as validate_mod

        monkeypatch.setattr(validate_mod, "_http_head", lambda url: 200)
        result = runner.invoke(app, ["datasets", "validate", "ghsl", "--live", "-j"])
        payload = json.loads(result.output)
        assert payload[0]["status"] == "ok", "ghsl live validate ran"

    def test_unknown_provider_rejected(self):
        """An unknown selector token is a usage error."""
        result = runner.invoke(app, ["datasets", "validate", "bogus"])
        assert result.exit_code == 2, "unknown provider -> exit 2"


class TestProbe:
    """Tests for `datasets probe` (curation seed; network mocked)."""

    _SAMPLE = {
        "features": [
            {
                "assets": {
                    "B04": {"type": "image/tiff", "eo:bands": [{"common_name": "red"}]}
                }
            }
        ]
    }

    def test_unsupported_provider_exits_nonzero(self):
        """A provider with no prober reports unsupported and exits 1."""
        result = runner.invoke(app, ["datasets", "probe", "gdacs", "whatever"])
        assert result.exit_code == 1, "unsupported -> exit 1"
        assert "unsupported" in result.output, "reason shown"

    def test_multiple_providers_rejected(self):
        """probe takes exactly one provider, not 'all'."""
        result = runner.invoke(app, ["datasets", "probe", "all", "x"])
        assert result.exit_code == 2, "'all' rejected for probe"

    def test_schema_json_with_mocked_sample(self, monkeypatch):
        """--json emits the parsed band/asset schema."""
        monkeypatch.setattr(stac_cli, "get_json", lambda url: self._SAMPLE)
        result = runner.invoke(
            app, ["datasets", "probe", "stac", "sentinel-2-l2a", "--json"]
        )
        payload = json.loads(result.output)
        assert payload["status"] == "ok", "probe succeeded"
        assert payload["assets"]["B04"]["common_name"] == "red", "schema parsed"

    def test_table_lists_assets(self, monkeypatch):
        """The default table lists each probed entry under the NAME column."""
        monkeypatch.setattr(stac_cli, "get_json", lambda url: self._SAMPLE)
        result = runner.invoke(app, ["datasets", "probe", "stac", "sentinel-2-l2a"])
        assert result.exit_code == 0, f"probe failed: {result.output}"
        assert "NAME" in result.output and "B04" in result.output


class TestShow:
    """Tests for `datasets show`."""

    def test_shows_record_fields(self):
        """The detail table includes backend-specific record fields."""
        result = runner.invoke(app, ["datasets", "show", "s3", "era5"])
        assert result.exit_code == 0, f"show failed: {result.output}"
        assert "bucket" in result.output, "s3 record field shown"

    def test_json_dumps_full_record(self):
        """--json carries provider, id and the record fields."""
        result = runner.invoke(app, ["datasets", "show", "s3", "era5", "--json"])
        payload = json.loads(result.output)
        assert payload["provider"] == "s3" and payload["dataset_id"] == "era5"
        assert "bucket" in payload, "record fields merged into the object"

    def test_missing_dataset_exits_nonzero(self):
        """An absent dataset id exits non-zero with a suggestion."""
        result = runner.invoke(app, ["datasets", "show", "s3", "era6"])
        assert result.exit_code == 1, "missing dataset -> exit 1"


class TestFacets:
    """Tests for `datasets facets`."""

    def test_summary_lists_facets(self):
        """With no --values, each facet and its distinct-value count is shown."""
        result = runner.invoke(app, ["datasets", "facets", "-p", "s3"])
        assert result.exit_code == 0, f"facets failed: {result.output}"
        assert "FACET" in result.output and "DISTINCT" in result.output

    def test_values_enumerates_counts(self):
        """--values shows the distinct values of the chosen facet."""
        result = runner.invoke(
            app, ["datasets", "facets", "--values", "provider", "-p", "s3", "--json"]
        )
        payload = json.loads(result.output)
        assert [p["value"] for p in payload] == ["s3"], "only the scoped provider"
        assert payload[0]["count"] > 0, "the scoped provider has datasets"

    def test_unknown_facet_rejected(self):
        """An unknown --values facet is a usage error."""
        result = runner.invoke(
            app, ["datasets", "facets", "--values", "bogus", "-p", "s3"]
        )
        assert result.exit_code == 2, "unknown facet -> exit 2"

    def test_summary_json(self):
        """The facet summary honours --json with a distinct-values object list."""
        result = runner.invoke(app, ["datasets", "facets", "-p", "s3", "--json"])
        payload = json.loads(result.output)
        assert any(item["facet"] == "provider" for item in payload), "facet listed"
        assert all("distinct_values" in item for item in payload), "counts carried"


class TestCommandBranches:
    """Coverage for shared command-branch edges (selectors + JSON/strict)."""

    def test_provider_split_empty_tokens_scans_all(self):
        """A -p value of only separators resolves to no restriction (scan all)."""
        result = runner.invoke(app, ["datasets", "where", "era5", "-p", ","])
        assert result.exit_code in (0, 1), "empty selector tolerated, not a usage error"

    def test_refresh_empty_selector_rejected(self):
        """A blank refresh selector is a usage error."""
        result = runner.invoke(app, ["datasets", "refresh", " "])
        assert result.exit_code == 2, "blank selector -> exit 2"

    def test_curate_requires_single_provider(self):
        """curate with a comma-list of providers is a usage error."""
        result = runner.invoke(app, ["datasets", "curate", "hdx,gee", "x"])
        assert result.exit_code == 2, "multiple providers -> exit 2"

    def test_validate_json_output(self):
        """validate honours --json with a per-provider result array."""
        result = runner.invoke(app, ["datasets", "validate", "nwp", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["provider"] == "nwp", "result carries the provider"

    def test_validate_surfaces_issues_and_strict_exits(self, monkeypatch):
        """validate prints each issue and --strict exits non-zero when any fail."""
        from earthlens.cli.validate import ValidateResult

        monkeypatch.setattr(
            datasets_mod,
            "validate_one",
            lambda info, live=False: ValidateResult(
                info.provider, "ok", checked=1, issues=["bad thing"]
            ),
        )
        shown = runner.invoke(app, ["datasets", "validate", "nwp"])
        assert "bad thing" in shown.output, "issue surfaced"
        strict = runner.invoke(app, ["datasets", "validate", "nwp", "--strict"])
        assert strict.exit_code == 1, "issues under --strict -> exit 1"
