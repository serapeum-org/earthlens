"""Backend that queries the NASA FIRMS active-fire CSV API over HTTPS.

`FIRMS(AbstractDataSource)` fetches active-fire detections — one point
per fire pixel, with brightness, confidence, and fire-radiative-power —
from the NASA FIRMS (Fire Information for Resource Management System)
area CSV endpoint, across MODIS (C6.1) and VIIRS (S-NPP / NOAA-20 /
NOAA-21) sensors. The rows for a `[start, end]` window over a bbox come
back as CSV, which :mod:`earthlens.firms.events` maps to a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` of fire-pixel
points.

This is a `vector` backend: the on-the-wire result is a table of
geolocated detections, not a gridded array, so `OUTPUT_KIND = "vector"`
and the :class:`earthlens.earthlens.EarthLens` facade rejects an
`aggregate=` argument (there is no meaningful gridded reduction of a
detection table). `download()` returns the in-memory FeatureCollection
and, as a side effect, writes it to one vector file under `path`.

FIRMS needs a free **`MAP_KEY`** — pass it to :meth:`FIRMS.authenticate`
as `api_key=`, or set `FIRMS_MAP_KEY` and let `authenticate()` /
`download()` read it from the environment. It is *not* a constructor
argument: the constructor describes only what to fetch. There is no SDK
and no `[firms]` extra — the only dependencies are `requests` +
`pandas`, both core. Sensor selection follows the vector-backend reading
of `variables` (see the package docstring): `variables` is a `list[str]`
of FIRMS sensor codes (`["VIIRS_SNPP_NRT"]`,
`["MODIS_NRT", "VIIRS_SNPP_NRT"]`); the detection filters ride as
explicit `min_confidence=` / `day_night=` keyword arguments. The
temporal window is chunked internally into ≤5-day requests (the FIRMS
per-request cap), so `temporal_resolution` carries the sentinel `"all"`.
"""

from __future__ import annotations

import datetime as dt
import time
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
from earthlens.firms import events
from earthlens.firms._helpers import chunk_windows, classify_body, firms_get
from earthlens.firms.auth import AuthenticationError, FirmsAuth, FirmsCredentials
from earthlens.firms.catalog import Catalog

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection


#: FIRMS area-CSV endpoint. Filled with the MAP_KEY, sensor, bbox
#: (W,S,E,N), day_range, and start_date path segments.
AREA_URL_TEMPLATE = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
    "{map_key}/{sensor}/{bbox}/{day_range}/{start_date}"
)

#: Default sensor when `variables=[]` — the highest-resolution current
#: NRT sensor.
_DEFAULT_SENSORS = ["VIIRS_SNPP_NRT"]

#: Approximate NRT retention: `*_NRT` sensors only hold roughly the last
#: two months, so a request older than this against an NRT sensor warns
#: (and names the `*_SP` archive variant).
NRT_RETENTION_DAYS = 60

#: When a request fans out to more than this many `(sensor, chunk)` GETs,
#: `_search` warns: FIRMS allows ~5000 transactions / 10 min and each GET
#: is one transaction, so a very wide window x many sensors can approach
#: the quota (the per-request back-off then paces it, but a heads-up is
#: cheaper than discovering it mid-download).
FANOUT_WARN_THRESHOLD = 50

FileFormat = Literal["gpkg", "geojson"]

#: Map output format to the OGR driver and file extension `to_file` uses.
_DRIVERS: dict[str, tuple[str, str]] = {
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}


class FIRMS(AbstractDataSource):
    """NASA FIRMS active-fire backend (vector point-feature output).

    Wraps the FIRMS area CSV API so a user can pull a space/time/sensor
    window of fire detections through the same `download()` shape every
    other earthlens backend uses. Windows longer than the FIRMS 5-day
    per-request cap are chunked, and each `(sensor, ≤5-day chunk)` is
    one CSV GET; the rows are mapped to a
    :class:`~pyramids.feature.collection.FeatureCollection`.

    FIRMS needs a free `MAP_KEY`. Supply it to :meth:`authenticate` as
    `api_key=`, or set the `FIRMS_MAP_KEY` environment variable and let
    `authenticate()` / `download()` resolve it. Credentials are not a
    constructor argument — the constructor describes only what to fetch.

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of detection
            features, so the facade rejects `aggregate=` with
            `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = "fire detections are vector point features, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate= and post-process the returned FeatureCollection (a GeoDataFrame) directly"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        min_confidence: float | None = None,
        day_night: str | None = None,
        file_format: FileFormat = "gpkg",
        timeout: float = 60.0,
    ):
        """Initialise a FIRMS backend instance.

        Args:
            start: Inclusive start of the detection window, as a string
                parsed with `fmt`.
            end: Inclusive end of the detection window.
            variables: List of FIRMS sensor codes to query
                (`["VIIRS_SNPP_NRT"]`, `["MODIS_NRT", "VIIRS_SNPP_NRT"]`).
                For this backend `variables` names the *sensors*, not
                data variables (see the package docstring). An empty list
                defaults to `["VIIRS_SNPP_NRT"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: FIRMS chunks by ≤5-day windows
                internally, not by a daily/monthly cadence, so this is
                the sentinel `"all"`, not a pandas frequency alias.
            path: Output directory for the written vector file. Created
                by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            min_confidence: Optional 0-100 lower bound applied
                client-side on the normalised `confidence_pct` column
                (FIRMS has no server-side confidence filter). `None`
                keeps every detection.
            day_night: Optional `"D"` / `"N"` filter applied client-side
                on the `daynight` column. `None` keeps both.
            file_format: Output vector format — `"gpkg"` (default,
                GeoPackage) or `"geojson"`.
            timeout: Per-request timeout in seconds for each CSV GET.

        Raises:
            ValueError: If `file_format` is not `"gpkg"` / `"geojson"`.
            TypeError: If `variables` is a mapping rather than a list of
                sensor codes.
        """
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got {file_format!r}."
            )
        if isinstance(variables, dict):
            raise TypeError(
                "FIRMS `variables` must be a list of sensor codes (e.g. "
                "['VIIRS_SNPP_NRT', 'MODIS_NRT']), not a mapping. For this "
                "backend `variables` selects sensors, not data variables; the "
                "detection filters are the explicit min_confidence= / "
                "day_night= keyword arguments."
            )
        self._min_confidence = min_confidence
        self._day_night = day_night
        self._file_format: FileFormat = file_format
        self._timeout = timeout
        self._catalog = Catalog()
        # Reactive back-off knobs (G2); the sleep is an instance attr so
        # it can be swapped for a no-op in tests.
        self._sleep = time.sleep
        self._max_retries = 5
        self._backoff_factor = 1.0
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_DEFAULT_SENSORS),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self) -> FirmsAuth:
        """Build the (unconfigured) :class:`FirmsAuth` holder.

        No credentials are resolved here — the constructor describes only
        what to fetch. The `MAP_KEY` is resolved later by
        :meth:`authenticate` (explicitly via `api_key=`, or from the
        `FIRMS_MAP_KEY` environment variable), which `download()` also
        triggers lazily if it has not run.

        Returns:
            FirmsAuth: An unconfigured auth; `is_authenticated()` is
                `False` until :meth:`authenticate` resolves a key.
        """
        return FirmsAuth(FirmsCredentials(api_key=None))

    def authenticate(self, api_key: str | None = None) -> FIRMS:
        """Resolve the FIRMS `MAP_KEY` and arm the backend for download.

        The explicit, fail-fast credential step. Pass `api_key=` to use a
        key directly; omit it (or pass `None`) to read the `FIRMS_MAP_KEY`
        environment variable. Either way the resolved key is held for the
        subsequent :meth:`download`. Calling it again with a different
        `api_key` re-arms with the new key. `download()` calls this with
        no argument on your behalf if you never do, so an explicit call is
        only needed to pass a key directly or to validate up front.

        Args:
            api_key: The FIRMS `MAP_KEY` to use. When `None`, the
                `FIRMS_MAP_KEY` environment variable is read instead.

        Returns:
            The backend instance, so it chains
            `EarthLens(...).authenticate(api_key=...).download()`.

        Raises:
            AuthenticationError: If `api_key` is `None` and no
                `FIRMS_MAP_KEY` environment variable is set.

        Examples:
            - Arm the backend with an explicit key and read it back:
                ```python
                >>> import tempfile
                >>> from earthlens.firms import FIRMS
                >>> backend = FIRMS(
                ...     start="2024-08-01", end="2024-08-01",
                ...     variables=["VIIRS_SNPP_NRT"],
                ...     lat_lim=[33.0, 35.0], lon_lim=[-119.0, -117.0],
                ...     path=tempfile.mkdtemp(),
                ... )
                >>> backend.authenticate(api_key="demo-key").client.api_key
                'demo-key'

                ```
            - A fresh backend is unauthenticated until the key resolves:
                ```python
                >>> import tempfile
                >>> from earthlens.firms import FIRMS
                >>> backend = FIRMS(
                ...     start="2024-08-01", end="2024-08-01",
                ...     variables=["VIIRS_SNPP_NRT"],
                ...     lat_lim=[33.0, 35.0], lon_lim=[-119.0, -117.0],
                ...     path=tempfile.mkdtemp(),
                ... )
                >>> backend.client.is_authenticated()
                False
                >>> backend.authenticate(api_key="abc123").client.is_authenticated()
                True

                ```
        """
        auth = FirmsAuth(
            FirmsCredentials(
                api_key=SecretStr(api_key) if api_key is not None else None
            )
        )
        auth.configure()
        self.client = auth
        return self

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        FIRMS chunks the window into ≤5-day requests internally (see
        :meth:`_search`), so the resolution is kept as the sentinel
        `"all"` (not a real pandas frequency alias) and `dates`
        collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label;
                FIRMS always chunks the full window.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="all")

    def _search(self) -> list[RemoteProduct]:
        """List one :class:`RemoteProduct` per `(sensor, ≤5-day chunk)`.

        Validates each code in `self.vars` against the bundled catalog
        (raising with a did-you-mean hint on an unknown sensor), warns
        when the requested window falls outside an `*_NRT` sensor's
        coverage (naming the `*_SP` archive variant — it does *not*
        auto-swap), and walks the `[start, end]` window in ≤5-day
        chunks. No network call is made here.

        Returns:
            list[RemoteProduct]: One product per `(sensor, chunk)`, whose
                `metadata` carries `sensor`, `family`, `start_date`, and
                `day_range`. The product `id` is `f"{sensor}:{start}"`.

        Raises:
            ValueError: If a code in `self.vars` is not a registered
                FIRMS sensor.
        """
        start_date = self.time.start_date.date()
        end_date = self.time.end_date.date()
        windows = chunk_windows(start_date, end_date)
        total_gets = len(self.vars) * len(windows)
        logger.info(
            f"FIRMS request: {len(self.vars)} sensor(s) x {len(windows)} chunk(s) "
            f"= {total_gets} CSV GET(s)"
        )
        if total_gets > FANOUT_WARN_THRESHOLD:
            logger.warning(
                f"FIRMS request fans out to {total_gets} CSV GET(s) (one "
                f"transaction each); FIRMS allows ~5000 per rolling 10 minutes. "
                "The per-request back-off will pace this, but consider narrowing "
                "the window or sensor list for a large pull."
            )
        products: list[RemoteProduct] = []
        non_percent: list[str] = []
        for code in self.vars:
            sensor = self._catalog.get_sensor(code)
            self._warn_if_out_of_coverage(sensor, start_date, end_date)
            if (
                self._min_confidence is not None
                and sensor.family not in events.PERCENT_CONFIDENCE_FAMILIES
            ):
                non_percent.append(code)
            for chunk_start, day_range in windows:
                products.append(
                    RemoteProduct(
                        id=f"{code}:{chunk_start.isoformat()}",
                        metadata={
                            "sensor": code,
                            "family": sensor.family,
                            "start_date": chunk_start,
                            "day_range": day_range,
                        },
                    )
                )
        if non_percent:
            logger.warning(
                f"min_confidence={self._min_confidence} is not applied to "
                f"{non_percent}: their confidence is a provider-scale (non "
                "0-100) value, so thresholding would drop every detection; "
                "those sensors' detections are kept unfiltered."
            )
        return products

    def _warn_if_out_of_coverage(
        self, sensor, start_date: dt.date, end_date: dt.date
    ) -> None:
        """Warn (do not auto-swap) when the window is outside coverage.

        An `*_NRT` sensor holds only roughly the last
        :data:`NRT_RETENTION_DAYS` days; a request for older data returns
        a silently empty CSV rather than an error. This logs a loud
        warning naming the `*_SP` archive variant when that variant
        exists. A request that predates the sensor's mission start is
        warned the same way.

        Args:
            sensor: The resolved :class:`~earthlens.firms.Sensor`.
            start_date: Requested inclusive start.
            end_date: Requested inclusive end.
        """
        # `temporal.start` / `temporal.end` are typed `datetime.date | None`
        # on the catalog model, so no datetime-narrowing is needed here.
        mission_start = sensor.temporal.start
        if mission_start is not None and start_date < mission_start:
            logger.warning(
                f"{sensor.code} coverage begins {mission_start}; the requested "
                f"window starts {start_date} and may return no detections."
            )
        coverage_end = sensor.temporal.end
        if coverage_end is not None and end_date > coverage_end:
            logger.warning(
                f"{sensor.code} coverage ends {coverage_end}; the requested "
                f"window ends {end_date} and may return no detections past the "
                "coverage end."
            )
        # The NRT-retention heuristic below is advisory (retention drifts
        # per sensor); it only applies to NRT sensors.
        if sensor.temporal.quality != "NRT":
            return
        cutoff = dt.date.today() - dt.timedelta(days=NRT_RETENTION_DAYS)
        if end_date < cutoff:
            sp_variant = sensor.code.replace("_NRT", "_SP")
            hint = (
                f" for archive data use {sp_variant}"
                if sp_variant in self._catalog
                else ""
            )
            logger.warning(
                f"{sensor.code} is near-real-time and covers only the last "
                f"~{NRT_RETENTION_DAYS} days; the requested window ending "
                f"{end_date} is older and will likely be empty{hint}."
            )

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Fetch each product's CSV and map it to a FeatureCollection.

        Widens the inherited `-> list[Path]` contract: a vector backend
        returns in-memory :class:`FeatureCollection`s, not file paths.
        Each product is one CSV GET issued through the quota back-off
        (`G2`); the response body is classified before parsing (`G6`) so
        a FIRMS error-as-HTTP-200 text body never reaches
        `pandas.read_csv`.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[FeatureCollection]: One collection per product, in the
                same order.
        """
        return [self._fetch_one(product) for product in products]

    def _fetch_one(self, product: RemoteProduct) -> FeatureCollection:
        """Fetch and map one `(sensor, chunk)` product.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            FeatureCollection: The chunk's detections (schema-only empty
                when the CSV had no rows).

        Raises:
            AuthenticationError: If the body is a bad-key message (`G6`).
            RuntimeError: If the body is a non-CSV error, or a quota body
                survives the back-off retries.
            requests.HTTPError: On a non-quota HTTP error status.
        """
        url = self._build_url(product)
        response = firms_get(
            url,
            timeout=self._timeout,
            get=requests.get,
            sleep=self._sleep,
            max_retries=self._max_retries,
            backoff_factor=self._backoff_factor,
        )
        status = getattr(response, "status_code", 200)
        if status >= 400:
            # Do NOT call response.raise_for_status(): its message embeds
            # the request URL, which carries the MAP_KEY as a path segment
            # and would leak the secret into logs/tracebacks. Raise a
            # redacted HTTPError instead.
            raise requests.HTTPError(
                f"FIRMS area request for sensor {product.metadata['sensor']} "
                f"failed with HTTP {status} (URL omitted to avoid leaking the "
                "MAP_KEY)."
            )
        text = response.text
        kind = classify_body(text)
        if kind == "auth":
            raise AuthenticationError(f"FIRMS rejected the MAP_KEY: {_truncate(text)}")
        if kind == "quota":
            raise RuntimeError(
                "FIRMS transaction quota exhausted after back-off retries: "
                f"{_truncate(text)}"
            )
        if kind == "error":
            raise RuntimeError(
                f"FIRMS returned a non-CSV error body: {_truncate(text)}"
            )
        frame = pd.read_csv(StringIO(text))
        return events.csv_to_fc(
            frame,
            sensor=product.metadata["sensor"],
            family=product.metadata["family"],
            min_confidence=self._min_confidence,
            day_night=self._day_night,
        )

    def _build_url(self, product: RemoteProduct) -> str:
        """Compose the FIRMS area-CSV URL for one product.

        The bbox path segment is `W,S,E,N` (FIRMS area order). The
        `MAP_KEY` is read from the configured :class:`FirmsAuth`.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            str: The fully-formed request URL.
        """
        bbox = (
            f"{self.space.west},{self.space.south},{self.space.east},{self.space.north}"
        )
        return AREA_URL_TEMPLATE.format(
            map_key=self.client.api_key,
            sensor=product.metadata["sensor"],
            bbox=bbox,
            day_range=product.metadata["day_range"],
            start_date=product.metadata["start_date"].isoformat(),
        )

    def _api_via_search_fetch_with_progress(
        self, progress_bar: bool
    ) -> list[FeatureCollection]:
        """C3 composition with a per-chunk progress bar.

        Mirrors the CMEMS / OpenAQ progress-aware composition: run the
        cheap :meth:`_search`, then map :meth:`_fetch_one` over the
        products wrapped in a `tqdm` bar (disabled when `progress_bar`
        is `False`). Short-circuits on an empty search.

        Args:
            progress_bar: Show the per-chunk `tqdm` bar when `True`.

        Returns:
            list[FeatureCollection]: One collection per product, or `[]`
                when nothing matched.
        """
        return self._search_fetch_each(
            progress_bar=progress_bar, desc="FIRMS chunks", unit="chunk"
        )

    def download(
        self,
        progress_bar: bool = True,
    ) -> FeatureCollection:
        """Query FIRMS and return the matched detections.

        Runs the cheap :meth:`_search` (sensor validation + chunk
        planning) then the throttled :meth:`_fetch` (one CSV GET per
        chunk), concatenates the per-chunk collections into one
        FeatureCollection, writes it to one vector file under `path`, and
        returns it. An empty result returns — and writes nothing for — a
        schema-correct empty FeatureCollection.

        Args:
            progress_bar: Show a per-chunk progress bar. Defaults to
                `True`.

        Returns:
            FeatureCollection: The matched detections, CRS `EPSG:4326`.
                Empty (schema-only) when nothing matched.
        """
        # Resolve the MAP_KEY from FIRMS_MAP_KEY if authenticate() was not
        # called explicitly, so EarthLens(...).download() still works when
        # the key lives in the environment.
        if not self.client.is_authenticated():
            self.authenticate()

        collections = self._api_via_search_fetch_with_progress(progress_bar)
        collection = events.concat(collections)
        # `concat` copied every chunk; keeping the per-chunk collections alive
        # through the write and the return doubles the request's footprint.
        collections.clear()

        if len(collection):
            out_path = self._write(collection)
            logger.info(
                f"FIRMS download summary: {len(collection)} detection(s) "
                f"written to {out_path}"
            )
        else:
            logger.warning(
                "FIRMS download summary: no detections matched the request, "
                "nothing written"
            )
        return collection

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the detections to one vector file under `root_dir`.

        The filename embeds the sensor list and the query's date window
        so successive downloads into the same `path` yield distinct
        files. Two downloads of the same request overwrite, the intended
        idempotent behaviour.

        Args:
            collection: The detections to write.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _DRIVERS[self._file_format]
        sensors = "-".join(self.vars)
        stem = (
            f"firms_{sensors}_{self.time.start_date:%Y%m%d}_{self.time.end_date:%Y%m%d}"
        )
        out_path = self.root_dir / f"{stem}.{ext}"
        collection.to_file(str(out_path), driver=driver)
        return out_path


def _truncate(text: str, limit: int = 200) -> str:
    """Return a single-line, length-capped slice of an error body.

    Args:
        text: The raw response body.
        limit: Maximum characters to keep.

    Returns:
        str: The body collapsed to one line and truncated for logging.
    """
    flattened = " ".join(text.split())
    return flattened[:limit]
