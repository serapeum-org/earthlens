"""Backend that fetches the FLODIS observed-flood impact tables.

`FLODIS(AbstractDataSource)` downloads one of the two FLODIS tables — `damages`
(EM-DAT fatalities + economic damages) or `displacement` (IDMC displacements),
each matched to Global Flood Database satellite footprints — from its pinned
static Zenodo record, filters it, and returns the per-event impact records as a
:class:`pandas.DataFrame`. It is the observed hazard-footprint -> impact bridge:
the global companion to the European `hanze` backend, and to the raw impact
tables in `emdat`.

Three design points carry this backend:

* **`dataset=` selects the table.** `dataset="damages"` (default) returns the
  EM-DAT deaths/damages table keyed on `disasterno`; `dataset="displacement"`
  returns the IDMC table keyed on `GID_1` / `GID_2`. The selector rides the
  facade's native-`dataset` path (the `s3` precedent), so
  `EarthLens("flodis", dataset="displacement", ...)` reaches the backend as a
  `dataset=` kwarg. FLODIS has no variable axis: a non-empty `variables=` is
  rejected.
* **Fetch the tables; join footprints via shipped backends.** FLODIS carries the
  join keys (`disasterno`, `GID_1` / `GID_2`) but does not re-fetch the
  footprints — the GDIS geometry comes from the shipped `emdat` backend and the
  GFD extents from the shipped `gee` backend, joined on those keys. The tables
  have no per-row coordinates, so `lat_lim` / `lon_lim` are accepted (the facade
  always supplies them) but are not a filter axis; filter by `country=` (ISO3),
  `gid=` (GADM, displacement only) and the date window instead.
* **No new dependency.** `HttpClient` + pandas are core and the Zenodo record is
  public (CC-BY-4.0), so there is no auth and no `[flodis]` extra.

These are per-event impact records, not gridded rasters, so `aggregate=` is
refused and nothing here imports a gridded-array library (no `xarray`).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
    to_datetime,
)
from earthlens.base.http import HttpClient
from earthlens.flodis.catalog import Catalog, FlodisDataset

#: Global sentinel bounds — FLODIS carries no per-row coordinates, so these are
#: recorded on the spatial extent but never used to drop rows.
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]

#: Leading bytes shared by both FLODIS CSVs (a leading unnamed index column, then
#: `ISO3`), used to reject an HTML error page served with a 200 status.
_CSV_MAGIC = b",ISO3,"


def _as_list(value: str | list[str] | None) -> list[str]:
    """Return a selector argument as a list of strings.

    Args:
        value: A single value, a list of values, or `None`.

    Returns:
        list[str]: `[]` for `None`, `[value]` for a bare string, else the list.

    Examples:
        - `None` becomes an empty list; a bare string is wrapped; a list passes through:
            ```python
            >>> from earthlens.flodis.backend import _as_list
            >>> _as_list(None)
            []
            >>> _as_list("MOZ")
            ['MOZ']
            >>> _as_list(["MOZ", "BGD"])
            ['MOZ', 'BGD']

            ```
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _normalize_iso3(country: str | list[str] | None) -> set[str]:
    """Validate and upper-case the requested ISO3 country codes.

    Args:
        country: One ISO3 code, a list of them, or `None` to keep every country.

    Returns:
        set[str]: The upper-cased codes; empty when `None`.

    Raises:
        ValueError: If a value is not three ASCII letters. This catches a
            *malformed* code (a typo in the ISO3 form). A well-formed but absent
            code (`"XYZ"`, or a country with no 2000-2018 flood events) is
            allowed through and legitimately yields an empty result.

    Examples:
        - Codes are upper-cased and de-duplicated; `None` keeps every country:
            ```python
            >>> from earthlens.flodis.backend import _normalize_iso3
            >>> _normalize_iso3("moz")
            {'MOZ'}
            >>> sorted(_normalize_iso3(["moz", "MOZ", "bgd"]))
            ['BGD', 'MOZ']
            >>> _normalize_iso3(None)
            set()

            ```
        - A malformed code (not three letters) is rejected:
            ```python
            >>> from earthlens.flodis.backend import _normalize_iso3
            >>> _normalize_iso3("MO")
            Traceback (most recent call last):
                ...
            ValueError: country= must be 3-letter ISO3 code(s) (e.g. 'MOZ'); got 'MO'. FLODIS keys events by ISO3 country code.

            ```
    """
    codes: set[str] = set()
    for raw in _as_list(country):
        code = raw.strip().upper()
        if not (len(code) == 3 and code.isalpha() and code.isascii()):
            raise ValueError(
                f"country= must be 3-letter ISO3 code(s) (e.g. 'MOZ'); got "
                f"{raw!r}. FLODIS keys events by ISO3 country code."
            )
        codes.add(code)
    return codes


def _normalize_gid(
    gid: str | list[str] | None, dataset: str, row: FlodisDataset
) -> set[str]:
    """Validate the requested GADM codes for the displacement table.

    Args:
        gid: One GADM `GID_1` / `GID_2` code, a list of them, or `None`.
        dataset: The selected table name, for the error message.
        row: The resolved dataset row, whose `key_columns` tell whether the
            table is GADM-keyed.

    Returns:
        set[str]: The upper-cased GADM codes; empty when `None`.

    Raises:
        ValueError: If `gid` is given for a table that is not GADM-keyed (the
            `damages` table has no `GID_1` / `GID_2` columns).

    Examples:
        - A gid is upper-cased for the GADM-keyed displacement table:
            ```python
            >>> from earthlens.flodis import FlodisDataset
            >>> from earthlens.flodis.backend import _normalize_gid
            >>> row = FlodisDataset(file="FLODIS_displacement.csv", key_columns=("GID_1", "GID_2"))
            >>> sorted(_normalize_gid("moz.1_1", "displacement", row))
            ['MOZ.1_1']

            ```
        - It is rejected for the non-GADM damages table:
            ```python
            >>> from earthlens.flodis import FlodisDataset
            >>> from earthlens.flodis.backend import _normalize_gid
            >>> row = FlodisDataset(file="FLODIS_mortality_damage.csv", key_columns=("disasterno",))
            >>> _normalize_gid("MOZ.1_1", "damages", row)
            Traceback (most recent call last):
                ...
            ValueError: gid= filters the GADM-keyed table, but dataset='damages' is keyed on ['disasterno']. Pass gid= only with dataset='displacement', or filter the damages table by country=.

            ```
    """
    codes = {raw.strip().upper() for raw in _as_list(gid)}
    if codes and "GID_1" not in row.key_columns:
        raise ValueError(
            f"gid= filters the GADM-keyed table, but dataset={dataset!r} is keyed "
            f"on {list(row.key_columns)}. Pass gid= only with "
            "dataset='displacement', or filter the damages table by country=."
        )
    return codes


class FLODIS(AbstractDataSource):
    """FLODIS observed-flood impacts backend (tabular).

    Downloads the selected FLODIS table from its pinned Zenodo release, filters
    it by country / GADM code / date window, and returns a
    :class:`pandas.DataFrame` carrying the join keys (`disasterno` for `damages`,
    `GID_1` / `GID_2` for `displacement`) so a caller can join to the shipped
    `emdat` (GDIS) footprints and `gee` (Global Flood Database) extents.

    The record is public (CC-BY-4.0); no credentials are needed.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is a table of per-event impact
            rows, so the facade rejects `aggregate=`.
        REQUIRES_TIME_WINDOW: `False` — a request without a window returns every
            year the record covers (2000-2018).

    Examples:
        - Pull Mozambique flood-damage events, or the displacement table, through
          the facade (both fetch from Zenodo, so this is illustrative, not a
          doctest):

            ```python
            from earthlens.core import EarthLens

            damages = EarthLens(
                "flodis", dataset="damages", country="MOZ", start="2000", end="2018"
            ).download()  # a pandas.DataFrame keyed on disasterno

            displacement = EarthLens(
                "flodis", dataset="displacement", country="MOZ"
            ).download()  # a pandas.DataFrame keyed on GID_1 / GID_2
            ```
    """

    OUTPUT_KIND: OutputKind = "tabular"

    REQUIRES_TIME_WINDOW = False

    AGGREGATE_REFUSAL_REASON = (
        "FLODIS serves per-event flood impact records (deaths / damages / "
        "displacements matched to observed footprints), not gridded rasters, so "
        "there is no meaningful gridded reduction. Call download() without "
        "aggregate= and post-process the returned DataFrame directly"
    )

    #: Whether the transport should draw a progress bar, set from
    #: `download(progress_bar=...)` so the flag reaches the fetch.
    _progress: bool = True

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        dataset: str = "damages",
        variables: list[str] | None = None,
        country: str | list[str] | None = None,
        gid: str | list[str] | None = None,
        timeout: float = 120.0,
    ):
        """Initialise a FLODIS backend instance.

        Args:
            start: Inclusive start of an optional window, parsed with `fmt`. Only
                its year is significant — FLODIS indexes events by year. `None`
                means "from the beginning of the record" (2000).
            end: Inclusive end of the optional window; `None` means "to the end
                of the record" (2018).
            lat_lim: Accepted for facade parity but not a filter axis — FLODIS
                tables carry no per-row coordinates (footprints come from the
                `emdat` / `gee` backends).
            lon_lim: Accepted for facade parity; see `lat_lim`.
            temporal_resolution: FLODIS issues one query over the whole window,
                so this is the sentinel `"all"`, not a pandas frequency alias.
            path: Output directory for the cached CSV and the written table.
                Created by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            dataset: Which table to fetch — `"damages"` (EM-DAT deaths/damages,
                the default) or `"displacement"` (IDMC displacements).
            variables: FLODIS has no variable axis (`dataset=` selects a whole
                table). Accepted only so the facade can route `dataset=`; a
                non-empty value is rejected.
            country: One ISO3 country code or a list of them (`"MOZ"`,
                `["MOZ", "BGD"]`). `None` keeps every country.
            gid: One GADM code or a list of them, matched against the
                displacement table's `GID_1` / `GID_2`. Only valid with
                `dataset="displacement"` (the damages table is not GADM-keyed).
                `None` keeps every region.
            timeout: Per-request timeout in seconds for the Zenodo download.

        Raises:
            ValueError: If `dataset` is not a registered table, `variables=` is
                non-empty, a `country` value is not a 3-letter ISO3 code, or
                `gid=` is given for the non-GADM `damages` table.
        """
        self._catalog = Catalog()
        if self._catalog.record is None:
            raise ValueError(
                "the FLODIS catalog failed to load its 'record:' block; the "
                "bundled flodis_data_catalog.yaml is malformed."
            )
        self._record = self._catalog.record
        if variables:
            raise ValueError(
                "FLODIS selects a whole table with dataset= and has no variable "
                f"axis; got variables={variables!r}. Use "
                "dataset='damages' | 'displacement'."
            )
        self._dataset_name = dataset
        # Resolve against the catalog (did-you-mean hint on a typo).
        self._dataset: FlodisDataset = self._catalog.dataset(dataset)
        self._country = _normalize_iso3(country)
        self._gid = _normalize_gid(gid, dataset, self._dataset)
        self._timeout = timeout
        self._http: HttpClient | None = None

        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            # FLODIS is facet-only over the wire: the table is chosen by
            # `dataset=`, so no `variables` list is threaded to the base class.
            variables=[],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

    def _create_grid(self, lat_lim: list[float], lon_lim: list[float]) -> SpatialExtent:
        """Capture the requested bounds as a :class:`SpatialExtent`.

        Args:
            lat_lim: `[min_lat, max_lat]`.
            lon_lim: `[min_lon, max_lon]`.

        Returns:
            SpatialExtent: The requested extent (recorded, not a filter axis).
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the optional `[start, end]` window into a :class:`TemporalExtent`.

        FLODIS covers a whole record and is indexed by event year, so `None`
        bounds are legal and yield a `None`-dated extent.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string bound; a non-matching
                string falls back to an ISO-8601 parse.

        Returns:
            TemporalExtent: Frozen model with the parsed (or `None`) endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = to_datetime(start, fmt) if start else None
        end_dt = to_datetime(end, fmt) if end else None
        dates = (
            pd.DatetimeIndex([start_dt, end_dt])
            if start_dt is not None and end_dt is not None
            else pd.DatetimeIndex([])
        )
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=dates,
        )

    @property
    def _year_range(self) -> tuple[int | None, int | None]:
        """Return the requested window as inclusive year bounds.

        Returns:
            tuple[int | None, int | None]: `(first_year, last_year)`, either
                `None` when that end of the window was not given.
        """
        start = self.time.start_date
        end = self.time.end_date
        return (
            start.year if start is not None else None,
            end.year if end is not None else None,
        )

    def _client(self) -> HttpClient:
        """Return this instance's pooled client, building it on first use.

        Zenodo is a single origin; a dropped connection there is a normal event,
        so connection and timeout errors are retried too (matching `hanze`).

        Returns:
            HttpClient: The same instance on every later call.
        """
        if self._http is None:
            self._http = HttpClient(
                timeout=self._timeout,
                retry_on_exceptions=(requests.ConnectionError, requests.Timeout),
            )
        return self._http

    def _load_table(self) -> pd.DataFrame:
        """Download and parse the selected FLODIS table (cached).

        The download is guarded by the shared CSV magic (`,ISO3,`), so an HTML
        error page served with a `200` status is rejected at the download site
        rather than cached under the CSV name and failing confusingly at
        `read_csv` on every later call. The leading unnamed index column FLODIS
        ships (a bare pandas write index) is dropped with `index_col=0`.

        Returns:
            pandas.DataFrame: The full selected table, FLODIS's documented
                headers, with a clean `RangeIndex`.
        """
        record = self._record.record
        entry = self._dataset
        # The pristine download lives in a dedicated sub-directory, never in
        # `root_dir` beside the written output. `download()` writes its filtered
        # result as `flodis_<table>.csv`, which on a case-insensitive filesystem
        # (Windows, default macOS) would be the *same path* as the raw
        # `FLODIS_<table>.csv` for the displacement table — overwriting the
        # pristine cache with an index-stripped copy and corrupting every later
        # read. Separating the two directories makes that collision impossible.
        local = self._source_path()
        if not local.exists():
            logger.info(f"FLODIS: downloading {entry.file} (record {record}).")
            local.parent.mkdir(parents=True, exist_ok=True)
            self._client().download(
                entry.content_url(record),
                local,
                expect_magic=_CSV_MAGIC,
                progress=self._progress,
            )
        return pd.read_csv(local, index_col=0).reset_index(drop=True)

    def _source_path(self) -> Path:
        """Return the cache path for the pristine download of the selected table.

        Kept in a dedicated `flodis_source/` sub-directory so it cannot collide
        with the filtered CSV `download()` writes into `root_dir` (see
        :meth:`_load_table`).

        Returns:
            Path: `root_dir/flodis_source/<file>`.
        """
        return self.root_dir / "flodis_source" / self._dataset.file

    def _filter_table(self, table: pd.DataFrame) -> pd.DataFrame:
        """Apply the request's country / GADM / date filters.

        Args:
            table: The full selected table.

        Returns:
            pandas.DataFrame: The matching rows, index reset.
        """
        columns = self._catalog.columns
        mask = pd.Series(True, index=table.index)

        if self._country:
            mask &= table[columns["iso3"]].astype(str).str.upper().isin(self._country)

        if self._gid:
            # Filter against the dataset's own join-key columns (`GID_1`/`GID_2`),
            # the same source of truth `_normalize_gid` validated against — so a
            # `gid=` that was accepted always has real columns to match, rather
            # than silently dropping every row if the `columns:` map drifted.
            gid_mask = pd.Series(False, index=table.index)
            for col in self._dataset.key_columns:
                if col in table.columns:
                    gid_mask |= table[col].astype(str).str.upper().isin(self._gid)
            mask &= gid_mask

        first_year, last_year = self._year_range
        year_col = columns["year"]
        if first_year is not None:
            mask &= table[year_col] >= first_year
        if last_year is not None:
            mask &= table[year_col] <= last_year

        return table[mask].reset_index(drop=True)

    def _search(self) -> list[RemoteProduct]:
        """Pin the one product to fetch (the selected FLODIS table).

        Returns:
            list[RemoteProduct]: A single product carrying the dataset name and
                record id.
        """
        return [
            RemoteProduct(
                id=f"flodis:{self._dataset_name}",
                metadata={"record": self._record.record, "file": self._dataset.file},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Any]:
        """Download and filter the one product.

        Args:
            products: The single-element list from :meth:`_search`.

        Returns:
            list[Any]: One element — the filtered :class:`pandas.DataFrame`.
        """
        # Single-product backend: the one table is re-derived from instance state
        # (`dataset` + filters), so `products` is accepted for the base
        # search -> fetch contract but carries nothing this method needs to read.
        return [self._filter_table(self._load_table())]

    def _api(self) -> list[Any]:
        """Compose :meth:`_search` and :meth:`_fetch`.

        Returns:
            list[Any]: The fetched result (one element).
        """
        return self._api_via_search_fetch()

    def download(self, progress_bar: bool = True) -> pd.DataFrame:
        """Fetch the selected FLODIS table and return it.

        Runs the download + filter, writes the result to `path` as a CSV, and
        returns it.

        Args:
            progress_bar: Whether to draw a download progress bar. Passed through
                to the transport, so `False` really does silence it.

        Returns:
            pandas.DataFrame: The filtered per-event impact rows, carrying the
                join keys (`disasterno` for `damages`, `GID_1` / `GID_2` for
                `displacement`). Also written under `root_dir`.

        Raises:
            requests.HTTPError: If the Zenodo download returns a non-2xx status.
            ValueError: If the download's body fails its content guard (an HTML
                error page served with a 200 status).
        """
        self._progress = progress_bar
        results = self._api()
        result = cast("pd.DataFrame", results[0])
        self._log_citation()
        out_path = self.root_dir / (self._result_stem() + ".csv")
        result.to_csv(out_path, index=False)
        logger.info(
            f"FLODIS {self._dataset_name}: {len(result)} row(s) written to {out_path}."
        )
        return result

    def _result_stem(self) -> str:
        """Compose an output file stem that encodes the table and its filters.

        A plain `flodis_<table>` for an unfiltered request; otherwise
        `flodis_<table>-<digest>` so two differently-filtered queries into one
        `path=` do not overwrite each other. The digest is order-insensitive in
        the multi-value filters.

        Returns:
            str: `flodis_<table>`, or `flodis_<table>-<8-hex-digest>` when any
                filter is active.
        """
        base = f"flodis_{self._dataset_name}"
        first_year, last_year = self._year_range
        applied = (
            bool(self._country),
            bool(self._gid),
            first_year is not None,
            last_year is not None,
        )
        if not any(applied):
            return base
        request = (
            tuple(sorted(self._country)),
            tuple(sorted(self._gid)),
            self._year_range,
        )
        digest = hashlib.sha1(
            repr(request).encode(), usedforsecurity=False
        ).hexdigest()[:8]
        return f"{base}-{digest}"

    def _log_citation(self) -> None:
        """Log the CC-BY attribution once (info, not a warning)."""
        record = self._record
        if record.attribution:
            logger.info(f"FLODIS source citation: {record.attribution}")
