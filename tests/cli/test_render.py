"""Unit tests for `earthlens.cli.render`."""

from __future__ import annotations

import json

import pytest
from rich.console import Console

from earthlens.cli.render import (
    COMPACT_COLUMNS,
    FULL_COLUMNS,
    counts_table,
    print_load_warnings,
    row_to_dict,
    rows_table,
    rows_to_ids,
    rows_to_json,
)
from earthlens.cli.table import CatalogRow, LoadError

pytestmark = pytest.mark.cli


def _row(provider="s3", dataset_id="era5", title="ERA5", cadence="monthly"):
    """Build a CatalogRow for render tests."""
    return CatalogRow(provider, dataset_id, title, cadence, "0.25", "open")


class TestRowToDict:
    """Tests for row_to_dict."""

    def test_projects_six_columns(self):
        """The dict carries the six string columns and not the record."""
        data = row_to_dict(_row())
        assert set(data) == {
            "provider",
            "dataset_id",
            "title",
            "cadence",
            "resolution",
            "license",
        }
        assert "record" not in data, "the pydantic record is dropped"


class TestRowsToJson:
    """Tests for rows_to_json."""

    def test_roundtrips_to_objects(self):
        """The JSON parses back to one object per row."""
        parsed = json.loads(rows_to_json([_row(), _row("ecmwf", "era5-land")]))
        assert len(parsed) == 2, "one object per row"
        assert parsed[0]["provider"] == "s3", "fields preserved"

    def test_empty_rows_give_empty_array(self):
        """No rows serialise to an empty JSON array."""
        assert rows_to_json([]) == "[]", "empty input -> []"


class TestRowsToIds:
    """Tests for rows_to_ids."""

    def test_tab_separated_pairs(self):
        """Each row renders as `provider<TAB>dataset_id`."""
        assert rows_to_ids([_row("ecmwf", "era5")]) == "ecmwf\tera5"

    def test_empty_rows_give_empty_string(self):
        """No rows render to the empty string."""
        assert rows_to_ids([]) == "", "no rows -> ''"


class TestRowsTable:
    """Tests for rows_table."""

    def test_default_columns(self):
        """The default table shows provider / id / title."""
        table = rows_table([_row()])
        assert [c.header for c in table.columns] == ["PROVIDER", "DATASET ID", "TITLE"]
        assert table.row_count == 1, "one row added"

    def test_compact_columns(self):
        """COMPACT_COLUMNS drops the title."""
        table = rows_table([_row()], columns=COMPACT_COLUMNS)
        assert [c.header for c in table.columns] == ["PROVIDER", "DATASET ID"]

    def test_full_columns(self):
        """FULL_COLUMNS adds cadence and resolution."""
        table = rows_table([_row()], columns=FULL_COLUMNS)
        assert [c.header for c in table.columns] == [
            "PROVIDER",
            "DATASET ID",
            "TITLE",
            "CADENCE",
            "RESOLUTION",
        ]


class TestCountsTable:
    """Tests for counts_table."""

    def test_one_row_per_pair(self):
        """Each (facet, value) pair becomes one row."""
        table = counts_table({"provider": [("chc", 2), ("gee", 1)]})
        assert table.row_count == 2, "two value rows"
        assert [c.header for c in table.columns] == ["FACET", "VALUE", "COUNT"]


class TestPrintLoadWarnings:
    """Tests for print_load_warnings."""

    def test_prints_one_warning_per_error(self, capsys):
        """Each LoadError prints a stderr warning naming the provider."""
        console = Console(stderr=True, force_terminal=False)
        print_load_warnings([LoadError("gee", "no SDK")], console=console)
        captured = capsys.readouterr()
        assert "gee" in captured.err, "provider named in the warning"
        assert "no SDK" in captured.err, "reason included"

    def test_no_errors_prints_nothing(self, capsys):
        """An empty error list produces no output."""
        print_load_warnings([])
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == "", "silent when healthy"
