"""Backend that queries the IUCN Red List v4 API for assessments (tabular).

`IUCN(AbstractDataSource)` fetches Red List assessment records — category,
criteria, population trend, year — from the IUCN Red List v4 API through a
thin direct `requests` shim (`earthlens.iucn._rest`); there is no mature
Python v4 client. v4 authenticates with an `Authorization: Bearer <token>`
header (resolved by :class:`IucnAuth`); the retired v3 `?token=` query param
is gone.

This is the cluster's only `tabular` backend (`OUTPUT_KIND = "tabular"`):
the result is an assessment table, not geometry, so `download()` returns a
`pandas.DataFrame` and the facade rejects an `aggregate=` argument. Each
entry in `variables` selects a species (`species:<binomial>`, resolved via
the two-step taxa→assessment flow) or a country (`country:<ISO2>`).

Red List data is CC-BY-NC and may not be redistributed without a written
IUCN waiver, so every fetch raises a `LicenseWarning` and the data is never
shipped as package data — the token stays user-supplied.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

import pandas as pd
from earthlens.iucn.auth import IucnAuth, IucnCredentials
from earthlens.iucn.catalog import Catalog
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.biodiversity import IUCN_LICENSE, warn_license
from earthlens.iucn import _rest

FileFormat = Literal["csv", "parquet"]

#: Prefix marking a species (binomial) selector in `variables`.
SPECIES_PREFIX = "species:"

#: Prefix marking a country (ISO alpha-2) selector in `variables`.
COUNTRY_PREFIX = "country:"


class IUCN(AbstractDataSource):
    """IUCN Red List v4 assessment backend (tabular output).

    Wraps the IUCN Red List v4 API so a user can pull species or country
    assessment records through the same `download()` shape every other
    earthlens backend uses. Each entry in `variables` is a
    `species:<binomial>` (resolved via the two-step taxa→assessment flow)
    or a `country:<ISO2>` code; the assessment rows are returned as a
    `pandas.DataFrame`.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the facade rejects `aggregate=`.

    Examples:
        - Build a backend for a species query (token is resolved early
          but no HTTP call is made until `download()`):
            ```python
            >>> from earthlens.iucn import IUCN
            >>> backend = IUCN(
            ...     start="2024-01-01", end="2024-12-31",
            ...     variables=["species:Panthera leo"],
            ...     lat_lim=[-90.0, 90.0], lon_lim=[-180.0, 180.0],
            ...     token="placeholder-token",
            ... )
            >>> backend.OUTPUT_KIND
            'tabular'
            >>> backend.vars
            ['species:Panthera leo']

            ```
    """

    OUTPUT_KIND: OutputKind = "tabular"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list,
        lat_lim: list,
        lon_lim: list,
        temporal_resolution: str = "all",
        path: str = "",
        fmt: str = "%Y-%m-%d",
        token: str | None = None,
        file_format: FileFormat = "csv",
    ):
        """Configure an IUCN Red List assessment query.

        Args:
            start: Inclusive start date string (parsed with `fmt`); the Red
                List API is not time-filtered, so this only frames the
                request.
            end: Inclusive end date string (parsed with `fmt`).
            variables: Selectors — `"species:<binomial>"` (e.g.
                `"species:Panthera leo"`) or `"country:<ISO2>"` (e.g.
                `"country:KE"`). Must be non-empty.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in degrees.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in degrees.
            temporal_resolution: The Red List is not time-chunked, so this
                is the sentinel `"all"`.
            path: Output directory for the assessment table. The empty
                string (the default) opts out of writing — `download()`
                returns the in-memory DataFrame without touching the
                filesystem. Pass an explicit directory to write the file.
            fmt: `strptime` format for `start` / `end`.
            token: IUCN Red List v4 token; falls back to the `IUCN_TOKEN`
                environment variable.
            file_format: Output table format — `"csv"` (default) or
                `"parquet"`.

        Raises:
            TypeError: If `variables` is a mapping.
            ValueError: If `variables` is empty or `file_format` unknown.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "IUCN `variables` must be a list of selectors (e.g. "
                "['species:Panthera leo'] or ['country:KE']), not a mapping."
            )
        if not list(variables):
            raise ValueError(
                "IUCN `variables` must name at least one species "
                "(species:<binomial>) or country (country:<ISO2>)."
            )
        if file_format not in ("csv", "parquet"):
            raise ValueError(
                f"file_format must be 'csv' or 'parquet', got {file_format!r}."
            )
        self._file_format: FileFormat = file_format
        self._token_arg = token
        self._auth: IucnAuth | None = None
        # Preserve the user's original `path` so download() can honour
        # `path=""` as "do not write a file".
        self._user_path = path
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """Build and configure the token auth (surfaces a missing token early).

        Returns:
            None: No long-lived client; `_rest` builds a session per call.

        Raises:
            AuthenticationError: When no `IUCN_TOKEN` / `token=` is set.
        """
        self._auth = IucnAuth(IucnCredentials(token=self._token_arg))
        self._auth.configure()
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse `[start, end]` into a :class:`TemporalExtent`.

        The Red List API is not time-filtered, so the window only frames the
        request and the resolution is the sentinel `"all"`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: The sentinel `"all"`.
            fmt: `strptime` format for both ends.

        Returns:
            TemporalExtent: The validated `[start, end]` window.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _fetch(self) -> pd.DataFrame:
        """Fetch every selector's assessments into one DataFrame.

        Routes each `variables` entry to the species (two-step) or country
        v4 path, concatenates the rows, and warns the CC-BY-NC license.

        Returns:
            pd.DataFrame: The assessment rows, columns per
                `_rest.IUCN_COLUMNS`; empty (schema-only) when nothing
                matched.
        """
        token = self._auth.token
        rows: list[dict] = []
        for selector in self.vars:
            rows.extend(self._fetch_one(token, selector))
        if rows:
            warn_license(
                IUCN_LICENSE,
                "iucn",
                detail="CC-BY-NC; redistribution needs a written IUCN waiver",
            )
        return _frame(rows)

    def _fetch_one(self, token: str, selector: str) -> list[dict]:
        """Fetch one selector (species binomial or country ISO2).

        Args:
            token: The resolved IUCN token.
            selector: A `"species:<binomial>"` or `"country:<ISO2>"` string.

        Returns:
            list[dict]: The selector's assessment rows.

        Raises:
            ValueError: If the selector has no recognised prefix.
        """
        text = selector.strip()
        if text.lower().startswith(SPECIES_PREFIX):
            genus, species = _split_binomial(text[len(SPECIES_PREFIX) :].strip())
            return _rest.fetch_species(token, genus, species)
        if text.lower().startswith(COUNTRY_PREFIX):
            return _rest.fetch_country(token, self._catalog.resolve_iso2(text))
        raise ValueError(
            f"IUCN selector {selector!r} must start with 'species:' "
            "(a binomial) or 'country:' (an ISO alpha-2 code)."
        )

    def _api(self) -> pd.DataFrame:
        """Fetch assessments (satisfies the abstract contract)."""
        return self._fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate=None,
    ) -> pd.DataFrame:
        """Fetch the assessment records and return them as a DataFrame.

        Args:
            progress_bar: Accepted for signature parity; the REST shim has
                no progress bar, so this is a no-op.
            aggregate: Must be `None`. Assessments are tabular, not gridded;
                the facade already rejects a non-`None` `aggregate=` for a
                `tabular` backend.

        Returns:
            pd.DataFrame: The assessment rows. Written to a CSV/Parquet file
                under `path` when `path` is set and rows are present.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "IUCN.download(aggregate=...) is not supported: Red List "
                "assessments are tabular records, not gridded rasters. Call "
                "download() without aggregate= and post-process the returned "
                "DataFrame directly."
            )
        frame = self._fetch()
        if self._user_path and len(frame):
            written = self._write(frame)
            logger.info(
                f"IUCN download summary: {len(frame)} assessment(s) written to {written}"
            )
        else:
            logger.info(
                f"IUCN download summary: {len(frame)} assessment(s), nothing written"
            )
        return frame

    def _write(self, frame: pd.DataFrame) -> Path:
        """Write the assessments to a CSV/Parquet file under `root_dir`.

        Args:
            frame: The assessment DataFrame.

        Returns:
            Path: Absolute path of the file written.
        """
        ext = "parquet" if self._file_format == "parquet" else "csv"
        out_path = self.root_dir / f"iucn_assessments.{ext}"
        if self._file_format == "parquet":
            frame.to_parquet(out_path)
        else:
            frame.to_csv(out_path, index=False)
        return out_path


def _split_binomial(name: str) -> tuple[str, str]:
    """Split a scientific binomial into genus and species epithet.

    Args:
        name: A binomial such as `"Panthera leo"`.

    Returns:
        A `(genus, species)` tuple; `species` is `""` for a bare genus.
    """
    parts = name.split()
    genus = parts[0] if parts else ""
    species = " ".join(parts[1:]) if len(parts) > 1 else ""
    return genus, species


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Assemble assessment rows into a typed DataFrame.

    Args:
        rows: Row dicts keyed by `_rest.IUCN_COLUMNS`.

    Returns:
        pd.DataFrame: One row per assessment with the declared columns and
            dtypes; empty input yields a schema-correct empty frame.
    """
    columns = _rest.IUCN_COLUMNS
    if not rows:
        return pd.DataFrame({c: pd.Series([], dtype=t) for c, t in columns.items()})
    frame = pd.DataFrame(rows, columns=list(columns))
    for column, dtype in columns.items():
        frame[column] = frame[column].astype(dtype)
    return frame
