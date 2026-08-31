"""Backend that fetches ground-station air-quality data from the EEA.

`EEA_AQ(AbstractDataSource)` wraps the `airbase` client over the European
Environment Agency download service and returns reference-grade European
monitor observations as a long-format `pandas.DataFrame` (one row per
measurement), the same `tabular` shape as `earthlens.openaq`.

This is a `tabular` backend: the result is per-row station observations,
not a gridded array, so `OUTPUT_KIND = "tabular"` and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument.

Transport: the EEA service is queried per **country** (ISO2), not per
bbox, and delivers **Parquet** files (via `airbase`). The backend maps
the request bbox to the reporting countries whose own bounding box
intersects it (or an explicit `country=`), picks the dataset era(s)
(`Historical` / `Verified` / `Unverified`) spanning the requested years,
downloads each to a temporary directory, reads and concatenates the
Parquet, and filters the rows to the exact date window. Because the
Parquet carries no coordinates (they live in a separate 100+ MB metadata
export whose keys do not cleanly join), the result is **country-granular**
— every station in each intersecting country — and has no `lat` / `lon`
columns; pass `country=` to be precise. `airbase` is imported lazily so
the `[eea_aq]` extra stays optional.

**Download volume — read this.** `airbase.request()` has **no date
filter**: it downloads *every* Parquet the service holds for a
(dataset-era, country, pollutant) triple, and the requested `[start, end]`
window is applied only *after* the download, in memory. So a one-day query
such as `country="DE", start="2015-06-01", end="2015-06-01"` still pulls
the **whole** `Verified` era (2013–2022) of hourly German PM2.5 —
potentially hundreds of MB to gigabytes — before trimming to one day, and a
bbox spanning several large countries multiplies that. Keep requests
cheap: prefer an explicit small `country=` (e.g. Malta `"MT"`), a single
pollutant, and the tightest year range; a recent year additionally pulls
**both** the `Verified` and `Unverified` eras (see `datasets_for_years`).

Pollutant selection: `variables` is a `list[str]` of pollutant names
(`["pm25"]`, `["pm25", "no2"]`), resolved to airbase `poll` notations via
the bundled catalog.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, cast

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    TemporalExtent,
    to_datetime,
)
from earthlens.eea_aq._helpers import (
    adjacent_eras,
    countries_in_bbox,
    datasets_for_years,
    download_request,
    empty_frame,
    shape_frame,
)
from earthlens.eea_aq.catalog import Catalog

FileFormat = Literal["csv", "parquet"]

#: Default pollutant when `variables` is empty.
_DEFAULT_PARAMETERS = ["pm25"]

#: `temporal_resolution` labels accepted by this backend. EEA validated
#: data is hourly; airbase has no server-side rollup, so the label is
#: recorded for provenance only. `"daily"` is accepted (the facade
#: default).
_ACCEPTED_RESOLUTIONS = frozenset({"hourly", "daily"})

#: Message shown when `airbase` (the `[eea_aq]` extra) is not installed.
_MISSING_AIRBASE = (
    "the eea_aq backend requires the optional 'airbase' dependency. Install "
    "it with `pip install earthlens[eea_aq]` (or `pip install airbase>=1.0`)."
)


class EEA_AQ(AbstractDataSource):
    """EEA air-quality backend (long-format tabular output, via airbase).

    Fetches reference-grade European monitor observations for a bbox (or
    explicit `country=`) / date window / pollutant list through the same
    `download()` shape every other earthlens backend uses, and returns a
    long-format `pandas.DataFrame` (one row per measurement). Results are
    country-granular (see the module docstring).

    There is no authentication — the EEA download service is public.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is per-row station
            observations, so the facade rejects `aggregate=` with
            `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "monitor observations are tabular per-row station data, not gridded rasters, so there is no meaningful gridded reduction"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "hourly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        country: str | list[str] | None = None,
        client: Any | None = None,
        file_format: FileFormat = "csv",
    ):
        """Initialise an EEA backend instance.

        Args:
            start: Inclusive start of the observation window, as a string
                parsed with `fmt`.
            end: Inclusive end of the observation window.
            variables: List of pollutant names to fetch (`["pm25"]`,
                `["pm25", "no2"]`). Resolved to airbase `poll` notations
                via the catalog. An empty list defaults to `["pm25"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees. Used to pick reporting countries when `country`
                is not given.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees.
            temporal_resolution: Recorded for provenance. EEA validated
                data is hourly and airbase has no server-side rollup, so
                this label does not change the request. Accepts
                `"hourly"` (default) or `"daily"` (the facade default).
            path: Output directory for the written CSV / Parquet. Created
                by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            country: Explicit reporting country/countries (ISO2, e.g.
                `"DE"` or `["DE", "FR"]`). When `None` (default) the
                countries are derived from the bbox.
            client: An `airbase.AirbaseClient` (or compatible) to reuse.
                Injectable so tests supply a fake transport; when `None`
                (default) airbase is imported lazily and a client built on
                first use.
            file_format: Output format — `"csv"` (default) or `"parquet"`.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "EEA_AQ `variables` must be a list of pollutant names (e.g. "
                "['pm25', 'no2']), not a mapping."
            )
        self._country = country
        self._client = client
        self._file_format: FileFormat = file_format
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_DEFAULT_PARAMETERS),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a `TemporalExtent`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Provenance label (`"hourly"` or
                `"daily"`); does not change the request.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `temporal_resolution` is not accepted, or
                `start` parses to a date later than `end`.
        """
        if temporal_resolution not in _ACCEPTED_RESOLUTIONS:
            raise ValueError(
                f"temporal_resolution must be one of "
                f"{sorted(_ACCEPTED_RESOLUTIONS)}, got {temporal_resolution!r}."
            )
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _airbase_client(self) -> Any:
        """Return the injected client, or lazily build a real airbase one.

        Returns:
            An `airbase.AirbaseClient` (or the injected stand-in).

        Raises:
            ImportError: When no client was injected and `airbase` (the
                `[eea_aq]` extra) is not installed.
        """
        if self._client is not None:
            return self._client
        try:
            import airbase
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise ImportError(_MISSING_AIRBASE) from exc
        self._client = airbase.AirbaseClient()
        return self._client

    def _resolve_countries(self) -> list[str]:
        """Resolve the request to a list of reporting country ISO2 codes.

        An explicit `country=` wins (normalised to upper-case); otherwise
        the countries whose bounding box intersects the request bbox.

        Returns:
            list[str]: The reporting country ISO2 codes to download.
        """
        if self._country:
            raw = [self._country] if isinstance(self._country, str) else self._country
            return [code.strip().upper() for code in raw]
        return countries_in_bbox(
            (self.space.south, self.space.north),
            (self.space.west, self.space.east),
        )

    def _supported_countries(self, client: Any, countries: list[str]) -> list[str]:
        """Drop countries the airbase client does not serve, order-stable.

        `airbase.request()` raises `ValueError` for any code outside its live
        `client.countries` set, which would abort an otherwise-valid request
        (a bbox can intersect a country airbase does not serve). Intersecting
        here lets the request degrade gracefully to the served countries. A
        client without a `countries` attribute (e.g. a test stand-in) skips
        the filter.

        Args:
            client: The airbase client (or a compatible stand-in).
            countries: The resolved ISO2 country codes.

        Returns:
            list[str]: `countries` keeping only airbase-served codes; a
                dropped code is logged.
        """
        supported = getattr(client, "countries", None)
        if supported is None:
            return countries
        kept = [code for code in countries if code in supported]
        dropped = [code for code in countries if code not in supported]
        if dropped:
            logger.warning(
                f"EEA download: skipping countries not served by the EEA "
                f"download service: {dropped}."
            )
        return kept

    def _window(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        """Return the `[lower, upper)` UTC filter bounds for the request.

        A date-granular end (midnight) is extended by one day so the whole
        end day is inclusive — the common path, and identical to AirNow /
        Sensor.Community. A non-midnight `end` (only reachable via an
        hour-aware `fmt`) yields a **half-open** `[start, end)` window here;
        AirNow instead treats its end hour as inclusive (its API takes an
        hourly range), so hour-granular callers should account for that
        one-endpoint difference.

        Returns:
            tuple[pd.Timestamp, pd.Timestamp]: `(lower, upper)`, tz-aware
                UTC; `upper` is exclusive.
        """
        lower = pd.Timestamp(self.time.start_date, tz="UTC")
        end = self.time.end_date
        if end.hour == 0 and end.minute == 0:
            upper = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        else:
            upper = pd.Timestamp(end, tz="UTC")
        return lower, upper

    def _api(self) -> pd.DataFrame:
        """Download, reshape, concatenate, and window the EEA observations.

        Returns:
            pd.DataFrame: The long-format frame (empty, schema-only, when
                no country intersects the bbox or nothing matched).
        """
        countries = self._resolve_countries()
        if not countries:
            logger.warning(
                "EEA download: no reporting country intersects the request "
                "bbox; returning an empty frame. Pass country= explicitly."
            )
            return empty_frame()
        client = self._airbase_client()
        countries = self._supported_countries(client, countries)
        if not countries:
            logger.warning(
                "EEA download: none of the requested countries are served by "
                "the EEA download service; returning an empty frame."
            )
            return empty_frame()
        polls = self._catalog.polls_for(cast("list[str]", self.vars))
        # Restrict the code -> name map to the requested pollutants so a
        # Parquet that happens to carry extra pollutants never leaks rows the
        # caller did not ask for.
        code_to_name = {
            self._catalog.get_pollutant(name).code: name for name in self.vars
        }
        start_year = self.time.start_date.year
        end_year = self.time.end_date.year
        datasets = datasets_for_years(start_year, end_year)

        # Lazy so a `limit=` stops the work where it costs: each dataset is a
        # separate bulk download of every matching Parquet, so a cap met by the
        # Verified era means the Unverified one is never requested at all.
        non_empty = self._sweep(datasets, client, countries, polls, code_to_name)
        swept = list(datasets)
        if not non_empty:
            # The primary era(s) returned zero files. Retry the adjacent live
            # era(s) not already swept (Verified <-> Unverified) whose year span
            # can plausibly cover the request: a boundary year can be missing
            # from its primary era yet present in the neighbour (a not-yet-
            # promoted year still in the Unverified stream). `adjacent_eras`
            # gates on year overlap so a genuinely out-of-range request (e.g.
            # 2015 against Unverified 2023+) never bulk-downloads an era it
            # cannot be satisfied by. This runs *only* on a fully-empty primary
            # sweep, so the normal success path never double-downloads, and a
            # recent-year request — already spanning both live eras — has no
            # adjacent era left to try. Logged at INFO: the fallback often
            # recovers the data, so it is not on its own an alarm.
            fallback = adjacent_eras(datasets, start_year, end_year)
            if fallback:
                logger.info(
                    f"EEA download: primary era(s) {datasets} returned no files "
                    f"for countries {countries} / pollutants {polls}; retrying the "
                    f"adjacent era(s) {fallback}."
                )
                swept += fallback
                non_empty = self._sweep(
                    fallback, client, countries, polls, code_to_name
                )
        if not non_empty:
            # Nothing from any era, primary or fallback. Only here — with the
            # whole sweep in — is the outage-framed WARNING warranted: zero files
            # across every swept era for the requested countries/pollutants is
            # the shape of an upstream EEA export outage, though it can also be a
            # genuine absence. A per-era WARNING would instead fire even when a
            # sibling era satisfied the request.
            logger.warning(
                f"EEA download: no era returned any usable observations for "
                f"countries {countries} / pollutants {polls} across {swept}; the "
                f"EEA export may be temporarily unavailable upstream, or these "
                f"countries/pollutants are genuinely absent from these eras. "
                f"Returning an empty frame."
            )
            return empty_frame()
        combined = pd.concat(non_empty, ignore_index=True)
        # A recently-promoted year can appear in both Verified and Unverified,
        # so drop rows duplicated across eras. Key on station/pollutant/time +
        # `agg_type` so genuinely-distinct aggregations sharing a timestamp are
        # kept; `validity`/`verification` are deliberately excluded because
        # they legitimately differ across eras for the same measurement (the
        # Verified copy is the more authoritative and, listed first, wins).
        combined = combined.drop_duplicates(
            subset=["station_id", "parameter", "datetime_utc", "agg_type"]
        )
        lower, upper = self._window()
        mask = (combined["datetime_utc"] >= lower) & (combined["datetime_utc"] < upper)
        windowed = combined[mask].reset_index(drop=True)
        if windowed.empty:
            # Files were downloaded and carried rows, but every row fell outside
            # [start, end). Logged at INFO to keep it deliberately distinct from
            # the aggregate outage WARNING (which fires only when *no* files came
            # back): a caller facing an empty frame can tell "the export returned
            # no files" (possible upstream outage) from "files came back, just
            # not for these dates". The wording stays factual — it reports what
            # was downloaded vs the window, without asserting a cause.
            logger.info(
                f"EEA download: {len(combined)} observation(s) were downloaded "
                f"for {countries} / {polls} but none fell within [{lower}, "
                f"{upper}); the swept era(s) returned data outside the requested "
                f"window."
            )
        return windowed

    def _sweep(
        self,
        datasets: list[str],
        client: Any,
        countries: list[str],
        polls: list[str],
        code_to_name: dict[int, str],
    ) -> list[pd.DataFrame]:
        """Download `datasets` era by era and return the non-empty shaped frames.

        Args:
            datasets: The eras to sweep, in priority order.
            client: The airbase client to request through.
            countries: Reporting-country codes the service serves.
            polls: Pollutant codes to request.
            code_to_name: Pollutant code (numeric) to catalog name, restricted
                to the requested pollutants.

        Returns:
            list[pd.DataFrame]: One shaped frame per Parquet that held rows;
                empty frames are dropped. A cap (`self._limit`) is honoured
                lazily, so an era past the cap is never bulk-downloaded.
        """
        frames = self._take_limited(
            self._iter_dataset_frames(datasets, client, countries, polls, code_to_name),
            limit=self._limit,
        )
        return [frame for frame in frames if not frame.empty]

    def _iter_dataset_frames(
        self,
        datasets: list[str],
        client: Any,
        countries: list[str],
        polls: list[str],
        code_to_name: dict[int, str],
    ) -> Iterator[pd.DataFrame]:
        """Yield one shaped frame per Parquet file, era by era.

        A generator rather than a list so a caller that has already collected
        enough rows never triggers the next era's bulk download. Each era's
        temporary directory lives only for that era's reads; abandoning the
        generator early unwinds the open one (`_take_limited` closes what it
        stops consuming).

        Args:
            datasets: The airbase datasets (eras) to sweep, in priority order.
            client: The airbase client to request through.
            countries: Reporting-country codes the service serves.
            polls: Pollutant codes to request.
            code_to_name: Pollutant code (numeric) to catalog name, restricted to the
                requested pollutants.

        Yields:
            pd.DataFrame: One shaped frame per downloaded Parquet file.
        """
        for dataset in datasets:
            with tempfile.TemporaryDirectory(prefix="earthlens_eea_") as tmp:
                request = client.request(dataset, *countries, poll=polls, verbose=False)
                download_request(request, tmp)
                parquets = sorted(Path(tmp).rglob("*.parquet"))
                if not parquets:
                    # Diagnostic only, at INFO: this era returned no files for the
                    # whole requested country/pollutant set. Whether that is an
                    # upstream outage or a genuine absence cannot be judged from a
                    # single era — a sibling era (or the adjacent-era fallback) may
                    # still hold the data — so `_api` owns the outage WARNING once
                    # the whole sweep is in. Warning per era here would cry
                    # "outage" on a download that actually succeeded.
                    logger.info(
                        f"EEA download: era {dataset!r} returned no Parquet files "
                        f"for countries {countries} / pollutants {polls}."
                    )
                for parquet in parquets:
                    yield shape_frame(pd.read_parquet(parquet), dataset, code_to_name)

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Fetch observations, write them to `path`, and return the frame.

        Runs the per-dataset airbase download, reshapes and windows the
        Parquet, writes the long-format result to `path` as CSV (or
        Parquet), and returns it. An empty result returns — and writes — a
        schema-only DataFrame so callers always get the same shape.

        Args:
            progress_bar: Accepted for API parity with the other backends;
                airbase owns its own download progress.
            limit: Cap on the total observations fetched, across every era and
                Parquet file. Applied as each file is read, so an era past the
                cap is never bulk-downloaded. `None` (the default) fetches
                everything. The cap is on rows *fetched*, before the
                cross-era de-duplication and the window filter, so the returned
                frame can be shorter than the cap.

        Returns:
            pd.DataFrame: The long-format observations (schema columns,
                `datetime_utc` tz-aware UTC). Empty (schema-only) when
                nothing matched.
        """
        self._limit = self.check_limit(limit)
        df = self._api()

        out_path = self._output_path()
        if self._file_format == "parquet":
            df.to_parquet(out_path, index=False)
        else:
            df.to_csv(out_path, index=False)

        if len(df):
            logger.info(
                f"EEA download summary: {len(df)} observation(s) across "
                f"{df['station_id'].nunique()} station(s) written to {out_path}"
            )
        else:
            logger.warning(
                "EEA download summary: no observations matched the request; "
                f"wrote an empty (schema-only) frame to {out_path}"
            )
        return df

    def _output_path(self) -> Path:
        """Compose the per-request output file path under `root_dir`."""
        ext = "parquet" if self._file_format == "parquet" else "csv"
        params = "-".join(self.vars)
        start = self.time.start_date.strftime("%Y%m%d")
        end = self.time.end_date.strftime("%Y%m%d")
        return self.root_dir / f"eea_aq_{params}_{start}_{end}.{ext}"
