"""Command-level tests for the `earthlens datasets …` group."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from earthlens.cli.app import app

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
