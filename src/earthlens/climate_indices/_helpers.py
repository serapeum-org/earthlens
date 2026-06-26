"""Pure, network-free helpers for the climate-indices backend.

Two ASCII dialects are parsed here into one canonical long frame:

* `parse_psl` — the NOAA PSL `correlation/<index>.data` layout: a
  `<first_year> <last_year>` header, one `year jan..dec` row per year
  (13 whitespace tokens), then a lone single-token missing-value
  sentinel line and a free-text provenance footer. The sentinel varies
  per file (`-99.9`, `-99.99`, `-9.99`, `-9.90`, `-999`), so it is read
  from the lone numeric line that follows the data block rather than
  assumed.
* `parse_climexp` — the KNMI Climate Explorer `<id>.dat` grid layout:
  any number of `#`-prefixed comment lines, then `year jan..dec` rows
  (13 tokens) or `year jan..dec annual` rows (14 tokens, the trailing
  annual mean is dropped). The sentinel is `-999.9`.

Both return the canonical `(date, value)` long frame (monthly
`Timestamp` on the first of each month, the sentinel mapped to `NaN`).
Everything here is pure text → pandas — there is no `xarray` anywhere
in this subpackage by design.
"""

from __future__ import annotations

import pandas as pd

#: The canonical long-format schema every parsed / concatenated frame
#: carries (the backend stamps `index` / `source` onto the `(date,
#: value)` frame these parsers return).
COLUMNS: list[str] = ["date", "index", "value", "source"]

#: The fixed missing-value sentinel used by the KNMI climexp grid files.
CLIMEXP_SENTINEL: float = -999.9


def empty_canonical() -> pd.DataFrame:
    """Return the zero-row canonical long frame.

    Returns:
        pd.DataFrame: An empty frame with exactly the :data:`COLUMNS`
            schema (`date`, `index`, `value`, `source`) and no rows —
            the all-empty fallback the backend returns when no requested
            index yields any data.
    """
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})


def _is_year_token(token: str) -> bool:
    """Return whether `token` is a bare 4-digit calendar year.

    Args:
        token: A single whitespace-split token.

    Returns:
        bool: `True` for a 4-character all-digit string (e.g. `"1948"`),
            `False` otherwise (an 8-digit `YYYYMMDD`, a float, text).
    """
    return len(token) == 4 and token.isdigit()


def _melt_year_months(
    rows: list[tuple[int, list[float]]], sentinel: float | None
) -> pd.DataFrame:
    """Reshape `(year, [jan..dec])` rows into a long `(date, value)` frame.

    Args:
        rows: One `(year, months)` pair per data row, where `months` is
            the 12 monthly values in calendar order.
        sentinel: The missing-value sentinel to map to `NaN`, or `None`
            to keep every value as-is.

    Returns:
        pd.DataFrame: A `date`/`value` frame with one row per month
            (`date` is the first of the month), values equal to
            `sentinel` replaced by `NaN`.
    """
    records: list[tuple[pd.Timestamp, float]] = []
    for year, months in rows:
        for month, value in enumerate(months, start=1):
            is_missing = sentinel is not None and value == sentinel
            records.append(
                (
                    pd.Timestamp(year=year, month=month, day=1),
                    float("nan") if is_missing else value,
                )
            )
    return pd.DataFrame(records, columns=["date", "value"])


def parse_psl(text: str) -> pd.DataFrame:
    """Parse a NOAA PSL `correlation/<index>.data` series to a long frame.

    Reads every 13-token `year jan..dec` data row, then takes the
    missing-value sentinel from the first lone single-numeric-token line
    that follows the data block (it varies per file). The
    `<first_year> <last_year>` header (2 tokens) and the free-text
    provenance footer (multi-token lines) are ignored.

    Args:
        text: The full file body as text.

    Returns:
        pd.DataFrame: The canonical `(date, value)` long frame; empty
            (zero rows) when no data rows are present.
    """
    rows: list[tuple[int, list[float]]] = []
    sentinel: float | None = None
    seen_data = False
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 13 and _is_year_token(parts[0]):
            try:
                months = [float(token) for token in parts[1:]]
            except ValueError:
                continue
            rows.append((int(parts[0]), months))
            seen_data = True
        elif seen_data and sentinel is None and len(parts) == 1:
            try:
                sentinel = float(parts[0])
            except ValueError:
                continue
    return _melt_year_months(rows, sentinel)


def parse_climexp(text: str) -> pd.DataFrame:
    """Parse a KNMI climexp `<id>.dat` grid series to a long frame.

    Skips `#`-prefixed comment lines and reads `year jan..dec` rows (13
    tokens) or `year jan..dec annual` rows (14 tokens — the trailing
    annual-mean column is dropped). The fixed :data:`CLIMEXP_SENTINEL`
    (`-999.9`) is mapped to `NaN`. A non-grid body (an HTML error page,
    or the `YYYYMMDD value` long form) yields no rows.

    Args:
        text: The full file body as text.

    Returns:
        pd.DataFrame: The canonical `(date, value)` long frame; empty
            (zero rows) when no grid data rows are present.
    """
    rows: list[tuple[int, list[float]]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) in (13, 14) and _is_year_token(parts[0]):
            try:
                months = [float(token) for token in parts[1:13]]
            except ValueError:
                continue
            rows.append((int(parts[0]), months))
    return _melt_year_months(rows, CLIMEXP_SENTINEL)
