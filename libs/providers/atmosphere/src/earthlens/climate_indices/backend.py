"""Backend that fetches monthly climate / teleconnection indices.

`ClimateIndices(AbstractDataSource)` downloads small monthly
teleconnection-index ASCII files from two open sources — NOAA PSL and the
KNMI Climate Explorer — parses each into a tidy long frame, filters to the
requested window, and returns the concatenation as a long-format
:class:`pandas.DataFrame` (`date`, `index`, `value`, `source`).

A request is `variables=[index id, …]` (e.g. `["oni", "nao"]`) + a
`[start, end]` window; the index id selects the catalogue row, which pins
the URL and the ASCII dialect. Climate indices are **global scalar
monthly series** with no geometry, so this is `OUTPUT_KIND = "tabular"`:
spatial arguments (`lat_lim` / `lon_lim` / `aoi`) are accepted for
signature parity but ignored, and the :class:`earthlens.earthlens.EarthLens`
facade rejects a non-`None` `aggregate=` (these are already monthly
scalars — there is nothing to grid-reduce).

The two sources are open (no auth, no SDK), so there is no `auth.py`; the
fetch is a plain `requests` GET and the parse is pure pandas/text — these
scalar series use no gridded-array dependency.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
    date_windows,
    to_datetime,
)
from earthlens.base.http import HttpClient
from earthlens.climate_indices import _helpers
from earthlens.climate_indices.catalog import Catalog

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk output formats for the written table.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: HTTP timeout (seconds) for a single index-file GET.
_HTTP_TIMEOUT: float = 60.0

#: Number of extra attempts after the first on a transient fetch failure
#: (connection error / timeout / 5xx). A 4xx (e.g. 404) fails fast.
_HTTP_RETRIES: int = 2

#: Base back-off (seconds) between retry attempts; the nth retry waits
#: `_HTTP_RETRY_BACKOFF * 2**(n-1)` (HttpClient's exponential back-off).
#: Coincides with the previous hand-rolled linear back-off at
#: `_HTTP_RETRIES=2` (both yield `[1s, 2s]`); bumping `_HTTP_RETRIES` diverges
#: the two (linear grows as `n`, exponential as `2**(n-1)`), whereas scaling
#: `_HTTP_RETRY_BACKOFF` rescales both by the same factor.
_HTTP_RETRY_BACKOFF: float = 1.0

#: Max index ids spelled out in the written-table filename before it is
#: summarised as `<n>_indices` (keeps the name short for big requests).
_MAX_STEM_IDS: int = 6

#: Global sentinel bounds — climate indices have no geometry, so the
#: spatial extent is the whole globe (`G4`).
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


class ClimateIndices(AbstractDataSource):
    """Climate / teleconnection index backend (long-format tabular output).

    Fetches one monthly index series per id in `variables` from its
    catalogue source (NOAA PSL or KNMI Climate Explorer), parses the
    source's ASCII dialect, filters to the `[start, end]` window, and
    returns the concatenation as a long-format
    :class:`pandas.DataFrame` (`date`, `index`, `value`, `source`). The
    query is a search/fetch split: :meth:`_search` resolves each id to a
    catalogue row, and :meth:`_fetch` downloads and parses each one.

    Climate indices are global scalar monthly series, so spatial
    arguments are ignored (`G4`) and `aggregate=` is rejected (`G1`).

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is long-format rows, so the
            facade rejects `aggregate=` with `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "climate indices are tabular monthly scalars, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate= and post-process the returned DataFrame directly"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "monthly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        output_format: OutputFormat = "csv",
    ):
        """Initialise a climate-indices backend instance.

        Args:
            start: Inclusive start of the window, parsed with `fmt`.
            end: Inclusive end of the window.
            variables: Index ids to fetch (`["oni", "nao"]`). Resolved
                against the catalogue with a did-you-mean hint on a miss.
                Must be a non-empty list — there is no implicit
                "fetch everything". Repeated ids are de-duplicated
                order-stably (first-wins), so each series is fetched once.
            lat_lim: Accepted for signature parity and **ignored** —
                climate indices are global scalars (`G4`).
            lon_lim: Accepted for signature parity and **ignored**.
            temporal_resolution: Recorded as the resolution label; the
                series are monthly. Defaults to `"monthly"`.
            path: Output directory for the written table.
            fmt: `strptime` format for `start` / `end`.
            output_format: On-disk format — `"csv"` (default) or
                `"parquet"`.

        Raises:
            TypeError: If `variables` is a mapping (this backend takes a
                flat list of index ids).
            ValueError: If `variables` is empty, or `output_format` is
                not a recognised value.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "ClimateIndices `variables` must be a list of index ids "
                "(e.g. ['oni', 'nao']), not a mapping."
            )
        # De-dupe the requested ids order-stably (first-wins) so a repeated
        # id does not fetch and emit the same series twice (mirrors the
        # usgs_water parameter-code de-dupe).
        index_ids = list(dict.fromkeys(variables)) if variables else []
        if not index_ids:
            raise ValueError(
                "ClimateIndices needs variables=[index id, ...]; available "
                f"indices: {Catalog().available()}."
            )
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )
        self._output_format: OutputFormat = output_format
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=index_ids,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Return a global :class:`SpatialExtent` (spatial args ignored).

        Climate indices have no geometry (`G4`), so the bbox arguments are
        discarded and a whole-globe extent is returned to keep
        `self.space` well-formed.

        Args:
            lat_lim: Ignored.
            lon_lim: Ignored.

        Returns:
            SpatialExtent: The whole-globe extent.
        """
        return SpatialExtent.from_pairs(lat_lim=_GLOBAL_LAT, lon_lim=_GLOBAL_LON)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a monthly :class:`TemporalExtent`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model spanning the window at month-start
                (`"MS"`) cadence.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="MS",
            dates=date_windows(start_dt, end_dt, "MS"),
        )

    def _search(self) -> list[RemoteProduct]:
        """Resolve each requested id to a catalogue row (one product each).

        Returns:
            list[RemoteProduct]: One product per index id, in request
                order; `href` is the file URL and `metadata` carries the
                row's `dialect` / `source` / `citation` / `units`.

        Raises:
            ValueError: If an id is not in the catalogue (with a
                did-you-mean hint).
        """
        products: list[RemoteProduct] = []
        for index_id in self.vars:
            row = self._catalog.get(index_id)
            products.append(
                RemoteProduct(
                    id=index_id,
                    href=row.url,
                    metadata={
                        "dialect": row.dialect,
                        "source": row.source,
                        "citation": row.citation,
                        "units": row.units,
                    },
                )
            )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[pd.DataFrame]:
        """Download and parse each product into a canonical long frame.

        Widens the inherited `-> list[Path]` contract: a tabular backend
        returns in-memory long-format frames, not file paths (the write
        happens in :meth:`download` via :meth:`_write_table`).

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[pd.DataFrame]: One canonical long-schema frame per
                product (empty when the window held no data), same order.
        """
        return self._fetch_limited(products, self._limit)

    def _fetch_one(self, product: RemoteProduct) -> pd.DataFrame:
        """Fetch one index, parse it, stamp it, and filter to the window.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            pd.DataFrame: The canonical long-schema frame for this index
                (empty-canonical when the window held no data, `G7`).

        Raises:
            ValueError: On a fetch failure or an unparseable body — the
                message names the index id and URL (`G8`).
        """
        text = self._get_text(product.href, product.id)
        parser = (
            _helpers.parse_psl
            if product.metadata["dialect"] == "psl"
            else (_helpers.parse_climexp)
        )
        parsed = parser(text)
        if parsed.empty:
            raise ValueError(
                f"climate index {product.id!r}: no monthly data parsed from "
                f"{product.href} (the source may have returned an error page)."
            )
        stamped = parsed.assign(index=product.id, source=product.metadata["source"])
        window = (stamped["date"] >= self.time.start_date) & (
            stamped["date"] <= self.time.end_date
        )
        result = stamped.loc[window, _helpers.COLUMNS].reset_index(drop=True)
        if result.empty:
            logger.warning(
                f"climate index {product.id!r}: no values in the requested "
                f"window [{self.time.start_date:%Y-%m} .. "
                f"{self.time.end_date:%Y-%m}]; contributing zero rows."
            )
            return _helpers.empty_canonical()
        return result

    @staticmethod
    def _get_text(url: str | None, index_id: str) -> str:
        """GET an index file's body, retrying transient failures.

        A transient failure (connection error, timeout, or a 5xx status)
        is retried up to :data:`_HTTP_RETRIES` times with an exponential
        back-off (`1s`, `2s`); a 4xx (e.g. 404) fails fast since retrying
        a real miss is pointless. The retry engine is
        :class:`~earthlens.base.http.HttpClient`, configured to match the
        old hand-rolled loop byte-for-byte on wait sequence and attempt
        count.

        Args:
            url: The index file URL.
            index_id: The requesting index id (for the error message).

        Returns:
            str: The response body text.

        Raises:
            ValueError: When the GET still fails after the retries, naming
                the index and URL (`G8`).
        """
        if url is None:
            raise ValueError(f"climate index {index_id!r}: no URL to fetch.")
        http = HttpClient(
            timeout=_HTTP_TIMEOUT,
            max_retries=_HTTP_RETRIES,
            backoff_factor=_HTTP_RETRY_BACKOFF,
            status_forcelist=tuple(range(500, 600)),
            retry_on_exceptions=(requests.ConnectionError, requests.Timeout),
            raise_for_status=True,
            sleep=lambda seconds: time.sleep(seconds),
        )
        try:
            return http.get(url).text
        except requests.RequestException as exc:
            raise ValueError(
                f"climate index {index_id!r}: failed to fetch {url} ({exc})."
            ) from exc

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Fetch every requested index, write the table, and return it.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends; the per-index loop is short, so this is a
                no-op.
            limit: Cap on the total rows returned, across every requested
                item. Applied as the per-item results arrive, so an item past
                the cap is never fetched. `None` (the default) fetches
                everything, which for a wide request is bounded only by memory.

        Returns:
            pd.DataFrame: The long-format table (`date`, `index`,
                `value`, `source`) for every requested index over the
                window; an empty (schema-only) frame when nothing matched.

        Raises:
            ValueError: If an index id is unknown, or a fetch fails
                (`G8`).
        """
        self._limit = self.check_limit(limit)
        frames = [frame for frame in self._api() if len(frame)]
        df = (
            pd.concat(frames, ignore_index=True)
            if frames
            else _helpers.empty_canonical()
        )
        out_path = self._write_table(df)
        self._log_citations()
        if len(df):
            logger.info(
                f"ClimateIndices: {len(df)} row(s) across "
                f"{df['index'].nunique()} index/indices written to {out_path}"
            )
        else:
            logger.warning(
                "ClimateIndices: no rows matched the requested window; wrote "
                f"an empty (schema-only) table to {out_path}"
            )
        return df

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write the long-format table to `root_dir` and return the path.

        The filename spells out the requested ids
        (`climate_indices_oni_nao.csv`) up to :data:`_MAX_STEM_IDS`; beyond
        that it is summarised as `climate_indices_<n>_indices.csv` to keep
        the name short.

        Args:
            df: The canonical long-format frame.

        Returns:
            Path: The written CSV / Parquet file path.

        Raises:
            ImportError: If `output_format="parquet"` but `pyarrow` is
                not installed.
        """
        if len(self.vars) <= _MAX_STEM_IDS:
            stem = "climate_indices_" + "_".join(self.vars)
        else:
            stem = f"climate_indices_{len(self.vars)}_indices"
        ext = "parquet" if self._output_format == "parquet" else "csv"
        out_path = self.root_dir / f"{stem}.{ext}"
        if self._output_format == "parquet":
            try:
                df.to_parquet(out_path, index=False)
            except ImportError as exc:  # pragma: no cover - depends on env
                raise ImportError(
                    "Writing Parquet requires 'pyarrow'. Install it (pip "
                    "install pyarrow) or use output_format='csv'."
                ) from exc
        else:
            df.to_csv(out_path, index=False)
        return out_path

    def _log_citations(self) -> None:
        """Log each requested source's citation once (info, not a warning)."""
        seen: set[str] = set()
        for index_id in self.vars:
            citation = self._catalog.datasets[index_id].citation
            if citation and citation not in seen:
                seen.add(citation)
                logger.info(f"ClimateIndices source citation: {citation}")
