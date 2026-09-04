"""Output helpers shared by every query command (tables, JSON, ids, warnings).

Centralises the dual-output contract — a human-readable Rich table by
default, machine output (`--json` / `--ids-only`) for piping — and the
"never silently drop a backend" rule: backends that failed to load are
printed as warnings on stderr, so a federated result is always honest
about what it could not scan.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from earthlens.cli.table import CatalogRow, LoadError

if TYPE_CHECKING:
    from rich.console import JustifyMethod

#: Header label for each :class:`~earthlens.cli.table.CatalogRow` column.
_COLUMN_HEADERS: dict[str, str] = {
    "provider": "PROVIDER",
    "dataset_id": "DATASET ID",
    "title": "TITLE",
    "cadence": "CADENCE",
    "resolution": "RESOLUTION",
    "license": "LICENSE",
}

#: Column set for the search / where result tables.
DEFAULT_COLUMNS: tuple[str, ...] = ("provider", "dataset_id", "title")

#: Compact column set — `list`'s names-only default.
COMPACT_COLUMNS: tuple[str, ...] = ("provider", "dataset_id")

#: Detailed column set — `list --full`.
FULL_COLUMNS: tuple[str, ...] = (
    "provider",
    "dataset_id",
    "title",
    "cadence",
    "resolution",
)


def out_console() -> Console:
    """Return a stdout Console for results."""
    return Console()


def err_console() -> Console:
    """Return a stderr Console for warnings (keeps stdout pipe-clean)."""
    return Console(stderr=True)


def row_to_dict(row: CatalogRow) -> dict[str, str | bool]:
    """Project a row to a JSON-friendly dict (drops the pydantic record).

    Args:
        row: The catalog row to project.

    Returns:
        A flat `{provider, dataset_id, title, cadence, resolution, license}`
        mapping.

    Examples:
        - The record object is excluded so the dict is JSON-serialisable:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.render import row_to_dict
            >>> row = CatalogRow("s3", "era5", "ERA5", "monthly", "0.25", "")
            >>> row_to_dict(row)["dataset_id"]
            'era5'
            >>> row_to_dict(row)["curated"]
            True

            ```
    """
    return {
        "provider": row.provider,
        "dataset_id": row.dataset_id,
        "title": row.title,
        "cadence": row.cadence,
        "resolution": row.resolution,
        "license": row.license,
        "curated": row.curated,
    }


def rows_to_json(rows: Iterable[CatalogRow]) -> str:
    """Serialise rows to a pretty JSON array string.

    Args:
        rows: The rows to serialise.

    Returns:
        A 2-space-indented JSON array of :func:`row_to_dict` objects.

    Examples:
        - Each row becomes one JSON object:

            ```python
            >>> import json
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.render import rows_to_json
            >>> rows = [CatalogRow("ecmwf", "era5", "ERA5", "", "", "")]
            >>> json.loads(rows_to_json(rows))[0]["provider"]
            'ecmwf'

            ```
    """
    return json.dumps([row_to_dict(row) for row in rows], indent=2)


def rows_to_ids(rows: Iterable[CatalogRow]) -> str:
    """Render rows as `provider<TAB>dataset_id` lines for piping.

    The tab-separated pair is exactly what `earthlens datasets show
    <provider> <dataset>` consumes, so results pipe straight back in.

    Args:
        rows: The rows to render.

    Returns:
        A newline-joined string (empty when there are no rows).

    Examples:
        - One `provider<TAB>id` line per row:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.render import rows_to_ids
            >>> rows = [CatalogRow("ecmwf", "era5", "ERA5", "", "", "")]
            >>> rows_to_ids(rows)
            'ecmwf\\tera5'

            ```
    """
    return "\n".join(f"{row.provider}\t{row.dataset_id}" for row in rows)


def rows_table(
    rows: Sequence[CatalogRow],
    title: str | None = None,
    columns: tuple[str, ...] = DEFAULT_COLUMNS,
) -> Table:
    """Build a Rich table of the chosen `columns` for `rows`.

    Args:
        rows: The rows to tabulate.
        title: Optional table title (e.g. a result summary).
        columns: The :class:`~earthlens.cli.table.CatalogRow` attributes to
            show, in order. Defaults to provider / dataset id / title; use
            :data:`COMPACT_COLUMNS` or :data:`FULL_COLUMNS` for `list`.

    Returns:
        A populated :class:`rich.table.Table`.

    Examples:
        - The default table has one row per dataset and three columns:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.render import rows_table
            >>> table = rows_table([CatalogRow("s3", "era5", "ERA5", "", "", "")])
            >>> table.row_count
            1
            >>> [column.header for column in table.columns]
            ['PROVIDER', 'DATASET ID', 'TITLE']

            ```
    """
    table = Table(title=title, header_style="bold", show_lines=False)
    for column in columns:
        table.add_column(_COLUMN_HEADERS[column], overflow="fold")
    for row in rows:
        table.add_row(*(getattr(row, column) or "" for column in columns))
    return table


def print_load_warnings(
    errors: Iterable[LoadError], console: Console | None = None
) -> None:
    """Print one stderr warning per backend that failed to load.

    Args:
        errors: The load failures to report.
        console: Target stderr console; a fresh one is used when omitted.
    """
    errors = list(errors)
    if not errors:
        return
    console = console or err_console()
    for error in errors:
        console.print(
            f"[yellow]warning:[/yellow] skipped provider "
            f"{error.provider!r}: {error.error}"
        )


def counts_table(counts_by_facet: dict[str, list[tuple[str, int]]]) -> Table:
    """Build a `FACET / VALUE / COUNT` table from per-facet count lists.

    Args:
        counts_by_facet: Mapping of facet name to its `(value, count)` pairs
            (e.g. from :func:`earthlens.cli.query.facet_counts`).

    Returns:
        A populated :class:`rich.table.Table`; facets with no values are
        skipped.

    Examples:
        - One row per `(facet, value)` pair, three columns:

            ```python
            >>> from earthlens.cli.render import counts_table
            >>> table = counts_table({"provider": [("chc", 2), ("gee", 1)]})
            >>> table.row_count
            2
            >>> [column.header for column in table.columns]
            ['FACET', 'VALUE', 'COUNT']

            ```
    """
    table = Table(header_style="bold", show_lines=False)
    table.add_column("FACET", overflow="fold")
    table.add_column("VALUE", overflow="fold")
    table.add_column("COUNT", justify="right")
    for facet, counts in counts_by_facet.items():
        for value, count in counts:
            table.add_row(facet, value, str(count))
    return table


def _is_scalar(value: object) -> bool:
    """True for values cheap to print inline (str / number / bool / None)."""
    return value is None or isinstance(value, (str, int, float, bool))


def _format_value(value: object) -> str:
    """Render a record field value compactly for the detail table.

    Small all-scalar dicts (e.g. a `cadence` of `{interval, unit}`) render
    as `key=value` pairs; larger / nested dicts collapse to a count plus
    their first keys (e.g. a `variables` map shows the variable names).
    """
    if isinstance(value, dict):
        if not value:
            return "{}"
        if len(value) <= 4 and all(_is_scalar(v) for v in value.values()):
            return ", ".join(f"{key}={val}" for key, val in value.items())
        keys = list(value)
        head = ", ".join(str(key) for key in keys[:8])
        more = "" if len(keys) <= 8 else f", … (+{len(keys) - 8})"
        return f"[{len(keys)}] {head}{more}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        head = ", ".join(str(item) for item in value[:8])
        more = "" if len(value) <= 8 else " …"
        return f"[{len(value)}] {head}{more}"
    return str(value)


def record_json(row: CatalogRow) -> str:
    """Serialise a row's full backend record to indented JSON.

    Args:
        row: The catalog row whose `record` to dump.

    Returns:
        A JSON object string carrying `provider` / `dataset_id` plus every
        field of the backend's pydantic record.

    Examples:
        - The provider and dataset id always lead the object:

            ```python
            >>> import json
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.render import record_json
            >>> row = CatalogRow("s3", "era5", "ERA5", "monthly", "", "")
            >>> json.loads(record_json(row))["dataset_id"]
            'era5'

            ```
    """
    data: dict[str, object] = {
        "provider": row.provider,
        "dataset_id": row.dataset_id,
    }
    record = row.record
    if record is not None and hasattr(record, "model_dump"):
        data.update(record.model_dump(mode="json"))
    return json.dumps(data, indent=2, default=str)


def record_table(row: CatalogRow) -> Table:
    """Build a `FIELD / VALUE` detail table for a row's backend record.

    Args:
        row: The catalog row to describe.

    Returns:
        A :class:`rich.table.Table` with one row per record field; nested
        dict / list fields are summarised by size and first members.
    """
    table = Table(header_style="bold", show_lines=False)
    table.add_column("FIELD", overflow="fold")
    table.add_column("VALUE", overflow="fold")
    table.add_row("provider", row.provider)
    table.add_row("dataset_id", row.dataset_id)
    record = row.record
    if record is not None and hasattr(record, "model_dump"):
        for field_name, value in record.model_dump().items():
            table.add_row(field_name, _format_value(value))
    return table


def kv_table(
    header_a: str, header_b: str, pairs, justify_b: JustifyMethod = "left"
) -> Table:
    """Build a simple two-column table from `(a, b)` pairs.

    Args:
        header_a: Header for the first (left) column.
        header_b: Header for the second column.
        pairs: An iterable of `(a, b)` rows; both are stringified.
        justify_b: Justification for the second column (e.g. `"right"`).

    Returns:
        A populated :class:`rich.table.Table`.

    Examples:
        - Two columns, one row per pair:

            ```python
            >>> from earthlens.cli.render import kv_table
            >>> table = kv_table("FACET", "DISTINCT", [("provider", 22)])
            >>> table.row_count
            1
            >>> [column.header for column in table.columns]
            ['FACET', 'DISTINCT']

            ```
    """
    table = Table(header_style="bold", show_lines=False)
    table.add_column(header_a, overflow="fold")
    table.add_column(header_b, justify=justify_b, overflow="fold")
    for left, right in pairs:
        table.add_row(str(left), str(right))
    return table


def refresh_table(outcomes) -> Table:
    """Build a per-provider summary table of live-refresh outcomes.

    Args:
        outcomes: An iterable of `RefreshOutcome` (duck-typed: `provider`,
            `status`, `live_count`, `bundled_count`, `new_ids`, `detail`).

    Returns:
        A :class:`rich.table.Table`; counts show `-` for non-`ok` rows.
    """
    table = Table(header_style="bold", show_lines=False)
    table.add_column("PROVIDER", overflow="fold")
    table.add_column("STATUS", overflow="fold")
    table.add_column("LIVE", justify="right")
    table.add_column("BUNDLED", justify="right")
    table.add_column("NEW", justify="right")
    table.add_column("DETAIL", overflow="fold")
    for outcome in outcomes:
        ok = outcome.status == "ok"
        table.add_row(
            outcome.provider,
            outcome.status,
            str(outcome.live_count) if ok else "-",
            str(outcome.bundled_count) if ok else "-",
            str(len(outcome.new_ids)) if ok else "-",
            outcome.detail,
        )
    return table


def probe_table(result) -> Table:
    """Build a schema table from a probe result, with dynamic columns.

    Each provider's probe yields a different per-entry field set (STAC asset
    media-type/dtype, GEE/openEO band common-name/units, …), so the columns
    are the union of the entries' field keys — a `NAME` column plus one per
    field seen, in first-seen order.

    Args:
        result: A `ProbeResult` (duck-typed: `.assets` mapping of entry name
            to a `{field: value}` schema dict).

    Returns:
        A :class:`rich.table.Table`; missing fields render as `-`.
    """
    field_keys: list[str] = []
    for schema in result.assets.values():
        for key in schema:
            if key not in field_keys:
                field_keys.append(key)
    table = Table(header_style="bold", show_lines=False)
    table.add_column("NAME", overflow="fold")
    for key in field_keys:
        table.add_column(key.upper(), overflow="fold")
    for name, schema in result.assets.items():
        cells = [
            "-" if schema.get(key) is None else str(schema.get(key))
            for key in field_keys
        ]
        table.add_row(name, *cells)
    return table


def audit_table(outcomes) -> Table:
    """Build a per-provider summary table of audit outcomes.

    Args:
        outcomes: An iterable of `AuditOutcome` (duck-typed: `provider`,
            `status`, `live_count`, `curated_count`, `broken`, `detail`).

    Returns:
        A :class:`rich.table.Table`; counts show `-` for non-`ok` rows. Variable
        drift is not a column (it would overflow an 80-column terminal); it is
        surfaced by the `datasets audit` command's per-provider drift lines, the
        `--json` output, and the `--strict` exit code.
    """
    table = Table(header_style="bold", show_lines=False)
    table.add_column("PROVIDER", overflow="fold")
    table.add_column("STATUS", overflow="fold")
    table.add_column("CURATED", justify="right")
    table.add_column("LIVE", justify="right")
    table.add_column("BROKEN", justify="right")
    table.add_column("UNTRACKED", justify="right")
    table.add_column("DETAIL", overflow="fold")
    for outcome in outcomes:
        ok = outcome.status == "ok"
        table.add_row(
            outcome.provider,
            outcome.status,
            str(outcome.curated_count) if ok else "-",
            str(outcome.live_count) if ok else "-",
            str(len(outcome.broken)) if ok else "-",
            str(len(outcome.untracked)) if ok else "-",
            outcome.detail,
        )
    return table


def coverage_table(outcomes) -> Table:
    """Build a per-provider curation-coverage table.

    Args:
        outcomes: An iterable of `CoverageOutcome` (duck-typed: `provider`,
            `status`, `counts`, `todo`, `detail`).

    Returns:
        A :class:`rich.table.Table` with one column per coverage bucket;
        counts show `-` for non-`ok` rows.
    """
    buckets = ("DONE", "addressable", "thin", "table", "missing")
    table = Table(header_style="bold", show_lines=False)
    table.add_column("PROVIDER", overflow="fold")
    table.add_column("STATUS", overflow="fold")
    for bucket in buckets:
        table.add_column(bucket.upper(), justify="right")
    table.add_column("DETAIL", overflow="fold")
    for outcome in outcomes:
        ok = outcome.status == "ok"
        table.add_row(
            outcome.provider,
            outcome.status,
            *(str(outcome.counts.get(b, 0)) if ok else "-" for b in buckets),
            outcome.detail,
        )
    return table


def validate_table(results) -> Table:
    """Build a per-provider summary table of validation results.

    Args:
        results: An iterable of `ValidateResult` (duck-typed: `provider`,
            `status`, `checked`, `issues`, `detail`).

    Returns:
        A :class:`rich.table.Table`; counts show `-` for non-`ok` rows.
    """
    table = Table(header_style="bold", show_lines=False)
    table.add_column("PROVIDER", overflow="fold")
    table.add_column("STATUS", overflow="fold")
    table.add_column("CHECKED", justify="right")
    table.add_column("ISSUES", justify="right")
    table.add_column("DETAIL", overflow="fold")
    for result in results:
        ok = result.status == "ok"
        table.add_row(
            result.provider,
            result.status,
            str(result.checked) if ok else "-",
            str(len(result.issues)) if ok else "-",
            result.detail,
        )
    return table
