"""Backend that fetches country/admin-indexed risk indicators.

`RiskIndicators(AbstractDataSource)` routes one request to whichever of three
risk sources the chosen dataset names — GFDRR ThinkHazard!, INFORM Risk (JRC),
or the Global Forest Watch Data API — and returns the result in the shape that
dataset declares. A request is one dataset id (`variables=["thinkhazard:flood_river"]`)
plus a country selector (`country="KEN"` ISO3, or a raw `admin_code=`); GFW also
accepts the country to build its SQL / geostore call.

Two design points carry this backend:

* **Per-instance `OUTPUT_KIND` (`G1`).** The resolved dataset's `output_kind`
  is copied onto `self.OUTPUT_KIND` in `__init__`: `tabular` returns a
  :class:`pandas.DataFrame`, `vector` returns a pyramids
  :class:`~pyramids.feature.collection.FeatureCollection`. The
  :class:`earthlens.earthlens.EarthLens` facade reads the instance attribute to
  gate `aggregate=` (rejected for both) and to know the return shape.
* **Conditional, per-source auth (`G3`).** Only a dataset whose provider is
  `gfw` builds and configures a :class:`GfwAuth`; ThinkHazard and INFORM stay
  keyless. A missing GFW key surfaces as an `AuthenticationError` naming
  `GFW_API_KEY` on construction of a gfw request.

These are pre-computed indices / SQL queries, so `aggregate=` is rejected and
the parse uses no gridded-array dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pandas as pd
from loguru import logger
from pydantic import SecretStr

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
    to_datetime,
)
from earthlens.config import cache_dir as _shared_cache_dir
from earthlens.risk_indicators import _helpers
from earthlens.risk_indicators.auth import GfwAuth, GfwCredentials
from earthlens.risk_indicators.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk output formats for a written tabular result.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

InformSource = Literal["auto", "release", "api"]

#: Where an INFORM dataset reads its scores. The published release workbook is
#: the current release; the Scores API serves the workflow the catalog pins.
INFORM_SOURCES: tuple[str, ...] = ("auto", "release", "api")

#: Global sentinel bounds — risk indicators are country/admin-indexed, not
#: gridded, so the spatial extent is the whole globe (`G7`).
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


def _resolve_ids(variables: list[str] | None) -> list[str]:
    """Reduce `variables` to the single dataset id this instance serves.

    Args:
        variables: The `variables=` argument as passed.

    Returns:
        list[str]: A one-element list holding the dataset id.

    Raises:
        TypeError: If `variables` is a mapping (the other backends accept one;
            this one takes a list of a single id).
        ValueError: If it does not name exactly one dataset.
    """
    if isinstance(variables, dict):
        raise TypeError(
            "RiskIndicators `variables` must be a one-element list naming "
            "the dataset id (e.g. ['thinkhazard:flood_river']), not a mapping."
        )
    ids = list(dict.fromkeys(variables)) if variables else []
    if len(ids) != 1:
        raise ValueError(
            "RiskIndicators needs exactly one dataset id in variables= "
            "(OUTPUT_KIND is per instance); got "
            f"{ids!r}. Available: {Catalog().available()}."
        )
    return ids


def _validate_options(
    source: InformSource, output_format: OutputFormat, workflow_id: int | None
) -> None:
    """Check the per-request options that do not depend on the resolved dataset.

    Args:
        source: The requested INFORM channel.
        output_format: The requested on-disk format.
        workflow_id: The requested INFORM WorkflowId override.

    Raises:
        ValueError: If `source` or `output_format` is unrecognised, or
            `workflow_id` is not a positive integer.
    """
    if source not in INFORM_SOURCES:
        raise ValueError(
            f"source must be one of {list(INFORM_SOURCES)}, got {source!r}."
        )
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(
            f"output_format must be one of {list(OUTPUT_FORMATS)}, "
            f"got {output_format!r}."
        )
    # The id goes straight into the query string, where a string, a float or a
    # non-positive number would 200 with an empty body rather than fail -
    # indistinguishable from a withdrawn workflow. bool is an int subclass.
    if workflow_id is not None and (
        isinstance(workflow_id, bool)
        or not isinstance(workflow_id, int)
        or workflow_id <= 0
    ):
        raise ValueError(
            f"workflow_id must be a positive INFORM WorkflowId integer, got "
            f"{workflow_id!r}."
        )


class RiskIndicators(AbstractDataSource):
    """Country/admin-indexed risk-indicator backend (mixed output).

    Resolves one dataset id to its catalog row, routes the request to that
    row's provider (ThinkHazard / INFORM / GFW), and returns a
    :class:`pandas.DataFrame` (`tabular`) or a
    :class:`~pyramids.feature.collection.FeatureCollection` (`vector`) per the
    row's `output_kind`. The query is a search/fetch split: :meth:`_search`
    pins the one product (dataset + resolved selector), and :meth:`_fetch`
    issues the provider call and parses it.

    Auth is conditional: only a `gfw` dataset builds a :class:`GfwAuth` (`G3`);
    ThinkHazard / INFORM need no credentials. `aggregate=` is rejected — these
    are pre-computed indices, not gridded rasters (`G8`).

    An INFORM row has two channels: the release workbook JRC publishes on its
    results page — the current release, and the default for the rows the
    workbook covers — and the Scores API, which serves one model release per
    request, identified by a WorkflowId. `source=` picks the channel and
    `workflow_id=` picks the API's release; every returned row records which
    channel produced it. The split exists because the two disagree — the API
    stopped serving the 2026 workflows while the results page kept publishing
    the 2026 release.

    Attributes:
        OUTPUT_KIND: Set **per instance** in :meth:`__init__` from the resolved
            dataset's `output_kind` (`"tabular"` or `"vector"`). The facade
            reads it to gate `aggregate=` and to know the return shape.

    Examples:
        - Resolve an INFORM dataset and read the shape it will return:
            ```python
            >>> from earthlens.risk_indicators import RiskIndicators
            >>> backend = RiskIndicators(variables=["inform:risk"], country="KEN")
            >>> backend.OUTPUT_KIND
            'tabular'
            >>> backend.vars
            ['inform:risk']

            ```
        - A GFW geometry dataset resolves to the vector shape instead:
            ```python
            >>> from earthlens.risk_indicators import RiskIndicators
            >>> backend = RiskIndicators(
            ...     variables=["gfw:admin_boundary"], country="KEN", api_key="k"
            ... )
            >>> backend.OUTPUT_KIND
            'vector'

            ```
        - A workflow id outside the valid domain is refused before it reaches
          the query string, where INFORM would answer 200 with an empty body:
            ```python
            >>> from earthlens.risk_indicators import RiskIndicators
            >>> RiskIndicators(
            ...     variables=["inform:risk"], country="KEN", workflow_id=0
            ... )
            Traceback (most recent call last):
                ...
            ValueError: workflow_id must be a positive INFORM WorkflowId integer, got 0.

            ```
        - Forcing the workbook on the row it does not cover is refused at
          construction, not at download time:
            ```python
            >>> from earthlens.risk_indicators import RiskIndicators
            >>> RiskIndicators(
            ...     variables=["inform:climate_risk"], country="KEN", source="release"
            ... )
            Traceback (most recent call last):
                ...
            ValueError: source='release' is not available for 'inform:climate_risk': the INFORM Risk release workbook does not carry it. Use source='api'.

            ```
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "risk indicators are pre-computed country-indexed indices / queries, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate="

    #: The indicator tables are a snapshot with no time axis, so a missing `start` /
    #: `end` is legal here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        variables: list[str] | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "annual",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        country: str | None = None,
        admin_code: str | None = None,
        api_key: str | None = None,
        workflow_id: int | None = None,
        source: InformSource = "auto",
        cache_dir: Path | str | None = None,
        output_format: OutputFormat = "csv",
    ):
        """Initialise a risk-indicators backend instance.

        Args:
            start: Inclusive start of an optional window, parsed with `fmt`;
                `None` is allowed (these datasets are country-indexed, not a
                time series).
            end: Inclusive end of the optional window; `None` allowed.
            variables: A one-element list naming the dataset id
                (`["thinkhazard:flood_river"]`). Exactly one dataset is
                resolved per instance, because `OUTPUT_KIND` is per instance.
            lat_lim: Accepted for signature parity and ignored — the request is
                country/admin-indexed (`G7`).
            lon_lim: Accepted for signature parity and ignored.
            temporal_resolution: Recorded as the resolution label only.
            path: Output directory for a written tabular result.
            fmt: `strptime` format for `start` / `end`.
            country: ISO3 country code selector (`"KEN"`). Resolved to a
                ThinkHazard ADM0 code for ThinkHazard, used as the SQL / geostore
                key for GFW, and filters INFORM to one country.
            admin_code: A raw ThinkHazard division code, bypassing the ISO3 ->
                code resolution (enables sub-national ThinkHazard divisions).
            api_key: The GFW `x-api-key`; only consulted for a `gfw` dataset.
                Falls back to the `GFW_API_KEY` env var.
            workflow_id: An INFORM model WorkflowId overriding the catalog pin;
                only consulted for an `inform` dataset read from the API. Use it
                to read a release other than the pinned one (`/workflows` lists
                every WorkflowId). Implies `source="api"` when `source` is
                `"auto"`.
            source: Where an `inform` dataset reads its scores. `"auto"` (the
                default) reads the published release workbook when the row
                declares a `release_column` — the current release — and the API
                otherwise. `"release"` forces the workbook, `"api"` forces the
                Scores endpoint and the pinned (or overridden) workflow.
            cache_dir: Directory for the downloaded release workbook. Defaults
                to `risk_indicators/` under the shared earthlens cache
                (`set_cache_dir()` / `EARTHLENS_CACHE`), not under `path`.
            output_format: On-disk format for a tabular result — `"csv"`
                (default) or `"parquet"`.

        Raises:
            TypeError: If `variables` is a mapping (pass a list of one id).
            ValueError: If `variables` is not exactly one dataset id, if
                `output_format` or `source` is unrecognised, if `workflow_id`
                is not a positive integer, if `source="release"` names a dataset
                the release workbook does not cover, or if the required country
                / admin selector for the resolved provider is missing (`G7`).
            AuthenticationError: For a `gfw` dataset when no key resolves (`G3`).
        """
        ids = _resolve_ids(variables)
        _validate_options(source, output_format, workflow_id)

        self._catalog = Catalog()
        self._dataset: Dataset = self._catalog.get(ids[0])
        self._country = country
        self._admin_code = admin_code
        self._api_key = api_key
        self._workflow_id = workflow_id
        self._source: InformSource = source
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        # Row count of the last INFORM payload, before the country filter, so an
        # empty result can say which of the two causes produced it.
        self._upstream_rows: int | None = None
        self._output_format: OutputFormat = output_format
        self._auth: GfwAuth | None = None

        # G1 — the per-instance output shape comes from the resolved dataset.
        self.OUTPUT_KIND = self._dataset.output_kind

        self._validate_selector()
        self._validate_source(workflow_id)

        if workflow_id is not None and self._dataset.provider != "inform":
            logger.warning(
                f"RiskIndicators {self._dataset.id}: workflow_id={workflow_id} "
                f"applies to INFORM datasets only, and this row is a "
                f"{self._dataset.provider} one - the request ignores it."
            )

        # G3 — only a GFW dataset needs (and builds) auth. Configuring here so a
        # missing key fails fast at construction of a gfw request.
        if self._dataset.provider == "gfw":
            self._auth = GfwAuth(
                GfwCredentials(
                    api_key=SecretStr(api_key) if api_key is not None else None
                )
            )
            self._auth.configure()

        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=ids,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

    def _validate_source(self, workflow_id: int | None) -> None:
        """Reject a `source=` the resolved dataset cannot honour.

        Checked at construction rather than at download time, so a request that
        can never work fails before any network call.

        Args:
            workflow_id: The override as passed, so a contradiction with
                `source="release"` (a workbook has no workflow) is caught here.

        Raises:
            ValueError: If `source="release"` names a dataset the release
                workbook does not cover, or is combined with a `workflow_id`.
        """
        if self._source != "release":
            return
        if self._dataset.release_column is None:
            raise ValueError(
                f"source='release' is not available for {self._dataset.id!r}: "
                "the INFORM Risk release workbook does not carry it. Use "
                "source='api'."
            )
        if workflow_id is not None:
            raise ValueError(
                "source='release' reads the published workbook, which has no "
                f"workflow; workflow_id={workflow_id} names an API release. Pass "
                "one or the other."
            )

    def _validate_selector(self) -> None:
        """Check the request carries the selector the resolved provider needs.

        ThinkHazard and GFW require a `country=` (or, for ThinkHazard, a raw
        `admin_code=`); INFORM may omit it (then every country is returned).

        Raises:
            ValueError: When a required selector is missing (`G7`).
        """
        provider = self._dataset.provider
        if provider == "thinkhazard" and not (self._country or self._admin_code):
            raise ValueError(
                f"dataset {self._dataset.id!r} (ThinkHazard) needs country= "
                "(ISO3, e.g. country='KEN') or a raw admin_code=."
            )
        if provider == "gfw":
            iso = (self._country or "").strip()
            if not (len(iso) == 3 and iso.isalpha()):
                raise ValueError(
                    f"dataset {self._dataset.id!r} (GFW) needs country= as a "
                    f"3-letter ISO3 code (e.g. country='KEN'); got "
                    f"{self._country!r}."
                )

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Return a global :class:`SpatialExtent` (spatial args ignored, `G7`).

        Args:
            lat_lim: Ignored.
            lon_lim: Ignored.

        Returns:
            SpatialExtent: The whole-globe extent.
        """
        return SpatialExtent.from_pairs(lat_lim=_GLOBAL_LAT, lon_lim=_GLOBAL_LON)

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the optional `[start, end]` window into a :class:`TemporalExtent`.

        Dates are not part of the request (these datasets are country-indexed),
        so `None` bounds are allowed and yield a `None`-dated extent.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

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

    def _search(self) -> list[RemoteProduct]:
        """Pin the one product to fetch (dataset + resolved selector).

        Returns:
            list[RemoteProduct]: A single product whose `metadata` carries the
                resolved `country` and ThinkHazard `admin_code`.
        """
        return [
            RemoteProduct(
                id=self._dataset.id,
                metadata={"country": self._country, "admin_code": self._admin_code},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list:
        """Route each product to its provider and parse the response.

        Args:
            products: The list from :meth:`_search` (one product).

        Returns:
            list: One element per product — a :class:`pandas.DataFrame` for a
                `tabular` dataset, a
                :class:`~pyramids.feature.collection.FeatureCollection` for a
                `vector` one.
        """
        return [self._fetch_one(product) for product in products]

    def _fetch_one(self, product: RemoteProduct):
        """Fetch and parse one product per its provider.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            A `DataFrame` (tabular) or `FeatureCollection` (vector).

        Raises:
            requests.HTTPError: If the upstream source returns a non-2xx status.
        """
        dataset = self._dataset
        country = product.metadata.get("country")
        admin_code = product.metadata.get("admin_code")
        # Cleared per fetch: the count describes the request in flight, so a
        # reused instance cannot diagnose this result from the previous one.
        self._upstream_rows = None
        if dataset.provider == "thinkhazard":
            code = admin_code or self._catalog.resolve_admin(cast("str", country))
            payload = _helpers.thinkhazard_query(code, dataset.hazard)
            return _helpers.thinkhazard_to_frame(
                payload, admin_code=code, hazard=dataset.hazard, country=country
            )
        if dataset.provider == "inform":
            if self._reads_release:
                return self._fetch_inform_release(country)
            workflow_id = self._resolved_workflow_id
            if workflow_id is None:
                raise ValueError(
                    f"INFORM dataset {dataset.id!r} resolves to no workflow id; "
                    "the catalog row is missing workflow_id and none was passed."
                )
            payload = _helpers.inform_query(
                workflow_id, cast("str", dataset.indicator_id)
            )
            frame = _helpers.inform_to_frame(
                payload, country=country, workflow_id=workflow_id
            )
            self._upstream_rows = frame.attrs.get("served_rows", len(payload))
            return frame
        # provider == "gfw" — GFW keys on upper-case ISO3, so normalise the
        # country before interpolating it (the other two providers already
        # resolve / filter case-insensitively).
        assert self._auth is not None  # a gfw dataset always builds auth in __init__
        api_key = self._auth.api_key
        iso = cast("str", country).strip().upper()
        if dataset.output_kind == "vector":
            payload = _helpers.gfw_geostore(iso, api_key=api_key)
            return _helpers.gfw_geostore_to_feature_collection(payload)
        sql = cast("str", dataset.sql_template).format(iso=iso)
        payload = _helpers.gfw_query(
            cast("str", dataset.gfw_dataset),
            cast("str", dataset.gfw_version),
            sql,
            api_key=api_key,
        )
        return _helpers.to_frame(payload)

    def download(
        self,
        progress_bar: bool = True,
    ) -> pd.DataFrame | FeatureCollection:
        """Fetch the dataset and return its per-instance shape.

        Args:
            progress_bar: Accepted for signature parity; one request is issued,
                so this is a no-op.

        Returns:
            A :class:`pandas.DataFrame` (written to `root_dir` too) for a
            `tabular` dataset, or an in-memory
            :class:`~pyramids.feature.collection.FeatureCollection` for a
            `vector` one.

        Raises:
            requests.HTTPError: If the upstream source returns a non-2xx status.
        """
        results = self._api()
        result = results[0]
        self._log_citation()
        if self.OUTPUT_KIND == "vector":
            logger.info(
                f"RiskIndicators {self._dataset.id}: returned a "
                f"FeatureCollection ({len(result)} feature(s))."
            )
            return result
        out_path = self._write_table(result)
        if len(result):
            logger.info(
                f"RiskIndicators {self._dataset.id}: {len(result)} row(s) "
                f"written to {out_path}."
            )
        else:
            logger.warning(
                f"RiskIndicators {self._dataset.id}: no rows matched; wrote an "
                f"empty (schema-only) table to {out_path}.{self._empty_hint()}"
            )
        return result

    @property
    def _cache_root(self) -> Path:
        """Directory holding the downloaded release workbook.

        Defaults to `risk_indicators/` under the shared earthlens cache
        (`set_cache_dir()` / `EARTHLENS_CACHE`), overridden by `cache_dir=`. The
        workbook is a reusable ~2.5 MB download shared by the four Risk
        datasets, so it belongs in the cache rather than beside the results.
        """
        return self._cache_dir or (_shared_cache_dir() / "risk_indicators")

    @property
    def _reads_release(self) -> bool:
        """Whether this request reads the published workbook rather than the API.

        `"auto"` prefers the workbook for a row the release covers, because that
        is the current published release — but an explicit `workflow_id=` names
        an API workflow, so it keeps the API. `"release"` and `"api"` force the
        choice; the combinations neither channel can honour are rejected in
        :meth:`_validate_source`, so this only reports the decision.

        Returns:
            bool: True when the release workbook is the source for this request.
        """
        if self._dataset.provider != "inform":
            return False
        covered = self._dataset.release_column is not None
        if self._source == "release":
            return covered
        if self._source == "api":
            return False
        return covered and self._workflow_id is None

    def _fetch_inform_release(self, country: str | None) -> pd.DataFrame:
        """Read this dataset's scores from the published release workbook.

        Discovers the newest workbook on the JRC results page, caches the
        download under :attr:`_cache_root`, and reshapes one score column into
        the canonical table.

        Args:
            country: ISO3 to filter to, or `None` for every country.

        Returns:
            pd.DataFrame: Columns `_helpers.INFORM_COLUMNS`.

        Raises:
            requests.RequestException: If the page or the workbook cannot be
                fetched after retries.
            ValueError: If the page carries no workbook link, or the workbook
                has no matching sheet / column.
        """
        url, year = _helpers.inform_release_url()
        target = self._cache_root / url.rsplit("/", 1)[-1]
        if not _helpers.is_xlsx(target):
            logger.info(
                f"RiskIndicators {self._dataset.id}: downloading the INFORM {year} "
                f"release workbook to {target} (a few MB, cached for reuse)."
            )
        workbook = _helpers.inform_download_release(url, target)
        logger.info(
            f"RiskIndicators {self._dataset.id}: reading the INFORM {year} "
            f"release workbook ({workbook.name})."
        )
        scores = _helpers.inform_release_to_frame(
            workbook,
            cast("str", self._dataset.release_column),
            indicator_id=cast("str", self._dataset.indicator_id),
            country=country,
            release_year=year,
        )
        self._upstream_rows = scores.attrs.get("served_rows", len(scores))
        return scores

    @property
    def _resolved_workflow_id(self) -> int | None:
        """The INFORM WorkflowId this request uses: the override, else the pin."""
        if self._workflow_id is not None:
            return self._workflow_id
        return self._dataset.workflow_id

    def _empty_hint(self) -> str:
        """Explain an empty INFORM result, so the cause is not left ambiguous.

        An INFORM request comes back empty for two very different reasons: the
        workflow served no scores at all (the pinned release was withdrawn or
        never published upstream), or it served the global table and the
        `country=` filter matched none of it. The written table looks identical
        either way, so the warning says which one happened.

        Returns:
            str: A sentence to append to the empty-result warning; `""` for a
                non-INFORM dataset, or when no INFORM request has been issued
                yet (nothing has been observed to explain).
        """
        if self._dataset.provider != "inform" or self._upstream_rows is None:
            return ""
        channel = (
            "the INFORM release workbook"
            if self._reads_release
            else f"INFORM workflow {self._resolved_workflow_id}"
        )
        if self._upstream_rows == 0:
            return (
                f" {channel} served no rows at all; it may have been withdrawn "
                "upstream — pass workflow_id= (or source=) to read another "
                "release."
            )
        # Rows were served, so only the country filter can have emptied the
        # frame - without one, shaping keeps every row.
        return (
            f" {channel} served {self._upstream_rows} row(s), none for "
            f"country={self._country!r}; the code may be misspelt or outside the "
            "country set INFORM scores."
        )

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write a tabular result to `root_dir` and return the path.

        Args:
            df: The result frame.

        Returns:
            Path: The written CSV / Parquet file path.

        Raises:
            ImportError: If `output_format="parquet"` but `pyarrow` is missing.
        """
        stem = "risk_" + self._dataset.id.replace(":", "_")
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

    def _log_citation(self) -> None:
        """Log the resolved dataset's source citation once (info, not a warning)."""
        citation = self._dataset.citation
        if citation:
            logger.info(f"RiskIndicators source citation: {citation}")
