"""Unit tests for `earthlens.cli.render`."""

from __future__ import annotations

import json

import pytest
from earthlens.cli.render import (
    COMPACT_COLUMNS,
    FULL_COLUMNS,
    _format_value,
    counts_table,
    coverage_table,
    print_load_warnings,
    record_json,
    record_table,
    row_to_dict,
    rows_table,
    rows_to_ids,
    rows_to_json,
)
from earthlens.cli.table import CatalogRow, LoadError
from rich.console import Console

pytestmark = pytest.mark.cli


def _row(provider="s3", dataset_id="era5", title="ERA5", cadence="monthly"):
    """Build a CatalogRow for render tests."""
    return CatalogRow(provider, dataset_id, title, cadence, "0.25", "open")


def _render(table):
    """Render a Rich table to plain text for substring assertions."""
    console = Console(force_terminal=False, width=200)
    with console.capture() as capture:
        console.print(table)
    return capture.get()


class TestRowToDict:
    """Tests for row_to_dict."""

    def test_projects_flat_columns(self):
        """The dict carries the flat columns (incl. curated) and not the record."""
        data = row_to_dict(_row())
        assert set(data) == {
            "provider",
            "dataset_id",
            "title",
            "cadence",
            "resolution",
            "license",
            "curated",
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


class TestFormatValue:
    """Tests for _format_value (the record-table value summariser)."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ({}, "{}"),
            ([], "[]"),
            ({"a": 1, "b": 2}, "a=1, b=2"),
        ],
    )
    def test_small_scalars(self, value, expected):
        """Empty / small scalar containers render inline.

        Args:
            value: The field value.
            expected: The rendered summary.
        """
        assert _format_value(value) == expected, f"{value!r}"

    def test_large_dict_summarised_by_keys(self):
        """A dict over four entries collapses to a count plus its first keys."""
        out = _format_value({str(i): i for i in range(10)})
        assert out.startswith("[10] "), "count prefix shown"
        assert "(+2)" in out, "overflow count shown"

    def test_long_list_summarised(self):
        """A list over eight items collapses to a count plus a head + ellipsis."""
        out = _format_value(list(range(10)))
        assert out.startswith("[10] ") and out.endswith("…"), "list summarised"


class TestRecordRenderers:
    """Tests for record_table / record_json with a model-backed row."""

    def _model_row(self):
        """A CatalogRow whose record exposes model_dump."""
        from pydantic import BaseModel

        class Rec(BaseModel):
            code: str = "00060"
            extra: dict = {"a": 1}

        row = _row()
        object.__setattr__(row, "record", Rec())
        return row

    def test_record_table_lists_model_fields(self):
        """record_table renders one row per model field plus provider/id."""
        table = record_table(self._model_row())
        rendered = _render(table)
        assert "code" in rendered and "00060" in rendered, "model field shown"

    def test_record_json_merges_model_dump(self):
        """record_json merges the model_dump into the provider/id envelope."""
        payload = json.loads(record_json(self._model_row()))
        assert payload["code"] == "00060", "model field merged"
        assert payload["provider"] == "s3", "envelope kept"

    def test_record_table_without_model(self):
        """A row whose record has no model_dump still renders provider/id."""
        rendered = _render(record_table(_row()))
        assert "provider" in rendered and "era5" in rendered, "base rows shown"


class TestCoverageTable:
    """Tests for coverage_table."""

    def test_mixed_ok_and_error_rows(self):
        """An ok row shows per-bucket counts; a non-ok row shows dashes."""
        from types import SimpleNamespace

        outcomes = [
            SimpleNamespace(
                provider="gee",
                status="ok",
                counts={
                    "DONE": 3,
                    "addressable": 1,
                    "thin": 0,
                    "table": 0,
                    "missing": 0,
                },
                detail="",
            ),
            SimpleNamespace(provider="x", status="error", counts={}, detail="boom"),
        ]
        rendered = _render(coverage_table(outcomes))
        assert "3" in rendered and "boom" in rendered, "counts + detail shown"
        assert "-" in rendered, "non-ok row dashed"
