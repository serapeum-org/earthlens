"""Backend that fetches NREL solar / wind resource time series.

`NREL(AbstractDataSource)` hits the keyed NREL/NLR Developer Network CSV
download API (`https://developer.nlr.gov/api/...`) directly with `requests` —
no heavy gridded-archive SDK, no array layer, no `pyramids`. A request selects
a product with `product="nsrdb-psm3"` (GOES Aggregated PSM v4 hourly solar) /
`"nsrdb-tmy"` (typical meteorological year) / `"wtk"` (WIND Toolkit hourly
wind), picks the attributes with `variables=["ghi", "dni", ...]` (defaulting to
the product's attribute list), samples the request location(s) — a single
`point=(lat, lon)` (or a degenerate bbox), or a bbox expanded to a point grid
at `spacing_deg` — and issues one throttled keyed `GET` per `(point, year)`
(the CSV endpoints serve one point and one year per call), parsing each into a
long-format `pandas.DataFrame` tagged with `lat`/`lon`/`year`/`product`.

Authentication is **required**: an NREL API key *and* the registered email,
passed as `api_key=` / `email=` or via `NREL_API_KEY` / `NREL_EMAIL`. They are
resolved (and a missing one raises `AuthenticationError`) at construction.

This is a `tabular` backend: the result is a per-coordinate time-series table,
so `OUTPUT_KIND = "tabular"` and the `earthlens.earthlens.EarthLens` facade
rejects an `aggregate=` argument for it — NREL already returns the resolved
hourly / TMY series.

NREL coverage is region-dependent (NSRDB: Americas + parts of Asia/Africa/
Europe; WTK: CONUS + offshore): an out-of-coverage or invalid request returns
an HTTP error. For a multi-point bbox the point/year is skipped with a warning;
for a single explicit point a `ValueError` naming the coordinate is raised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import pandas as pd
import requests
from loguru import logger
from pydantic import SecretStr

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.nrel import _helpers
from earthlens.nrel.auth import NrelAuth, NrelCredentials
from earthlens.nrel.catalog import Catalog, Product

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk output formats.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Default product when the caller passes no `product=`.
DEFAULT_PRODUCT = "nsrdb-psm3"

#: Default bbox sampling step in degrees. NSRDB / WTK cells are ~2-4 km, so a
#: coarse default keeps a casual bbox from fanning out to thousands of calls.
DEFAULT_SPACING_DEG = 0.5

#: Soft / hard request-count guards. The fan-out is points x years, and the CSV
#: API is capped at 5000 req/day, <=1 req/s — a grid over the soft threshold
#: warns, over the hard cap raises, so a country-scale request never silently
#: fires thousands of keyed GETs.
WARN_REQUESTS = 100
DEFAULT_MAX_REQUESTS = 500

#: The NREL attribution logged once on a successful download.
_CITATION = (
    "Data from the NREL National Solar Radiation Database (NSRDB) / WIND "
    "Toolkit via the NREL Developer Network (developer.nlr.gov). Please cite "
    "NREL per https://nsrdb.nrel.gov/ and the WIND Toolkit references."
)


class NREL(AbstractDataSource):
    """NREL NSRDB / WIND Toolkit time-series backend (long-format tabular).

    Fetches per-coordinate hourly (or TMY) time series for a point (or a bbox
    sampled to a point grid) / date window / product through the same
    `download()` shape every other earthlens backend uses, and returns a
    long-format `pandas.DataFrame` (one block of rows per sampled
    `(point, year)`). The query is a search/fetch split: `_search` enumerates
    the `(lat, lon, year)` calls to make, and `_fetch` issues one throttled
    keyed GET per call and parses it.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is per-row observations, so the
            facade rejects `aggregate=` with `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "NREL output is a per-coordinate hourly time series (tabular), not a gridded raster, so there is no meaningful gridded reduction. NREL already returns the resolved hourly / TMY series"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str] | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "hourly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        point: tuple[float, float] | None = None,
        spacing_deg: float = DEFAULT_SPACING_DEG,
        output_format: OutputFormat = "csv",
        product: str = DEFAULT_PRODUCT,
        interval: int | None = None,
        utc: str = "false",
        api_key: str | None = None,
        email: str | None = None,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        **knobs: Any,
    ):
        """Initialise an NREL backend instance.

        Args:
            start: Inclusive start of the window, parsed with `fmt`. Its year is
                the first `names=` year for the year-based products; `tmy`
                ignores it.
            end: Inclusive end of the window (its year is the last `names=`
                year).
            variables: The attributes to request (e.g. `["ghi", "dni"]`). An
                empty list / `None` falls back to the product's
                `default_attributes`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes. Optional when
                `point=` is given.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes. Optional when
                `point=` is given.
            temporal_resolution: Recorded resolution label (NREL is hourly).
            path: Output directory for the written table.
            fmt: `strptime` format for `start` / `end`.
            point: An explicit `(lat, lon)` to query a single location. When
                given it wins, collapsing the request to that one coordinate and
                overriding any `lat_lim` / `lon_lim` (including the whole-Earth
                defaults the facade injects).
            spacing_deg: Grid step in degrees when sampling a bbox.
            output_format: On-disk format — `"csv"` (default) or `"parquet"`.
            product: The catalog product id — `"nsrdb-psm3"` (default),
                `"nsrdb-tmy"`, or `"wtk"`.
            interval: Data resolution in minutes (`30` or `60`); defaults to the
                product's `interval`.
            utc: `"true"` for UTC timestamps, `"false"` (default) for local.
            api_key: The NREL API key; falls back to `NREL_API_KEY`.
            email: The registered contact email; falls back to `NREL_EMAIL`.
            max_requests: Hard cap on the number of `(point, year)` calls; a
                request that would exceed it raises `ValueError`.
            **knobs: Reserved for future per-product query knobs.

        Raises:
            ValueError: When `output_format` is unrecognised, or when no
                location (neither `point=` nor a bbox) is supplied.
            TypeError: When `variables` is a mapping or a bare string (this
                backend takes a list of attribute names; a string would split
                into single-character attributes).
            AuthenticationError: When neither an explicit nor an environment
                API key + email pair can be resolved.
        """
        if isinstance(variables, (dict, str)):
            raise TypeError(
                "NREL `variables` must be a list of attribute names "
                "(e.g. ['ghi', 'dni']), not a "
                f"{type(variables).__name__}. A bare string would split into "
                "single-character attributes; wrap it in a list (['ghi']). "
                "Pick the product with product='nsrdb-psm3' / 'nsrdb-tmy' / 'wtk'."
            )
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )
        if point is not None:
            # `point=` wins and collapses the bbox to that single coordinate,
            # overriding any lat_lim/lon_lim (including the whole-Earth defaults
            # the EarthLens facade injects), so a single-point request behaves
            # identically through the facade and directly.
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise ValueError(
                    f"NREL `point` must be a 2-tuple (lat, lon); got {point!r}."
                )
            lat_lim = [float(point[0]), float(point[0])]
            lon_lim = [float(point[1]), float(point[1])]
        if lat_lim is None or lon_lim is None:
            raise ValueError(
                "NREL needs a location: pass point=(lat, lon) for a single "
                "site, or lat_lim=/lon_lim= (or aoi=) for a bbox."
            )

        self._catalog = Catalog()
        self._spacing_deg = spacing_deg
        self._output_format: OutputFormat = output_format
        self._product_id = product
        self._interval_override = interval
        self._utc = utc
        self._max_requests = max_requests
        self._knobs = dict(knobs)
        self._requested_variables = list(variables) if variables else []
        self._auth = NrelAuth(
            NrelCredentials(
                api_key=None if api_key is None else SecretStr(api_key), email=email
            )
        )
        self._product: Product | None = None
        self._attributes: list[str] = []
        self._show_progress = True
        super().__init__(
            start=start,
            end=end,
            variables=self._requested_variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """Resolve the product + attributes and authenticate.

        Resolves the catalog product, picks the attribute list (the requested
        `variables` or the product default), and runs `NrelAuth.configure()` so
        a missing key/email fails fast. Returns `None` — there is no client
        object.

        Raises:
            ValueError: If `product` is not a known id (with a did-you-mean
                hint).
            AuthenticationError: If neither an explicit nor an environment key +
                email pair resolves.
        """
        self._product = self._catalog.get(self._product_id)
        self._attributes = self._requested_variables or list(
            self._product.default_attributes
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
        """Parse the `[start, end]` window into a `TemporalExtent`.

        The resolution label is always `"hourly"` — NREL's CSV products are
        hourly — so the caller's (or the facade's `"daily"`) value for
        `temporal_resolution` is ignored here.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Accepted for the shared constructor shape but
                not recorded — the label is fixed to `"hourly"`.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints and an
                `"hourly"` resolution label.

        Raises:
            pydantic.ValidationError: If `start` parses to a date later than
                `end` — `TemporalExtent` rejects inverted bounds (note this is
                not a `ValueError` subclass).
            ValueError: If `start` / `end` cannot be parsed at all. `fmt` is
                only tried first — a non-matching but ISO-8601-parseable
                string still succeeds.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="hourly")

    def _names(self) -> list[Any]:
        """Enumerate the `names=` values (years, or the literal `tmy`).

        Returns:
            list[Any]: `["tmy"]` for the TMY product, else one entry per
                calendar year in the `[start, end]` window.
        """
        assert self._product is not None  # set by _initialize
        if self._product.names_kind == "tmy":
            return ["tmy"]
        return list(range(self.time.start_date.year, self.time.end_date.year + 1))

    def _interval(self) -> int:
        """The data resolution in minutes (override or product default)."""
        assert self._product is not None  # set by _initialize
        return (
            self._interval_override
            if self._interval_override is not None
            else self._product.interval
        )

    def _calls(self) -> list[tuple[float, float, Any]]:
        """Enumerate and guard the `(lat, lon, name)` calls to make.

        Returns:
            list[tuple[float, float, Any]]: One tuple per `(point, year)`
                (or `(point, "tmy")`) call.

        Raises:
            ValueError: If the fan-out exceeds `max_requests` — the message
                tells the user to shrink the bbox, coarsen `spacing_deg`, or
                narrow the year window.
        """
        points = _helpers.point_grid(self.space, self._spacing_deg)
        names = self._names()
        calls = [(lat, lon, name) for lat, lon in points for name in names]
        if len(calls) > self._max_requests:
            raise ValueError(
                f"NREL request would make {len(calls)} keyed CSV calls "
                f"({len(points)} point(s) x {len(names)} year(s)) > the "
                f"max_requests={self._max_requests} cap (CSV API: 5000/day, "
                f"<=1/s). Shrink the bbox, coarsen spacing_deg "
                f"(now {self._spacing_deg}), narrow the years, or raise "
                f"max_requests= deliberately."
            )
        if len(calls) > WARN_REQUESTS:
            logger.warning(
                f"NREL will make {len(calls)} keyed CSV calls (one per "
                f"point x year), throttled to <=1 req/s — this takes at least "
                f"{len(calls)} seconds. Coarsen spacing_deg or narrow the "
                f"years to reduce the count."
            )
        return calls

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Fetch every `(point, year)` call, write the table, and return it.

        Args:
            progress_bar: Show a per-call `tqdm` bar while fetching.
            limit: Cap on the total rows returned, across every requested
                call. Applied as each call's frame arrives, so a call past
                the cap is never requested. `None` (the default) fetches
                everything.

        Returns:
            pd.DataFrame: The concatenated long-format frame — one block of
                rows per in-coverage `(point, year)`, tagged with
                `lat`/`lon`/`year`/`product`.

        Raises:
            ValueError: If a single explicit point is out of NREL coverage, or
                the fan-out exceeds `max_requests`.
        """
        self._limit = self.check_limit(limit)
        assert self._product is not None  # set by _initialize
        self._show_progress = progress_bar
        frames = [frame for frame in self._api() if frame is not None]
        if frames:
            df = pd.concat(frames, ignore_index=True)
        else:
            df = _helpers.empty_canonical(
                [*self._product.columns, "lat", "lon", "year", "product"]
            )
        out_path = self._write_table(df)
        if len(df):
            logger.info(f"NREL {self._product_id}: {len(df)} row(s) -> {out_path}")
            logger.info(_CITATION)
        else:
            logger.warning(
                f"NREL {self._product_id}: no rows returned; wrote an empty "
                f"(schema-only) table to {out_path}"
            )
        return df

    def _search(self) -> list[RemoteProduct]:
        """Enumerate one product per `(lat, lon, name)` call.

        Returns:
            list[RemoteProduct]: One product per call, each carrying the
                `lat`/`lon`/`name` in `metadata`.

        Raises:
            ValueError: If the fan-out exceeds `max_requests`.
        """
        return [
            RemoteProduct(
                id=f"{self._product_id}:{lat},{lon}:{name}",
                metadata={"lat": lat, "lon": lon, "name": name},
            )
            for lat, lon, name in self._calls()
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[pd.DataFrame | None]:
        """Fetch each call's series, throttled and coverage-aware.

        Widens the inherited `-> list[Path]` contract: a tabular backend
        returns in-memory long-format frames (or `None` for a skipped
        out-of-coverage call), not file paths.

        Args:
            products: The list returned by `_search`.

        Returns:
            list[pd.DataFrame | None]: One frame per in-coverage call (same
                order); `None` where a multi-point bbox skipped a call.
                Truncated when `limit=` was passed to `download`.
        """
        from tqdm import tqdm

        last_call = [0.0]
        single = len({(p.metadata["lat"], p.metadata["lon"]) for p in products}) == 1
        # `with`, so a cap that stops the sweep early still closes the bar:
        # tqdm keeps redrawing and holds the terminal until it is closed.
        with (
            tqdm(
                products,
                disable=not self._show_progress,
                desc="NREL",
                unit="call",
            ) as iterator,
            requests.Session() as session,
        ):
            # Lazy so a `limit=` stops the work: a call past the cap is never
            # requested. Skipped (`None`) calls count as zero rows, so the cap
            # bounds returned rows rather than attempted calls.
            return self._take_limited(
                (
                    self._fetch_call(product, session, last_call, single=single)
                    for product in iterator
                ),
                limit=self._limit,
                size=lambda frame: 0 if frame is None else len(frame),
            )

    def _fetch_call(
        self,
        product: RemoteProduct,
        session: Any,
        last_call: list[float],
        *,
        single: bool,
    ) -> pd.DataFrame | None:
        """Fetch and parse one `(point, year)` call, applying the coverage policy.

        Args:
            product: One `RemoteProduct` from `_search`.
            session: The shared `requests.Session`.
            last_call: The shared throttle timestamp (one-element list).
            single: Whether the request targets a single explicit point.

        Returns:
            pd.DataFrame | None: The parsed frame tagged with
                `lat`/`lon`/`year`/`product`, or `None` when an out-of-coverage
                call is skipped (multi-point bbox only).

        Raises:
            ValueError: When a single explicit point is out of coverage; the
                message names the coordinate and the NREL error.
        """
        assert self._product is not None  # set by _initialize
        lat = product.metadata["lat"]
        lon = product.metadata["lon"]
        name = product.metadata["name"]
        url = _helpers.build_url(
            self._product.endpoint,
            lat,
            lon,
            name,
            self._attributes,
            api_key=self._auth.api_key.get_secret_value(),
            email=self._auth.email,
            interval=self._interval(),
            utc=self._utc,
        )
        resp = _helpers.throttled_get(session, url, last_call=last_call)
        if resp.status_code >= 400:
            message = self._error_message(resp)
            if single:
                raise ValueError(
                    f"NREL returned no data for point (lat={lat}, lon={lon}, "
                    f"names={name}): {message} (the location may be outside "
                    f"{self._product.source.upper()} coverage)."
                )
            logger.warning(
                f"NREL skipped point (lat={lat}, lon={lon}, names={name}): {message}"
            )
            return None
        # Auto-detect the data-table header offset (the `Year,Month,Day,...`
        # line) rather than pinning the catalog's `meta_rows`: it handles the
        # NSRDB (2-row) and WTK (1-row) layouts identically and stays correct if
        # a future PSM revision changes the metadata-header height. The catalog
        # `meta_rows` field documents the expected layout.
        df = _helpers.parse_psm3_csv(resp.text)
        df["lat"] = lat
        df["lon"] = lon
        df["year"] = name
        df["product"] = self._product_id
        return df

    @staticmethod
    def _error_message(resp: Any) -> str:
        """Extract a readable error message from an NREL error response.

        NREL error bodies are JSON (`{"errors": [...]}` / `{"error": {...}}`);
        fall back to the raw (truncated) body when it is not JSON.

        Args:
            resp: The HTTP response with a 4xx/5xx status.

        Returns:
            str: A short human-readable message.
        """
        try:
            payload = resp.json()
        except ValueError:
            return cast("str", resp.text[:200])
        if isinstance(payload, dict):
            errors = payload.get("errors") or payload.get("error")
            if errors:
                return str(errors)
        return str(payload)[:200]

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write the long-format table to `root_dir` and return the path.

        Args:
            df: The canonical long-format frame.

        Returns:
            Path: The written CSV / Parquet file path.

        Raises:
            ImportError: When `output_format="parquet"` but `pyarrow` is
                missing.
        """
        ext = "parquet" if self._output_format == "parquet" else "csv"
        out_path = self.root_dir / f"nrel_{self._product_id}.{ext}"
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
