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
from earthlens.risk_indicators import _helpers
from earthlens.risk_indicators.auth import GfwAuth, GfwCredentials
from earthlens.risk_indicators.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk output formats for a written tabular result.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Global sentinel bounds — risk indicators are country/admin-indexed, not
#: gridded, so the spatial extent is the whole globe (`G7`).
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


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

    Attributes:
        OUTPUT_KIND: Set **per instance** in :meth:`__init__` from the resolved
            dataset's `output_kind` (`"tabular"` or `"vector"`). The facade
            reads it to gate `aggregate=` and to know the return shape.
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
                only consulted for an `inform` dataset. Use it when the pinned
                workflow stops serving scores, or to read an older release
                (`/workflows` lists every WorkflowId).
            output_format: On-disk format for a tabular result — `"csv"`
                (default) or `"parquet"`.

        Raises:
            TypeError: If `variables` is a mapping (pass a list of one id).
            ValueError: If `variables` is not exactly one dataset id, if
                `output_format` is unrecognised, if `workflow_id` is not an
                integer, or if the required country / admin selector for the
                resolved provider is missing (`G7`).
            AuthenticationError: For a `gfw` dataset when no key resolves (`G3`).
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
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )
        # The id goes straight into the query string, where a string or a float
        # would 200 with an empty body rather than fail - indistinguishable from
        # a withdrawn workflow. Reject it here instead. bool is an int subclass.
        if workflow_id is not None and (
            isinstance(workflow_id, bool) or not isinstance(workflow_id, int)
        ):
            raise ValueError(
                f"workflow_id must be an INFORM WorkflowId integer, got "
                f"{workflow_id!r}."
            )

        self._catalog = Catalog()
        self._dataset: Dataset = self._catalog.get(ids[0])
        self._country = country
        self._admin_code = admin_code
        self._api_key = api_key
        self._workflow_id = workflow_id
        # Row count of the last INFORM payload, before the country filter, so an
        # empty result can say which of the two causes produced it.
        self._upstream_rows: int | None = None
        self._output_format: OutputFormat = output_format
        self._auth: GfwAuth | None = None

        # G1 — the per-instance output shape comes from the resolved dataset.
        self.OUTPUT_KIND = self._dataset.output_kind

        self._validate_selector()

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
        if dataset.provider == "thinkhazard":
            code = admin_code or self._catalog.resolve_admin(cast("str", country))
            payload = _helpers.thinkhazard_query(code, dataset.hazard)
            return _helpers.thinkhazard_to_frame(
                payload, admin_code=code, hazard=dataset.hazard, country=country
            )
        if dataset.provider == "inform":
            payload = _helpers.inform_query(
                cast("int", self._resolved_workflow_id),
                cast("str", dataset.indicator_id),
            )
            self._upstream_rows = len(payload)
            return _helpers.inform_to_frame(payload, country=country)
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
        workflow = self._resolved_workflow_id
        if self._upstream_rows == 0:
            return (
                f" INFORM workflow {workflow} served no rows at all; it may have "
                "been withdrawn upstream — pass workflow_id= to read another "
                "workflow (/workflows lists them)."
            )
        if self._country is None:
            return (
                f" INFORM workflow {workflow} served {self._upstream_rows} row(s) "
                "that did not survive shaping."
            )
        return (
            f" INFORM workflow {workflow} served {self._upstream_rows} row(s), none "
            f"for country={self._country!r}; INFORM scores ~191 countries, so the "
            "code may be misspelt or outside its coverage."
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
