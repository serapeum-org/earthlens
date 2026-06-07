"""Matching, filtering and cross-provider ordering for the query commands.

Pure functions over :class:`~earthlens.cli.table.CatalogRow` lists — no
I/O, no Typer — so `where` / `search` / `list` / `facets` all share one
matching and ordering policy and it can be unit-tested in isolation.

Cross-provider ordering follows the conda `channel_priority` model: when
the same dataset surfaces from several providers, an explicit, documented
priority list decides which appears first, overridable via the
`EARTHLENS_PROVIDER_PRIORITY` environment variable.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable

from earthlens.cli.table import FACET_NAMES, CatalogRow

#: Default provider precedence for ordering federated results. Providers
#: not listed here sort after these, alphabetically. The intent is that the
#: most authoritative / convenient source for a widely-mirrored dataset
#: (e.g. ERA5) wins by default; users override with
#: `EARTHLENS_PROVIDER_PRIORITY`.
DEFAULT_PROVIDER_PRIORITY: tuple[str, ...] = (
    "ecmwf",
    "cmems",
    "earthdata",
    "s3",
    "gee",
    "stac",
    "chc",
)

#: Environment variable holding a comma-separated provider precedence that
#: overrides :data:`DEFAULT_PROVIDER_PRIORITY` when set.
PRIORITY_ENV_VAR = "EARTHLENS_PROVIDER_PRIORITY"


def provider_priority() -> list[str]:
    """Return the active provider-precedence list.

    Reads `EARTHLENS_PROVIDER_PRIORITY` (comma-separated) when set,
    otherwise :data:`DEFAULT_PROVIDER_PRIORITY`.

    Returns:
        The ordered list of provider ids that sort first.

    Examples:
        - The default precedence puts ECMWF before CHC:

            ```python
            >>> from earthlens.cli.query import provider_priority
            >>> order = provider_priority()
            >>> order.index("ecmwf") < order.index("chc")
            True

            ```
    """
    raw = os.environ.get(PRIORITY_ENV_VAR)
    if raw:
        return [token.strip() for token in raw.split(",") if token.strip()]
    return list(DEFAULT_PROVIDER_PRIORITY)


def provider_rank(provider: str, priority: list[str] | None = None) -> int:
    """Rank a provider for sorting (lower sorts first).

    Args:
        provider: Canonical provider id.
        priority: Precedence list; defaults to :func:`provider_priority`.

    Returns:
        The index of `provider` in the priority list, or a large sentinel
        (so unlisted providers sort after listed ones).

    Examples:
        - A listed provider outranks an unlisted one:

            ```python
            >>> from earthlens.cli.query import provider_rank
            >>> provider_rank("ecmwf", ["ecmwf", "chc"])
            0
            >>> provider_rank("worldpop", ["ecmwf", "chc"]) > provider_rank("chc", ["ecmwf", "chc"])
            True

            ```
    """
    order = priority if priority is not None else provider_priority()
    try:
        return order.index(provider)
    except ValueError:
        return len(order)


def sort_rows(
    rows: Iterable[CatalogRow], priority: list[str] | None = None
) -> list[CatalogRow]:
    """Order rows by provider precedence, then provider id, then dataset id.

    Args:
        rows: The rows to order.
        priority: Precedence list; defaults to :func:`provider_priority`.

    Returns:
        A new sorted list (the input is not mutated).

    Examples:
        - ECMWF sorts ahead of CHC for the default precedence:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.query import sort_rows
            >>> rows = [
            ...     CatalogRow("chc", "chirps", "", "", "", ""),
            ...     CatalogRow("ecmwf", "era5", "", "", "", ""),
            ... ]
            >>> [r.provider for r in sort_rows(rows)]
            ['ecmwf', 'chc']

            ```
    """
    order = priority if priority is not None else provider_priority()
    return sorted(
        rows,
        key=lambda row: (
            provider_rank(row.provider, order),
            row.provider,
            row.dataset_id,
        ),
    )


def is_exact(row: CatalogRow, query: str) -> bool:
    """True when `row`'s dataset id equals `query` (case-insensitively)."""
    return row.dataset_id.lower() == query.strip().lower()


def match_rows(
    rows: Iterable[CatalogRow], query: str, *, exact: bool = False
) -> list[CatalogRow]:
    """Find rows matching `query` by dataset id (and title), case-insensitively.

    By default this is breadth-first — every row whose dataset id *or*
    title contains `query` (which naturally includes any exact id match),
    so "who has this dataset?" surfaces every provider that mirrors it.
    With `exact=True` only rows whose dataset id equals `query` are
    returned. Callers that want exact hits ranked first apply
    :func:`exact_first`.

    Args:
        rows: The rows to search.
        query: The dataset name / fragment to look for.
        exact: Restrict to exact dataset-id matches only.

    Returns:
        The matching rows, in input order (callers apply ordering).

    Examples:
        - Breadth-first match spans every provider mirroring a dataset:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.query import match_rows
            >>> rows = [
            ...     CatalogRow("s3", "era5", "ERA5 on AWS", "", "", ""),
            ...     CatalogRow("ecmwf", "reanalysis-era5-land", "ERA5-Land", "", "", ""),
            ...     CatalogRow("chc", "chirps", "Rainfall", "", "", ""),
            ... ]
            >>> [r.dataset_id for r in match_rows(rows, "era5")]
            ['era5', 'reanalysis-era5-land']

            ```
        - `exact=True` keeps only the literal id match:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.query import match_rows
            >>> rows = [
            ...     CatalogRow("s3", "era5", "ERA5 on AWS", "", "", ""),
            ...     CatalogRow("ecmwf", "reanalysis-era5-land", "ERA5-Land", "", "", ""),
            ... ]
            >>> [r.dataset_id for r in match_rows(rows, "era5", exact=True)]
            ['era5']

            ```
    """
    needle = query.strip().lower()
    rows = list(rows)
    if exact:
        return [row for row in rows if row.dataset_id.lower() == needle]
    return [
        row
        for row in rows
        if needle in row.dataset_id.lower() or needle in row.title.lower()
    ]


def exact_first(
    rows: Iterable[CatalogRow], query: str, priority: list[str] | None = None
) -> list[CatalogRow]:
    """Order rows with exact dataset-id matches first, then by precedence.

    Exact id matches for `query` float to the top (each group still
    ordered by :func:`sort_rows`), so `where era5` lists the provider
    keyed exactly `era5` ahead of the ones that merely mention it.

    Args:
        rows: The rows to order.
        query: The query whose exact id matches rank first.
        priority: Precedence list; defaults to :func:`provider_priority`.

    Returns:
        A new ordered list.

    Examples:
        - The literal `era5` provider sorts ahead of the partial matches:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.query import exact_first
            >>> rows = [
            ...     CatalogRow("ecmwf", "reanalysis-era5-land", "ERA5-Land", "", "", ""),
            ...     CatalogRow("s3", "era5", "ERA5 on AWS", "", "", ""),
            ... ]
            >>> [r.dataset_id for r in exact_first(rows, "era5")]
            ['era5', 'reanalysis-era5-land']

            ```
    """
    rows = list(rows)
    exact_hits = [row for row in rows if is_exact(row, query)]
    partial = [row for row in rows if not is_exact(row, query)]
    return sort_rows(exact_hits, priority) + sort_rows(partial, priority)


def free_text(rows: Iterable[CatalogRow], query: str) -> list[CatalogRow]:
    """Filter rows by a free-text query over `provider id title`.

    Args:
        rows: The rows to filter.
        query: A case-insensitive substring; an empty/blank query matches
            every row (so `--filter`-only searches work).

    Returns:
        The rows whose search blob contains `query`, in input order.

    Examples:
        - A blank query keeps everything; a term narrows by the search blob:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.query import free_text
            >>> rows = [
            ...     CatalogRow("ecmwf", "reanalysis-era5-land", "ERA5-Land", "", "", ""),
            ...     CatalogRow("chc", "chirps", "Rainfall", "", "", ""),
            ... ]
            >>> len(free_text(rows, ""))
            2
            >>> [r.provider for r in free_text(rows, "rainfall")]
            ['chc']

            ```
    """
    needle = query.strip().lower()
    rows = list(rows)
    if not needle:
        return rows
    return [row for row in rows if needle in row.search_text]


def parse_filters(values: Iterable[str]) -> dict[str, str]:
    """Parse `facet=value` filter tokens into a mapping.

    Args:
        values: Raw `--filter` tokens (each `facet=value`).

    Returns:
        A `{facet: value}` mapping (later duplicates win).

    Raises:
        ValueError: If a token is malformed or names an unknown facet.

    Examples:
        - Well-formed tokens parse into a mapping:

            ```python
            >>> from earthlens.cli.query import parse_filters
            >>> parse_filters(["provider=chc", "cadence=daily"])
            {'provider': 'chc', 'cadence': 'daily'}

            ```
        - An unknown facet is rejected:

            ```python
            >>> from earthlens.cli.query import parse_filters
            >>> parse_filters(["colour=blue"])
            Traceback (most recent call last):
                ...
            ValueError: unknown filter facet 'colour'; choose from cadence, license, provider, resolution

            ```
    """
    filters: dict[str, str] = {}
    for token in values:
        if "=" not in token:
            raise ValueError(f"filter {token!r} must be of the form facet=value")
        key, value = token.split("=", 1)
        key = key.strip().lower()
        if key not in FACET_NAMES:
            choices = ", ".join(sorted(FACET_NAMES))
            raise ValueError(f"unknown filter facet {key!r}; choose from {choices}")
        filters[key] = value.strip()
    return filters


def apply_filters(
    rows: Iterable[CatalogRow], filters: dict[str, str]
) -> list[CatalogRow]:
    """Keep rows matching every `facet=value` filter (case-insensitively).

    Args:
        rows: The rows to filter.
        filters: A `{facet: value}` mapping (see :func:`parse_filters`).

    Returns:
        The rows where each filtered facet equals its requested value.

    Examples:
        - All filters must match (logical AND):

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.query import apply_filters
            >>> rows = [
            ...     CatalogRow("chc", "chirps-daily", "", "daily", "", ""),
            ...     CatalogRow("chc", "chirps-monthly", "", "monthly", "", ""),
            ... ]
            >>> [r.dataset_id for r in apply_filters(rows, {"cadence": "daily"})]
            ['chirps-daily']

            ```
    """
    rows = list(rows)
    for facet, value in filters.items():
        wanted = value.lower()
        rows = [row for row in rows if row.facet(facet).lower() == wanted]
    return rows


def facet_counts(rows: Iterable[CatalogRow], facet: str) -> list[tuple[str, int]]:
    """Count rows per distinct non-empty value of `facet`.

    Args:
        rows: The rows to aggregate.
        facet: The facet column to group by.

    Returns:
        `(value, count)` pairs sorted by descending count, then value.

    Examples:
        - Group a small result set by provider:

            ```python
            >>> from earthlens.cli.table import CatalogRow
            >>> from earthlens.cli.query import facet_counts
            >>> rows = [
            ...     CatalogRow("chc", "a", "", "", "", ""),
            ...     CatalogRow("chc", "b", "", "", "", ""),
            ...     CatalogRow("gee", "c", "", "", "", ""),
            ... ]
            >>> facet_counts(rows, "provider")
            [('chc', 2), ('gee', 1)]

            ```
    """
    counter: Counter[str] = Counter(
        value for row in rows if (value := row.facet(facet))
    )
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
