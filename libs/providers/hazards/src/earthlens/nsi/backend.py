"""Backend for US object-level flood exposure & loss (`earthlens.nsi`).

`NSI(AbstractDataSource)` serves three keyless US-federal REST sources chosen by
a `source=` discriminator — `structures` (USACE National Structure Inventory),
`nfhl` (FEMA National Flood Hazard Layer), and `nfip` (FEMA NFIP redacted claims
v3) — and returns the shape that source declares.

Two design points carry this backend:

* **Per-instance `OUTPUT_KIND` (`G1`).** The resolved source's `output_kind` is
  copied onto `self.OUTPUT_KIND` in `__init__`: `structures`/`nfhl` return a
  pyramids :class:`~pyramids.feature.collection.FeatureCollection` (`vector`),
  `nfip` returns a :class:`pandas.DataFrame` (`tabular`). The
  :class:`earthlens.earthlens.EarthLens` facade reads the instance attribute to
  gate `aggregate=` and to know the return shape.
* **Bounded requests, required (`G3`).** No unbounded national pull: `structures`
  needs a `fips=` or a `[lat_lim, lon_lim]` box, `nfhl` needs the box, and `nfip`
  needs at least one of `state` / `county` / `year` / `flood_event`. NFIP paging
  logs the total record count so a large pull is visible.

All three are US public-domain, keyless — no auth. `aggregate=` is rejected
(these are records, not gridded rasters), and the parse uses no gridded-array
dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pandas as pd
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
from earthlens.nsi import _helpers
from earthlens.nsi.catalog import Catalog, Source
from earthlens.nsi.geometry import (
    arcgis_envelope,
    nsi_polygon_body,
    to_feature_collection,
)

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk output formats for a written tabular result.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Global sentinel bounds used when a source is not selected by a box (a `fips=`
#: structures pull, or a `nfip` attribute query).
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


class NSI(AbstractDataSource):
    """US flood exposure / loss backend (per-instance output).

    Resolves `source=` to its catalog row, issues the one bounded request that
    source needs, and returns a
    :class:`~pyramids.feature.collection.FeatureCollection` (`structures`/`nfhl`)
    or a :class:`pandas.DataFrame` (`nfip`). US-only: a non-US box returns an
    empty result, not an error (`G4`).

    Attributes:
        OUTPUT_KIND: Set **per instance** in :meth:`__init__` from the resolved
            source's `output_kind`. The facade reads it to gate `aggregate=` and
            to know the return shape.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = (
        "NSI/FEMA sources are object-level records (structures, flood zones, "
        "insurance claims), not gridded rasters, so there is no meaningful "
        "gridded reduction. Call download() without aggregate="
    )

    #: These sources are point-in-time inventories / claim records, not a time
    #: series, so a missing `start` / `end` is legal.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "snapshot",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        source: str = "structures",
        fips: str | None = None,
        state: str | None = None,
        county: str | None = None,
        year: int | None = None,
        flood_event: str | None = None,
        max_records: int | None = None,
        output_format: OutputFormat = "csv",
        http_client: HttpClient | None = None,
    ):
        """Initialise an NSI backend instance.

        Args:
            start: Inclusive start of an optional window, parsed with `fmt`;
                `None` allowed (these sources are snapshots, not a time series).
            end: Inclusive end of the optional window; `None` allowed.
            lat_lim: `[min_lat, max_lat]` box — required for `nfhl`, and one of
                the two ways to select `structures` (the other is `fips=`).
            lon_lim: `[min_lon, max_lon]` box; see `lat_lim`.
            temporal_resolution: Recorded as the resolution label only.
            path: Output directory for a written tabular (`nfip`) result.
            fmt: `strptime` format for `start` / `end`.
            source: Which source to query — `"structures"` (default), `"nfhl"`,
                or `"nfip"`.
            fips: A 2/5/11/15-digit FIPS code selecting `structures` by
                state/county/tract/block.
            state: Two-letter state code selecting `nfip` claims.
            county: 5-digit county FIPS selecting `nfip` claims.
            year: Loss year selecting `nfip` claims.
            flood_event: Named flood event selecting `nfip` claims.
            max_records: Optional cap on the number of `nfip` records fetched.
            output_format: On-disk format for the `nfip` table — `"csv"`
                (default) or `"parquet"`.
            http_client: An :class:`~earthlens.base.http.HttpClient` to use for
                every request. Defaults to a fresh client; injectable so the
                whole path is testable with a fake transport.

        Raises:
            ValueError: If `source` is unknown, `output_format` is
                unrecognised, or the source's required bound is missing (`G3`).
        """
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )

        self._catalog = Catalog()
        self._source: Source = self._catalog.get(source)
        self._fips = fips
        self._state = state
        self._county = county
        self._year = year
        self._flood_event = flood_event
        self._max_records = max_records
        self._output_format: OutputFormat = output_format
        self._http: HttpClient = (
            http_client if http_client is not None else HttpClient()
        )

        # G1 — the per-instance output shape comes from the resolved source.
        self.OUTPUT_KIND = self._source.output_kind

        self._has_box = lat_lim is not None and lon_lim is not None
        self._lat_lim = lat_lim if lat_lim is not None else _GLOBAL_LAT
        self._lon_lim = lon_lim if lon_lim is not None else _GLOBAL_LON
        self._validate_bound()

        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=[self._source.id],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

    def _validate_bound(self) -> None:
        """Enforce the required spatial/attribute bound for the source (`G3`).

        Raises:
            ValueError: If `structures` has neither `fips=` nor a box, `nfhl`
                has no box, or `nfip` has no `state`/`county`/`year`/`flood_event`.
        """
        provider = self._source.provider
        if provider == "nsi" and not (self._fips or self._has_box):
            raise ValueError(
                "source='structures' needs a fips= (2/5/11/15-digit) or a "
                "[lat_lim, lon_lim] box; an unbounded national pull is refused."
            )
        if provider == "fema-arcgis" and not self._has_box:
            raise ValueError(
                "source='nfhl' needs a [lat_lim, lon_lim] box (the ArcGIS query "
                "envelope); an unbounded national pull is refused."
            )
        if provider == "openfema" and not (
            self._state or self._county or self._year or self._flood_event
        ):
            raise ValueError(
                "source='nfip' needs at least one of state=, county=, year=, "
                "flood_event=; an unbounded national pull is refused."
            )

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Return the request's :class:`SpatialExtent`.

        Args:
            lat_lim: `[min_lat, max_lat]` (global sentinel when unbounded).
            lon_lim: `[min_lon, max_lon]`.

        Returns:
            SpatialExtent: The extent for the resolved box (or the globe).
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

        Dates are not part of the request (these sources are snapshots), so
        `None` bounds are allowed and yield a `None`-dated extent.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string `start` / `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed (or `None`) endpoints.
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

    def _search(self) -> list[RemoteProduct]:
        """Pin the one product to fetch (the resolved source).

        Returns:
            list[RemoteProduct]: A single product identified by the source name.
        """
        return [RemoteProduct(id=self._source.id, metadata={})]

    def _fetch(self, products: list[RemoteProduct]) -> list:
        """Route the product to its source and parse the response.

        Args:
            products: The list from :meth:`_search` (one product).

        Returns:
            list: One element — a `FeatureCollection` (`structures`/`nfhl`) or a
                `DataFrame` (`nfip`).
        """
        return [self._fetch_source()]

    def _fetch_source(self):
        """Fetch and parse the resolved source.

        Returns:
            A `FeatureCollection` (vector) or a `DataFrame` (tabular).

        Raises:
            requests.HTTPError: If the upstream source returns a non-2xx status.
        """
        provider = self._source.provider
        if provider == "nsi":
            return self._fetch_structures()
        if provider == "fema-arcgis":
            return self._fetch_nfhl()
        return self._fetch_nfip()

    def _fetch_structures(self) -> FeatureCollection:
        """Fetch NSI structures by `fips=` (GET) or a box (POST polygon)."""
        endpoint = self._source.endpoint
        if self._fips:
            geojson = self._http.get_json(endpoint, params={"fips": self._fips})
        else:
            body = nsi_polygon_body(self._lat_lim, self._lon_lim)
            geojson = self._http.post(endpoint, json=body).json()
        return to_feature_collection(geojson)

    def _fetch_nfhl(self) -> FeatureCollection:
        """Fetch FEMA NFHL flood zones for the box via the ArcGIS query."""
        url = f"{self._source.endpoint}/{self._source.layer_id}/query"
        params = arcgis_envelope(self._lat_lim, self._lon_lim)
        geojson = self._http.get_json(url, params=params)
        return to_feature_collection(geojson)

    def _fetch_nfip(self) -> pd.DataFrame:
        """Fetch NFIP claims for the attribute filter, paged, as a `DataFrame`."""
        source = self._source
        filter_str = _helpers.odata_filter(
            state=self._state,
            county=self._county,
            year=self._year,
            flood_event=self._flood_event,
        )
        total = _helpers.nfip_count(self._http, source.endpoint, filter_str=filter_str)
        logger.info(
            f"NSI nfip: {total} claim record(s) match {filter_str!r}"
            + (f"; capping at {self._max_records}" if self._max_records else "")
        )
        records = _helpers.paginate_nfip(
            self._http,
            source.endpoint,
            cast("str", source.records_key),
            filter_str=filter_str,
            page_size=source.page_size or 1000,
            max_records=self._max_records,
        )
        return _helpers.records_to_frame(records, source.fields)

    def download(
        self,
        progress_bar: bool = True,
    ) -> pd.DataFrame | FeatureCollection:
        """Fetch the selected source and return its per-instance shape.

        Args:
            progress_bar: Accepted for signature parity; one logical request is
                issued (paged for `nfip`), so this is a no-op.

        Returns:
            A :class:`~pyramids.feature.collection.FeatureCollection` for
            `structures`/`nfhl`, or a :class:`pandas.DataFrame` (also written to
            `root_dir`) for `nfip`.

        Raises:
            requests.HTTPError: If the upstream source returns a non-2xx status.
        """
        results = self._api()
        result = results[0]
        self._log_citation()
        if self.OUTPUT_KIND == "vector":
            logger.info(
                f"NSI {self._source.id}: returned a FeatureCollection "
                f"({len(result)} feature(s))."
            )
            return result
        out_path = self._write_table(result)
        if len(result):
            logger.info(
                f"NSI {self._source.id}: {len(result)} row(s) written to {out_path}."
            )
        else:
            logger.warning(
                f"NSI {self._source.id}: no rows matched; wrote an empty "
                f"(schema-only) table to {out_path}."
            )
        return result

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write a tabular result to `root_dir` and return the path.

        Args:
            df: The result frame.

        Returns:
            Path: The written CSV / Parquet file path.

        Raises:
            ImportError: If `output_format="parquet"` but `pyarrow` is missing.
        """
        ext = "parquet" if self._output_format == "parquet" else "csv"
        out_path = self.root_dir / f"nsi_{self._source.id}.{ext}"
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

    def _log_citation(self) -> None:
        """Log the resolved source's citation once (info, not a warning)."""
        citation = self._source.citation
        if citation:
            logger.info(f"NSI source citation: {citation}")
