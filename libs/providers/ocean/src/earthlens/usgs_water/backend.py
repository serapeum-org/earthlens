"""Backend that fetches USGS water data from NWIS / the USGS Water Data API.

`USGSWater(AbstractDataSource)` wraps the official **`dataretrieval`**
SDK to pull time-series and discrete water observations from the U.S.
Geological Survey's National Water Information System — ~10,000 active
stream gauges and many more groundwater / water-quality sites across the
United States. A request is a bbox (or explicit `sites=`) + a time
window + a list of **NWIS parameter codes** (`["00060"]` discharge,
`["00065"]` gage height, …); the backend returns a per-site time-series
as a long-format :class:`pandas.DataFrame`, so `OUTPUT_KIND = "tabular"`
and the :class:`earthlens.earthlens.EarthLens` facade rejects an
`aggregate=` argument (use the server-side `service="statistics"`
rollup instead).

The full NWIS / Water Data service surface is selectable via a
`service=` keyword argument (default `"daily"`): `daily`,
`instantaneous`, `samples`, `statistics`, `gwlevels`,
`field-measurements`, `peaks`, `ratings`, and `sites`. The USGS is
mid-migration from the legacy `waterservices.usgs.gov` endpoint (the
`dataretrieval.nwis` module) to the modern `api.waterdata.usgs.gov`
endpoint (the `dataretrieval.waterdata` module). The `api=` keyword
selects which:

* `"auto"` (default) — try the modern endpoint, but because it
  rate-limits anonymous access aggressively (HTTP 429), transparently
  fall back to the legacy endpoint on a 429 when no token is set.
* `"waterdata"` — force the modern endpoint (a 429 surfaces as an
  error).
* `"legacy"` — force the legacy endpoint.

Authentication is an **optional** Personal Access Token (the
`API_USGS_PAT` env var or the `api_token=` argument); anonymous access
works at lower rate limits. See :class:`earthlens.usgs_water.auth`.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Literal

import pandas as pd
from loguru import logger
from pydantic import SecretStr

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.usgs_water import _helpers
from earthlens.usgs_water.auth import UsgsWaterAuth, UsgsWaterCredentials
from earthlens.usgs_water.catalog import Catalog

ApiFlavour = Literal["auto", "waterdata", "legacy"]
OutputFormat = Literal["csv", "parquet"]

#: Every NWIS / Water Data service plane the backend can address. The
#: `service=` argument is validated against this tuple; the per-module
#: function names live in :data:`earthlens.usgs_water._helpers._SERVICE_FN`.
SERVICES: tuple[str, ...] = (
    "daily",
    "instantaneous",
    "samples",
    "statistics",
    "gwlevels",
    "field-measurements",
    "peaks",
    "ratings",
    "sites",
)

#: Accepted `api=` selectors (modern / legacy / auto-fallback).
API_FLAVOURS: tuple[str, ...] = ("auto", "waterdata", "legacy")

#: Accepted on-disk output formats.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Services that are keyed by site rather than parameter code, so they
#: ignore `variables` and require an explicit `sites=`.
_SITE_KEYED_SERVICES: frozenset[str] = frozenset({"peaks", "ratings"})

#: Services that require an explicit `sites=` because neither endpoint
#: offers a spatial (bbox) filter for them. The site-keyed services
#: above plus `statistics` (the `get_stats_date_range` / `get_stats`
#: functions take a site, not a bbox, so a bbox-only statistics request
#: would be spatially unbounded). `statistics` still uses `variables`
#: (parameter codes), so it is not in :data:`_SITE_KEYED_SERVICES`.
_SITES_REQUIRED_SERVICES: frozenset[str] = _SITE_KEYED_SERVICES | {"statistics"}

#: Default parameter code when `variables` is empty — discharge (cfs).
_DEFAULT_CODES: list[str] = ["00060"]

#: `temporal_resolution` tokens that alias the default service onto
#: `instantaneous`. Any other value leaves the service at `"daily"`, so
#: an unrelated label (`"monthly"`, `"yearly"`) never silently switches
#: the plane — an explicit `service=` is the real selector.
_SUBDAILY_RESOLUTIONS: frozenset[str] = frozenset(
    {"instantaneous", "hourly", "sub-daily", "subdaily", "iv", "raw", "15min", "minute"}
)


def _import_dataretrieval():
    """Import the `dataretrieval` SDK lazily with a friendly error.

    Keeps `import earthlens.usgs_water` working without the optional
    `[usgs-water]` extra: the SDK is only needed at `download()` time.

    Returns:
        The imported `dataretrieval` top-level module.

    Raises:
        ImportError: When `dataretrieval` is not installed; the message
            names the `earthlens[usgs-water]` extra to install.
    """
    try:
        import dataretrieval  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised via fakes
        raise ImportError(
            "The USGS Water backend requires the 'dataretrieval' SDK. "
            "Install it with: pip install earthlens[usgs-water]"
        ) from exc
    return dataretrieval


class USGSWater(AbstractDataSource):
    """USGS NWIS / Water Data backend (long-format tabular output).

    Fetches per-site water observations for a bbox / explicit sites /
    date window / parameter-code list through the same `download()`
    shape every other earthlens backend uses, and returns a
    long-format :class:`pandas.DataFrame` (one row per observation).
    The query is a search/fetch split: :meth:`_search` enumerates the
    monitoring locations to pull, and :meth:`_fetch` pulls each
    service's observations and normalises them to one tidy long schema.

    The `service=` argument selects the NWIS / Water Data plane (see
    :data:`SERVICES`); `api=` selects the modern / legacy endpoint with
    a 429 auto-fallback (see the module docstring). Authentication is
    an optional Personal Access Token.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is per-row site
            observations, so the facade rejects `aggregate=` with
            `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "USGS water observations are tabular per-site rows, not gridded rasters, so there is no meaningful gridded reduction. Use service='statistics' for a server-side temporal rollup (daily/monthly/annual) instead"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        api_token: str | None = None,
        service: str = "daily",
        sites: list[str] | str | None = None,
        api: ApiFlavour = "auto",
        output_format: OutputFormat = "csv",
        stat_type: str = "daily",
        limit: int | None = None,
    ):
        """Initialise a USGS Water backend instance.

        Args:
            start: Inclusive start of the window, parsed with `fmt`.
            end: Inclusive end of the window.
            variables: List of NWIS parameter codes or friendly names
                (`["00060"]`, `["discharge", "gage_height"]`), resolved
                to 5-digit codes via the catalog. An empty list defaults
                to discharge (`["00060"]`). Ignored by the site-keyed
                services (`peaks`, `ratings`).
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes.
            temporal_resolution: Convenience alias mapped onto
                `service` when `service` is left at its default —
                `"daily"` keeps `daily`, a sub-daily value selects
                `instantaneous`. An explicit `service=` always wins.
            path: Output directory for the written table.
            fmt: `strptime` format for `start` / `end`.
            api_token: Optional USGS Personal Access Token; falls back
                to the `API_USGS_PAT` env var, then anonymous access.
            service: The NWIS / Water Data plane to query — one of
                :data:`SERVICES`. Defaults to `"daily"`.
            sites: Explicit USGS site number(s) to query, bypassing the
                bbox site discovery. **Required** for the services that
                have no spatial (bbox) filter — `peaks`, `ratings`, and
                `statistics`; a bbox-only request for one of those raises
                `ValueError`.
            api: Endpoint selector — `"auto"` (default; modern with a
                429 fallback to legacy), `"waterdata"` (force modern),
                or `"legacy"` (force the deprecated `nwis` endpoint).
            output_format: On-disk format — `"csv"` (default) or
                `"parquet"`.
            stat_type: For `service="statistics"` on the **legacy**
                endpoint (`api="legacy"`), the `get_stats` rollup period
                — `"daily"`, `"monthly"`, or `"annual"`. Ignored by the
                modern endpoint, whose `get_stats_date_range` returns its
                own intervals over the `start`/`end` window.
            limit: Optional cap on the rows pulled **per request**, passed
                through to the modern endpoint's own `limit=`. `None`
                means the SDK default. Distinct from `download(limit=)`,
                which caps the *total* rows returned across every item;
                the two compose, and neither overwrites the other.

        Raises:
            ValueError: When `service`, `api`, or `output_format` is
                not a recognised value.
            TypeError: When `variables` is a mapping (this backend takes
                a flat list of parameter codes / names).
        """
        if isinstance(variables, dict):
            raise TypeError(
                "USGSWater `variables` must be a list of NWIS parameter "
                "codes or friendly names (e.g. ['00060', 'gage_height']), "
                "not a mapping. Query filters are explicit USGSWater(...) "
                "keyword arguments (service=, sites=, api=, ...)."
            )
        if service not in SERVICES:
            raise ValueError(
                f"service must be one of {list(SERVICES)}, got {service!r}."
            )
        if api not in API_FLAVOURS:
            raise ValueError(f"api must be one of {list(API_FLAVOURS)}, got {api!r}.")
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )

        # `service` wins; otherwise honour the temporal_resolution alias —
        # but only for explicitly sub-daily tokens. Any other value
        # (e.g. "monthly", "yearly", "") leaves the default daily service
        # rather than silently selecting instantaneous.
        if service == "daily" and temporal_resolution.lower() in _SUBDAILY_RESOLUTIONS:
            service = "instantaneous"

        self._api_token = api_token
        self._service = service
        self._sites = [sites] if isinstance(sites, str) else sites
        self._api_flavour = api
        self._output_format: OutputFormat = output_format
        self._stat_type = stat_type
        # Validated like fdsn's: this goes onto the wire as the modern
        # endpoint's own `limit=`, where 0 / -5 / True is a caller bug the
        # service would answer confusingly rather than reject.
        self._request_limit = self.check_limit(limit)
        self._auth: UsgsWaterAuth | None = None
        self._catalog = Catalog()
        self._used_legacy_fallback = False
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_DEFAULT_CODES),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """Build :class:`UsgsWaterAuth` and resolve the optional token.

        Returns `None` (the SDK has no global client object; the token,
        when present, is exported to `API_USGS_PAT` by the auth).
        """
        self._auth = UsgsWaterAuth(
            UsgsWaterCredentials(
                api_token=(
                    SecretStr(self._api_token) if self._api_token is not None else None
                )
            )
        )
        self._auth.configure()
        return None

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        The whole window is fetched per site in one call (NWIS takes a
        start/end range), so there is no per-date loop; `dates`
        collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        return self._whole_window_extent(
            start, end, fmt=fmt, resolution=temporal_resolution
        )

    def _resolved_codes(self) -> list[str]:
        """Resolve `variables` to 5-digit NWIS parameter codes, order-stable.

        Returns:
            list[str]: The resolved codes (de-duplicated, first-wins).

        Raises:
            ValueError: If a name is neither a known catalog entry nor a
                raw 5-digit code (with a did-you-mean hint).
        """
        codes: list[str] = []
        for name in self.vars:
            code = self._catalog.resolve(name)
            if code not in codes:
                codes.append(code)
        return codes

    def _bbox_list(self) -> list[float]:
        """Return the request bbox as modern `[west, south, east, north]`.

        The legacy `"west,south,east,north"` string form is built inline by
        :func:`earthlens.usgs_water._helpers.query_kwargs` from this list.
        """
        return [self.space.west, self.space.south, self.space.east, self.space.north]

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Fetch the selected service, write the table, and return it.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends. USGS Water issues one bulk `dataretrieval`
                call per service rather than a per-item loop, so there
                is no progress bar to show — this is a no-op.
            limit: Cap on the **total** rows returned. **Trims, it does not
                reduce the fetch**: `_search` plans one bulk `dataretrieval`
                call per service, so there is no later item for the cap to
                skip. The constructor's `limit=` is the one that reduces
                transfer — it is a per-request cap the modern endpoint applies
                server-side — and the two are independent; passing neither,
                either, or both is valid. `None` (the default) returns
                everything.

        Returns:
            pd.DataFrame: The long-format observation table for the
                selected `service`.

        Raises:
            ValueError: If the selected `service` requires an explicit
                `sites=` (`peaks` / `ratings` / `statistics`) but none
                was supplied.
        """
        self._limit = self.check_limit(limit)
        # Each frame is already normalised to its service's schema (even
        # when empty), so concat all of them — preserving the right
        # columns for non-values services — rather than dropping empties.
        frames = self._api()
        df = (
            pd.concat(frames, ignore_index=True)
            if frames
            else _helpers.empty_canonical()
        )
        out_path = self._write_table(df)
        if len(df):
            logger.info(
                f"USGSWater {self._service}: {len(df)} row(s) written to {out_path}"
            )
        else:
            logger.warning(
                f"USGSWater {self._service}: no rows matched; wrote an empty "
                f"(schema-only) table to {out_path}"
            )
        return df

    def _search(self) -> list[RemoteProduct]:
        """Enumerate the products to fetch (one per request, pre-C8).

        The C3 search is a single product carrying the resolved
        parameter codes and any explicit `sites=`; the real bbox site
        discovery (one product per monitoring location) lands in C8.

        Returns:
            list[RemoteProduct]: One product whose `metadata` holds the
                resolved `codes` and the explicit `sites` (or `None`).

        Raises:
            ValueError: When the service requires an explicit `sites=`
                (no bbox filter is available for it) but none was given.
        """
        if self._service in _SITES_REQUIRED_SERVICES and not self._sites:
            raise ValueError(
                f"service={self._service!r} requires an explicit sites= "
                f"(no spatial bbox filter is available for it); pass "
                f"sites=[...] (e.g. sites='01646500')."
            )
        codes = [] if self._service in _SITE_KEYED_SERVICES else self._resolved_codes()
        return [
            RemoteProduct(
                id=self._service,
                metadata={"codes": codes, "sites": self._sites},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[pd.DataFrame]:
        """Query each product's service and normalise to the long schema.

        Widens the inherited `-> list[Path]` contract: a tabular
        backend returns in-memory long-format frames, not file paths
        (the write happens in :meth:`download` via :meth:`_write_table`).

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[pd.DataFrame]: One canonical long-schema frame per
                product, same order.
        """
        return self._fetch_limited(products, self._limit)

    def _fetch_one(self, product: RemoteProduct) -> pd.DataFrame:
        """Fetch one product's service frame and normalise it.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            pd.DataFrame: The canonical long-schema frame for this
                product (empty when the service returned nothing).
        """
        codes = product.metadata.get("codes", [])
        sites = product.metadata.get("sites")
        df, flavour = self._call_with_fallback(codes, sites)
        return _helpers.normalize(df, flavour, self._service, self._code_meta(codes))

    def _module(self, flavour: str):
        """Lazily import the `dataretrieval` submodule for a flavour.

        Args:
            flavour: `"waterdata"` (modern) or `"nwis"` (legacy).

        Returns:
            The imported `dataretrieval.waterdata` / `dataretrieval.nwis`
                module.

        Raises:
            ImportError: When `dataretrieval` is not installed (names
                the `earthlens[usgs-water]` extra).
        """
        _import_dataretrieval()
        submodule = "waterdata" if flavour == "waterdata" else "nwis"
        return importlib.import_module(f"dataretrieval.{submodule}")

    def _invoke(
        self, flavour: str, codes: list[str], sites: list[str] | None
    ) -> pd.DataFrame:
        """Call the resolved service function and return its raw frame.

        Args:
            flavour: `"waterdata"` or `"nwis"`.
            codes: Resolved parameter codes.
            sites: Explicit site numbers, or `None` for a bbox query.

        Returns:
            pd.DataFrame: The frame returned by the SDK call (the first
                element when the SDK returns a `(frame, metadata)`
                tuple).
        """
        module = self._module(flavour)
        fn_name = _helpers.service_function(self._service, flavour)
        # _call_with_fallback only routes to a flavour that has a function.
        assert fn_name is not None
        function = getattr(module, fn_name)
        kwargs = _helpers.query_kwargs(
            service=self._service,
            flavour=flavour,
            codes=codes,
            sites=sites,
            bbox=self._bbox_list(),
            start=self.time.start_date.strftime("%Y-%m-%d"),
            end=self.time.end_date.strftime("%Y-%m-%d"),
            limit=self._request_limit,
            stat_type=self._stat_type,
        )
        result = function(**kwargs)
        return result[0] if isinstance(result, tuple) else result

    def _call_with_fallback(
        self, codes: list[str], sites: list[str] | None
    ) -> tuple[pd.DataFrame, str]:
        """Invoke the service, applying the modern→legacy fallbacks.

        Two fallbacks fold in here (both honouring `api=`): a service
        the modern endpoint can only query by site (e.g. instantaneous,
        which has no `bbox`) routes to legacy when only a bbox is given;
        and a modern HTTP 429 (anonymous rate-limit) under `api="auto"`
        retries on legacy.

        Args:
            codes: Resolved parameter codes.
            sites: Explicit site numbers, or `None` for a bbox query.

        Returns:
            tuple[pd.DataFrame, str]: The raw frame and the flavour
                (`"waterdata"` / `"nwis"`) that produced it.

        Raises:
            ValueError: When `api="waterdata"` is forced for a
                bbox-only query the modern endpoint cannot serve.
        """
        has_legacy = _helpers.service_function(self._service, "nwis") is not None
        use_legacy = self._api_flavour == "legacy"
        if use_legacy and not has_legacy:
            raise ValueError(
                f"service {self._service!r} has no legacy endpoint (it is "
                f"modern-only in dataretrieval); use api='auto' or "
                f"api='waterdata'."
            )

        bbox_only = not sites
        if (
            not use_legacy
            and bbox_only
            and not _helpers.modern_supports_bbox(self._service)
        ):
            if self._api_flavour == "waterdata" or not has_legacy:
                raise ValueError(
                    f"The modern endpoint cannot query service "
                    f"{self._service!r} by bbox (no bbox filter). Pass "
                    f"sites=[...] explicitly, or use api='auto'/'legacy'."
                )
            use_legacy = True  # api="auto": legacy supports bBox

        if use_legacy:
            return self._invoke("nwis", codes, sites), "nwis"
        try:
            return self._invoke("waterdata", codes, sites), "waterdata"
        except Exception as exc:  # noqa: BLE001 - re-raised unless a 429 fallback
            anonymous = self._auth is None or not self._auth.is_authenticated()
            if (
                self._api_flavour == "auto"
                and anonymous
                and _helpers.is_rate_limit_error(exc)
            ):
                if not has_legacy:
                    raise RuntimeError(
                        f"The modern USGS endpoint rate-limited this anonymous "
                        f"request (HTTP 429) and service {self._service!r} has "
                        f"no legacy fallback. Set API_USGS_PAT (or pass "
                        f"api_token=) to use the modern endpoint."
                    ) from exc
                self._warn_legacy_fallback()
                return self._invoke("nwis", codes, sites), "nwis"
            raise

    def _warn_legacy_fallback(self) -> None:
        """Log a one-time warning when falling back to the legacy endpoint."""
        if self._used_legacy_fallback:
            return
        self._used_legacy_fallback = True
        logger.warning(
            "USGS modern endpoint rate-limited this anonymous request "
            "(HTTP 429); falling back to the legacy waterservices.usgs.gov "
            "endpoint. Set API_USGS_PAT (or pass api_token=) to use the "
            "modern endpoint without throttling."
        )

    def _code_meta(self, codes: list[str]) -> dict[str, tuple[str, str]]:
        """Build a `{code: (name, units)}` map from the catalog.

        Args:
            codes: Resolved 5-digit parameter codes.

        Returns:
            dict[str, tuple[str, str]]: Friendly name + units per code
                that the catalog curates; uncurated codes map to
                `("", "")`.
        """
        by_code = {p.code: (p.name, p.units) for p in self._catalog.parameters.values()}
        return {code: by_code.get(code, ("", "")) for code in codes}

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write the long-format table to `root_dir` and return the path.

        Args:
            df: The canonical long-format frame.

        Returns:
            Path: The written CSV / Parquet file path.
        """
        codes = [] if self._service in _SITE_KEYED_SERVICES else self._resolved_codes()
        codes_part = "_".join(codes or ["all"])
        ext = "parquet" if self._output_format == "parquet" else "csv"
        out_path = self.root_dir / f"usgs_{self._service}_{codes_part}.{ext}"
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
