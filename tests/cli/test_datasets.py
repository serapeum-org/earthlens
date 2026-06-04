"""Command-level tests for the `earthlens datasets …` group."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from earthlens.cli import refresh as refresh_mod
from earthlens.cli.app import app
from earthlens.cli.table import build_table

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
        result = runner.invoke(app, ["datasets", "refresh", "chc"])
        assert result.exit_code == 0, f"refresh failed: {result.output}"
        assert "unsupported" in result.output, "chc reported unsupported"

    def test_unknown_provider_rejected(self):
        """An unknown selector token is a usage error."""
        result = runner.invoke(app, ["datasets", "refresh", "bogus"])
        assert result.exit_code == 2, "unknown provider -> exit 2"

    def test_all_covers_every_backend(self, monkeypatch):
        """'all' refreshes every backend (stac live-mocked, rest unsupported)."""
        monkeypatch.setattr(
            refresh_mod, "_get_json", lambda url: {"collections": [], "links": []}
        )
        result = runner.invoke(app, ["datasets", "refresh", "all", "--json"])
        payload = json.loads(result.output)
        assert len(payload) == 22, "one outcome per backend"
        assert any(o["provider"] == "stac" for o in payload), "stac included"

    def test_stac_json_reports_new_ids(self, monkeypatch):
        """A live id absent from the bundle shows up as new (mocked)."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url: {"collections": [{"id": "new-z"}], "links": []},
        )
        result = runner.invoke(app, ["datasets", "refresh", "stac", "--json"])
        payload = json.loads(result.output)
        assert payload[0]["provider"] == "stac" and payload[0]["status"] == "ok"
        assert "new-z" in payload[0]["new_ids"], "the new id surfaces"

    def test_show_ids_lists_new_ids(self, monkeypatch):
        """--show-ids prints each new upstream id under the table."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url: {"collections": [{"id": "brand-new-collection"}], "links": []},
        )
        result = runner.invoke(app, ["datasets", "refresh", "stac", "--show-ids"])
        assert "new upstream ids" in result.output, "section header shown"
        assert "brand-new-collection" in result.output, "the id is listed"


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
