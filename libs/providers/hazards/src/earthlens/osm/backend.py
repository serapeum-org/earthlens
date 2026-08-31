"""Backend that fetches OpenStreetMap features over Overpass + ohsome + pbf.

`OSM(AbstractDataSource)` routes a request to one of three public, keyless OSM
query protocols and returns the result as a pyramids
`~pyramids.feature.collection.FeatureCollection` (CRS `EPSG:4326`):

* **Overpass** (`overpy`) — small/targeted **current-state** features by bbox +
  tag filter. The backend POSTs the Overpass QL itself with core `requests`
  (a descriptive `User-Agent` — the canonical `overpass-api.de` returns HTTP
  406 without one — and its own timeout), then parses the JSON with
  `overpy.Overpass().parse_json(...)` and builds geometry from the parsed
  elements (`earthlens.osm.overpy_to_gdf`). This deviates from a bare
  `overpy.Overpass().query(...)` deliberately: overpy 0.7 sends no custom UA,
  so its `.query()` cannot reach overpass-api.de (see the A1 capture).
* **ohsome** (`ohsome`) — OSM **history + analytics** via the
  `elements/geometry` endpoint. `OhsomeClient(...).post(endpoint=
  "elements/geometry", ...).as_dataframe()` already returns a geopandas
  `GeoDataFrame`, wrapped straight into a `FeatureCollection` (no `xarray`,
  `G7`); a `403` / `429` throttle from the public endpoint is surfaced as a
  typed `OhsomeUnavailableError` (`_fetch_ohsome`).
* **pbf** (`pyrosm` / `pyosmium`) — **bulk / regional** reads from a Geofabrik
  `.osm.pbf` extract (`G9`, `G10`). The backend fetch-and-caches the extract
  for the request's `region=` (via `download_extract`) and reads the row's
  layer (`pyrosm_method`) into a `FeatureCollection` with the selected engine
  (`read_pbf`), clipping to the request bbox. `pyrosm`/`pyosmium` are
  OSM-domain SDKs, so this reader lives in earthlens — not pyramids — by
  maintainer decision (`G9`).

A request names one or more curated **named queries** (`variables=
["overpass:hospitals"]`, `variables=["pbf:buildings"]`) plus a bbox; the
catalog row's `protocol` picks the branch (`G2`, `G10`). A raw `query=`
(Overpass QL) / `filter=` (ohsome) override is accepted for power users (`G6`).
OSM data is **ODbL** (share-alike), so every successful `download()` emits a
`LicenseWarning` (`G5`).

This is a `vector` backend (`OUTPUT_KIND = "vector"`), so the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument and
`download(aggregate=...)` raises `NotImplementedError` (`G1`). All three
protocols are public — there is no auth class (`G6`) — and the SDKs are
imported lazily (Overpass/ohsome inside `_fetch`; pyrosm/pyosmium inside
`read_pbf`), so the package imports without `earthlens[osm]` /
`earthlens[osm-pbf]`. ohsome's aggregation endpoints remain out of scope.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    to_datetime,
)
from earthlens.base.http import DEFAULT_TIMEOUT, HttpClient
from earthlens.config import cache_dir
from earthlens.osm._helpers import (
    LicenseWarning,
    OhsomeResponseError,
    OhsomeUnavailableError,
    bbox_swne,
    bbox_wsen,
    empty_fc,
    ohsome_body_preview,
    ohsome_error_response,
    ohsome_http_status,
    ohsome_response_is_non_json,
    overpy_to_gdf,
    to_fc,
)
from earthlens.osm._pbf import Engine, download_extract, read_pbf
from earthlens.osm.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection


#: Canonical public Overpass endpoint. Overridable via `endpoint=`.
OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"

#: Descriptive User-Agent sent on every Overpass POST. overpass-api.de returns
#: HTTP 406 for requests with no / a default UA (verified in A1), so a real one
#: is required, not optional. Overridable via `user_agent=`.
USER_AGENT = "earthlens (+https://github.com/serapeum-org/earthlens)"

#: ODbL attribution string surfaced in the per-result `LicenseWarning`.
ODBL_NOTICE = (
    "OpenStreetMap data is licensed under the Open Database License (ODbL 1.0), "
    "which carries attribution and share-alike obligations: credit "
    "'(c) OpenStreetMap contributors' and license any derived database under "
    "ODbL when redistributing."
)


def default_pbf_cache_dir() -> Path:
    """The directory `.osm.pbf` extracts are cached in when none is given.

    Resolved per call from the shared earthlens cache directory
    (`set_cache_dir()` / `EARTHLENS_CACHE`), so redirecting that moves the
    extracts with it.

    Returns:
        Path: `<cache_dir()>/osm_pbf`.
    """
    return cache_dir() / "osm_pbf"


FileFormat = Literal["geojson", "gpkg"]

#: Map output format to the OGR driver and file extension `to_file` uses.
_DRIVERS: dict[str, tuple[str, str]] = {
    "geojson": ("GeoJSON", "geojson"),
    "gpkg": ("GPKG", "gpkg"),
}

#: Default bbox-area cap, in square degrees, for the live-query footgun guard.
#: OSM is for small/targeted queries; a box larger than this (a continent, or
#: the whole-Earth `[-90, 90]` x `[-180, 180]` default a facade caller gets when
#: they omit `lat_lim` / `lon_lim`, ~64 800 square degrees) would hammer the
#: shared public services planet-wide. ~100 square degrees comfortably covers a
#: large country (France is ~64); raise it with `max_bbox_deg2=` for a genuinely
#: larger area.
_DEFAULT_MAX_BBOX_DEG2 = 100.0

#: Single-axis span cap, in degrees, applied under the default area cap so a
#: degenerate thin-but-globe-spanning box (e.g. `0.1° x 360°`, ~36 square
#: degrees) is still rejected — the area cap alone would let it through.
_MAX_BBOX_SPAN_DEG = 90.0


class _RequestsHttp:
    """Session-like adapter routing GET/POST through the module `requests`.

    Keeps :class:`~earthlens.base.http.HttpClient` pointed at the module-level
    `requests.get` / `requests.post` (rather than a private session) so this
    single-shot Overpass POST stays a fresh connection per call, and so tests
    that monkeypatch `earthlens.osm.backend.requests.post` still drive the
    transport (`HttpClient._send` calls `getattr(session, "post")` first, so
    a shim with both `get` and `post` works for either verb).
    """

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Issue a GET via the module-level `requests.get`."""
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return requests.get(url, **kwargs)  # nosec B113 - default timeout applied above

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Issue a POST via the module-level `requests.post`."""
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return requests.post(url, **kwargs)  # nosec B113 - default timeout applied above


class OSM(AbstractDataSource):
    """OpenStreetMap feature backend (vector FeatureCollection output).

    Wraps the public Overpass + ohsome services so a user can pull a
    bbox window of OSM features through the same `download()` shape every
    other earthlens backend uses. A named query (`variables=
    ["overpass:hospitals"]`) selects the protocol and tag filter; the
    backend runs the live query, converts the result to a
    `~pyramids.feature.collection.FeatureCollection` (EPSG:4326), emits an
    ODbL `LicenseWarning`, optionally writes it to one vector file under
    `path`, and returns it.

    Neither protocol needs credentials.

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of features, so the
            facade rejects `aggregate=` with `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    #: Overpass' usage policy asks for a single request at a time and a
    #: pause between them; ohsome rate-limits per user. One second is the
    #: commonly cited floor for both, and it is a floor, not a target —
    #: raise it for a large sweep. Before this, `min_interval` existed on
    #: HttpClient and no call site ever set it, so nothing in earthlens
    #: paced itself against any provider.
    MIN_REQUEST_INTERVAL: float = 1.0

    #: Retry budget for the ohsome `elements/geometry` request. `api.ohsome.org`
    #: throttles anonymous callers with `429` (and can `5xx` transiently), so the
    #: SDK's transport is handed a urllib3 `Retry` with this many retries,
    #: exponential `backoff_factor` growth, and `Retry-After` honoured — the same
    #: policy the repo-wide `HttpClient` applies. `403` is deliberately *not*
    #: retried: on a public, keyless endpoint it is a hard block, not transient.
    #: (The ohsome SDK adds one final no-retry attempt of its own after a
    #: `RetryError`, so the effective request count is this budget plus one.)
    MAX_OHSOME_RETRIES: int = 5
    OHSOME_BACKOFF_FACTOR: float = 1.0

    #: Ceiling (whole seconds) on any single ohsome retry wait — the exponential
    #: backoff and a server-sent `Retry-After` alike — mirroring HttpClient's
    #: `DEFAULT_MAX_BACKOFF` (300 s) so a hostile/misconfigured `Retry-After`
    #: cannot pin the calling thread for an unbounded interval. Kept an `int`
    #: because urllib3's `retry_after_max` is integer-typed.
    OHSOME_MAX_BACKOFF: int = 300

    AGGREGATE_REFUSAL_REASON = "OSM features are vector, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate= and post-process the returned FeatureCollection (a GeoDataFrame) directly"

    #: An Overpass current-state query has no window; ohsome supplies its own, so a
    #: missing `start` / `end` is legal here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        variables: list[str] | str,
        lat_lim: list[float],
        lon_lim: list[float],
        start: str | None = None,
        end: str | None = None,
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        query: str | None = None,
        filter: str | None = None,
        endpoint: str = OVERPASS_ENDPOINT,
        user_agent: str = USER_AGENT,
        timeout: float = 180.0,
        file_format: FileFormat = "geojson",
        max_bbox_deg2: float | None = None,
        region: str | None = None,
        engine: Engine = "pyrosm",
        cache_dir: Path | str | None = None,
    ):
        """Initialise an OSM backend instance.

        Args:
            variables: One or more named-query ids to fetch
                (`["overpass:hospitals"]`, `["ohsome:buildings"]`, or
                several at once). For this backend `variables` selects
                *named queries*, not data variables. A bare string is
                wrapped into a one-element list.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in degrees,
                both in `[-90, 90]`. Required.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`. Required.
            start: Inclusive start date string (parsed with `fmt`). Used to
                build the ohsome `time` window; ignored by Overpass
                (current-state). `None` is allowed for Overpass-only
                requests.
            end: Inclusive end date string. With `start`, forms the ohsome
                `time` range `"start/end"`; ignored by Overpass.
            temporal_resolution: Sentinel `"all"` — OSM is queried in one
                shot, not chunked by day/month.
            path: Output directory for the written vector file. Created by
                the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            query: Optional raw Overpass QL override applied to every
                requested `overpass` query (a `{bbox}` placeholder, if
                present, is filled with the request bbox). It must request
                JSON output (`[out:json]`) — the response is parsed with
                `overpy.Overpass().parse_json`, so an `[out:xml]` / `[out:csv]`
                override would fail to parse. Power-user escape hatch (`G6`).
            filter: Optional raw ohsome filter override applied to every
                requested `ohsome` query. Power-user escape hatch (`G6`).
            endpoint: Overpass API endpoint URL. Defaults to the canonical
                `overpass-api.de`.
            user_agent: `User-Agent` header sent on every Overpass POST.
                A descriptive value is required (overpass-api.de 406s
                without one).
            timeout: HTTP timeout in seconds for the Overpass POST; also
                fills the QL `[timeout:N]` server budget.
            file_format: Output vector format — `"geojson"` (default, robust
                for mixed geometry types) or `"gpkg"`.
            max_bbox_deg2: Optional override of the bbox-area cap (square
                degrees) that guards against a planet-wide live query (the
                whole-Earth default a facade caller gets when they omit
                `lat_lim` / `lon_lim`). `None` uses the built-in
                `100.0`-square-degree default; pass a larger value for a
                genuinely larger area. The cap is **not** applied to a
                `pbf` request (a local extract read is not a live-service
                footgun).
            region: The Geofabrik region for a `pbf` request — a key from the
                catalog's `regions:` table (`"malta"`, `"netherlands"`, …) or a
                raw Geofabrik path (`"europe/andorra"`). Required when any
                requested query is a `pbf:*` layer, ignored otherwise.
            engine: The `pbf` read engine — `"pyrosm"` (in-memory, the default)
                or `"pyosmium"` (streaming, for planet-scale extracts). Ignored
                by the `overpass` / `ohsome` protocols.
            cache_dir: Directory the fetched `.osm.pbf` extracts are cached in.
                `None` uses `default_pbf_cache_dir()` (a cross-run user cache
                under the shared earthlens cache directory).

        Raises:
            TypeError: If `variables` is a mapping rather than a list / string
                of named-query ids.
            ValueError: If `variables` is empty, `file_format` is not
                `"geojson"` / `"gpkg"`, or `engine` is not `"pyrosm"` /
                `"pyosmium"`.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "OSM `variables` must be a list of named-query ids (e.g. "
                "['overpass:hospitals']), not a mapping. For this backend "
                "`variables` selects named queries; a raw query is the explicit "
                "query= / filter= keyword argument."
            )
        if isinstance(variables, str):
            variables = [variables]
        if not variables:
            raise ValueError(
                "OSM `variables` is empty; supply at least one named-query id, "
                "e.g. variables=['overpass:hospitals']."
            )
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got {file_format!r}."
            )
        if engine not in ("pyrosm", "pyosmium"):
            raise ValueError(f"engine must be 'pyrosm' or 'pyosmium', got {engine!r}.")
        self._query = query
        self._filter = filter
        self._endpoint = endpoint
        self._user_agent = user_agent
        self._timeout = timeout
        self._file_format: FileFormat = file_format
        self._max_bbox_deg2 = max_bbox_deg2
        self._region = region
        self._engine: Engine = engine
        self._cache_dir_arg = cache_dir
        # Built on first use and reused, so `MIN_REQUEST_INTERVAL` actually
        # paces successive queries: the interval is enforced from a timestamp
        # the client carries, which a per-query client would always reset.
        self._overpass_http: HttpClient | None = None
        self._pbf_http: HttpClient | None = None
        self._catalog = Catalog()
        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=list(variables),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )
        # OSM is queried in one shot — pin the sentinel even when the facade
        # forwards its default cadence, so the attribute never misrepresents a
        # temporal cadence the backend does not have.
        self.temporal_resolution = "all"

    @property
    def _cache_dir(self) -> Path:
        """The directory `.osm.pbf` extracts are cached in.

        Resolved per call, so a later `set_cache_dir()` moves the cache the same
        way it does for the other backends that hang off the shared directory.

        Returns:
            Path: The `cache_dir=` argument, else `default_pbf_cache_dir()`.
        """
        if self._cache_dir_arg:
            return Path(self._cache_dir_arg)
        return default_pbf_cache_dir()

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the optional `[start, end]` window into a `TemporalExtent`.

        OSM is queried in one shot, so there is no per-date loop. Overpass
        ignores the window (current-state); ohsome uses it for its `time`
        parameter. Both bounds are optional — an Overpass-only request needs
        none.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Recorded as the resolution label; OSM
                always queries in one shot.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model; `start_date` / `end_date` are
                `None` when the corresponding argument was `None`.
        """
        start_dt = to_datetime(start, fmt) if start else None
        end_dt = to_datetime(end, fmt) if end else None
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([]),
        )

    def _search(self) -> list[RemoteProduct]:
        """Plan one `RemoteProduct` per requested named query.

        Resolves every id in `self.vars` against the bundled catalog
        (raising with a did-you-mean hint on an unknown id) and records the
        resolved `Dataset` on each product's metadata. No network call is
        made here.

        Returns:
            list[RemoteProduct]: One product per requested query id; each
                `id` is the query id and `metadata["dataset"]` carries the
                `Dataset` row.

        Raises:
            ValueError: If an id in `self.vars` is not a registered named
                query, the requested bbox exceeds the area cap (live protocols
                only), or a `pbf:*` query was requested without a `region=`.
        """
        products = [
            RemoteProduct(
                id=query_id,
                metadata={"dataset": self._catalog.get(query_id)},
            )
            for query_id in self.vars
        ]
        protocols = {product.metadata["dataset"].protocol for product in products}
        # The area cap guards the shared live services; a `pbf` read hits a
        # local extract, so it is only applied when a live query is present.
        if protocols & {"overpass", "ohsome"}:
            self._guard_bbox()
        if "pbf" in protocols and self._region is None:
            examples = ", ".join(self._catalog.region_ids()[:3])
            raise ValueError(
                "a pbf:* query needs a Geofabrik region: pass region= (a key "
                f"from the catalog, e.g. {examples}, or a raw 'continent/region' "
                "path)."
            )
        return products

    def _guard_bbox(self) -> None:
        """Reject a bbox large enough to be a planet-wide live-query footgun.

        OSM is for small/targeted queries; an oversized box (a continent, or the
        whole-Earth default a facade caller gets when they omit `lat_lim` /
        `lon_lim`) would hammer the shared public services. Raises when the bbox
        area exceeds the cap (`max_bbox_deg2`, else `100.0` square degrees). With
        the default cap it *also* rejects a degenerate box that slips under the
        area cap yet still spans (near) the whole globe on one axis (e.g. a thin
        `0.1° x 360°` strip); an explicit `max_bbox_deg2` is taken as "I know
        what I'm doing" and relaxes the single-axis guard too.

        Raises:
            ValueError: If the requested bbox area (or, under the default cap, a
                single-axis span) is too large.
        """
        cap = (
            self._max_bbox_deg2
            if self._max_bbox_deg2 is not None
            else _DEFAULT_MAX_BBOX_DEG2
        )
        width = self.space.east - self.space.west
        height = self.space.north - self.space.south
        area = width * height
        over_span = self._max_bbox_deg2 is None and (
            width > _MAX_BBOX_SPAN_DEG or height > _MAX_BBOX_SPAN_DEG
        )
        if area > cap or over_span:
            raise ValueError(
                f"The requested bbox ({width:.1f}deg x {height:.1f}deg, "
                f"{area:.1f} square degrees) is too large for a live OSM query "
                "(an oversized box hammers the shared public services). Shrink "
                "the bbox (lat_lim / lon_lim or aoi=), or raise the cap with "
                "max_bbox_deg2=."
            )

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Run each planned query live and map the result to a FeatureCollection.

        Widens the inherited `-> list[Path]` contract: a vector backend
        returns in-memory `FeatureCollection`s. Routes on the resolved
        `Dataset.protocol` (`G2`, `G10`) — `overpass` via `_fetch_overpass`,
        `ohsome` via `_fetch_ohsome`, `pbf` via `_fetch_pbf`. An HTTP / SDK
        error propagates rather than being silently swallowed.

        Args:
            products: The products returned by `_search`.

        Returns:
            list[FeatureCollection]: One collection per product, in product
                order.
        """
        # Lazy so a `limit=` stops the work: each query is a live Overpass /
        # ohsome request or a PBF extract, so a query past the cap is never
        # issued rather than issued and then trimmed away.
        return self._take_limited(
            (self._fetch_product(product) for product in products),
            limit=self._limit,
        )

    def _fetch_product(self, product: RemoteProduct) -> FeatureCollection:
        """Run one planned query, routing on its dataset's protocol.

        Args:
            product: One product from `_search`; its `dataset` metadata
                decides the transport.

        Returns:
            FeatureCollection: The query's features (empty when nothing
                matched).
        """
        dataset: Dataset = product.metadata["dataset"]
        if dataset.protocol == "overpass":
            collection = self._fetch_overpass(product.id, dataset)
        elif dataset.protocol == "ohsome":
            collection = self._fetch_ohsome(product.id, dataset)
        else:
            collection = self._fetch_pbf(product.id, dataset)
        logger.info(f"{product.id}: fetched {len(collection)} feature(s)")
        return collection

    def _overpass_client(self) -> HttpClient:
        """Return this instance's Overpass client, built once.

        One client per backend, not one per query: `min_interval` paces
        requests through the `_last_request` timestamp the client carries, so a
        fresh client per query starts with no history and never sleeps —
        `MIN_REQUEST_INTERVAL` would be declared, passed, and completely
        inert against the public endpoint it exists to protect.

        Returns:
            HttpClient: The memoised Overpass client.
        """
        if self._overpass_http is None:
            self._overpass_http = HttpClient(
                session=cast("requests.Session | None", _RequestsHttp()),
                user_agent=self._user_agent,
                min_interval=self.MIN_REQUEST_INTERVAL,
                timeout=self._timeout,
                max_retries=0,
                status_forcelist=(),
                raise_for_status=True,
            )
        return self._overpass_http

    def _pbf_client(self) -> HttpClient:
        """Return this instance's PBF-download client, built once.

        Memoised for the same reason as :meth:`_overpass_client`: the pacing
        state lives on the client.

        Returns:
            HttpClient: The memoised PBF client.
        """
        if self._pbf_http is None:
            self._pbf_http = HttpClient(
                user_agent=self._user_agent,
                timeout=self._timeout,
                min_interval=self.MIN_REQUEST_INTERVAL,
                retry_on_exceptions=(
                    requests.ConnectionError,
                    requests.Timeout,
                    OSError,
                ),
            )
        return self._pbf_http

    def _fetch_overpass(self, query_id: str, dataset: Dataset) -> FeatureCollection:
        """Fetch one Overpass query: POST the QL (with UA), parse, build geometry.

        POSTs the resolved Overpass QL to `self._endpoint` through the shared
        :class:`~earthlens.base.http.HttpClient` (a descriptive
        `User-Agent` — required to avoid the overpass-api.de 406 — applied at
        the client level via `default_headers`, and `self._timeout`), parses
        the JSON with `overpy.Overpass().parse_json(...)`, and converts the
        parsed elements to a `FeatureCollection` via `overpy_to_gdf` / `to_fc`.
        The client is single-shot (`max_retries=0`, empty
        `status_forcelist`), so behaviour matches the previous bare
        `requests.post`: no retry, non-2xx statuses propagate immediately.

        Args:
            query_id: The named-query id (for logging).
            dataset: The resolved `Dataset` row (its `query_template`, unless
                a raw `query=` override was given).

        Returns:
            FeatureCollection: The matched features, CRS `EPSG:4326`.

        Raises:
            ImportError: If `overpy` is not installed (`earthlens[osm]`).
            requests.HTTPError: If the Overpass endpoint returns a non-2xx
                status.
        """
        try:
            import overpy
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise ImportError(
                "The OSM Overpass protocol requires the `overpy` SDK. Install "
                "it with `pip install earthlens[osm]`."
            ) from exc

        template = cast("str", self._query or dataset.query_template)
        south, west, north, east = bbox_swne(self.space)
        bbox_str = f"{south},{west},{north},{east}"
        # Substitute only the known placeholders — never `str.format` the whole
        # QL, which would choke on Overpass regex brace-quantifiers (e.g.
        # `~"^A.{2,5}$"`) in a raw `query=` override (`{2,5}` -> KeyError).
        ql = template.replace("{bbox}", bbox_str).replace(
            "{timeout}", str(int(self._timeout))
        )
        logger.info(f"Querying Overpass for {query_id!r} over bbox ({bbox_str})")
        response = self._overpass_client().post(self._endpoint, data={"data": ql})
        result = overpy.Overpass().parse_json(response.text)
        return to_fc(overpy_to_gdf(result))

    def _fetch_ohsome(self, query_id: str, dataset: Dataset) -> FeatureCollection:
        """Fetch one ohsome `elements/geometry` query into a FeatureCollection.

        POSTs to the ohsome `elements/geometry` endpoint with the bbox in
        ohsome order `W,S,E,N` and the request time window, then wraps the
        `.as_dataframe()` `GeoDataFrame` directly (no `xarray`, `G7`). The
        result's `(@osmId, @snapshotTimestamp)` MultiIndex is reset into columns
        so the history fields survive into the `FeatureCollection`.

        The request is issued via `OhsomeClient(...).post(endpoint=
        "elements/geometry")` rather than the chained `.elements.geometry.post`:
        the chained form spawns a fresh sub-client that silently drops the
        `retry` and `user_agent` set on the root client, so only the direct
        `post(endpoint=...)` actually applies our transport policy. That policy
        gives the SDK's session a urllib3 `Retry` (`MAX_OHSOME_RETRIES` retries,
        exponential `OHSOME_BACKOFF_FACTOR` growth, `Retry-After` honoured) so a
        `429`/`5xx` throttle is retried with backoff — matching the repo-wide
        `HttpClient`. Any remaining failure is turned into a clear, typed error
        (via `_reraise_ohsome_error`, which logs the recovered status /
        `Content-Type` / body preview) instead of the SDK's opaque failure: a
        `403` / `429` throttle becomes an `OhsomeUnavailableError`, and any other
        non-JSON body (a rate-limit / maintenance / error page or a redirect)
        becomes an `OhsomeResponseError` — so a decoder error never stands in for
        "the server said no" (`#930`).

        Args:
            query_id: The named-query id (for logging).
            dataset: The resolved `Dataset` row (its `ohsome_filter`, unless
                a raw `filter=` override was given).

        Returns:
            FeatureCollection: The matched features, CRS `EPSG:4326`.

        Raises:
            ImportError: If `ohsome` is not installed (`earthlens[osm]`).
            OhsomeUnavailableError: If `api.ohsome.org` blocks/throttles the
                request with a `403` / `429`, or is unavailable with a `5xx`
                outlasting the retries — a public-endpoint denial/outage, not a
                credential error.
            OhsomeResponseError: If `api.ohsome.org` returns any other non-JSON
                body (a rate-limit / maintenance / error page or a redirect).
            ValueError: If no `start` (and `time`) was supplied — ohsome
                requires a time.
        """
        try:
            from ohsome import OhsomeClient
            from urllib3.util.retry import Retry
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise ImportError(
                "The OSM ohsome protocol requires the `ohsome` SDK (and "
                "`urllib3`). Install them with `pip install earthlens[osm]`."
            ) from exc

        ohsome_filter = self._filter or dataset.ohsome_filter
        west, south, east, north = bbox_wsen(self.space)
        bboxes = f"{west},{south},{east},{north}"
        time = self._ohsome_time()
        logger.info(
            f"Querying ohsome for {query_id!r} over bbox ({bboxes}) at time {time!r}"
        )
        retry = Retry(
            total=self.MAX_OHSOME_RETRIES,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            backoff_factor=self.OHSOME_BACKOFF_FACTOR,
            respect_retry_after_header=True,
            # Ceiling on any single wait — the exponential backoff *and* a
            # server-sent `Retry-After` — so a hostile/misconfigured `Retry-After`
            # cannot pin the calling thread. Mirrors HttpClient's DEFAULT_MAX_BACKOFF.
            backoff_max=self.OHSOME_MAX_BACKOFF,
            retry_after_max=self.OHSOME_MAX_BACKOFF,
        )
        # `log=False` keeps the SDK from writing an `ohsome_log/` directory into
        # the caller's CWD on every failed query.
        client = OhsomeClient(user_agent=self._user_agent, retry=retry, log=False)
        try:
            response = client.post(
                bboxes=bboxes,
                time=time,
                filter=ohsome_filter,
                endpoint="elements/geometry",
            )
            gdf = response.as_dataframe()
        # Broad by design: convert a throttle/block or a non-JSON response into a
        # clear typed error (logging the recovered evidence), else re-raise the
        # original failure unchanged.
        except Exception as exc:  # noqa: BLE001
            self._reraise_ohsome_error(exc)
            raise
        # `as_dataframe()` carries a (@osmId, @snapshotTimestamp) MultiIndex;
        # reset it so the history fields become ordinary columns on the FC.
        if gdf.index.names and any(name is not None for name in gdf.index.names):
            gdf = gdf.reset_index()
        return to_fc(gdf)

    def _reraise_ohsome_error(self, exc: Exception) -> None:
        """Convert an ohsome SDK failure into a clear, typed, logged error.

        The raw SDK failure discards what the server actually said — a decoder
        error is the wrong abstraction for "the response was not JSON" (`#930`).
        This recovers the HTTP status, `Content-Type`, and first bytes of the
        body from the exception chain (whether the SDK wrapped the failure into
        an `OhsomeException` or leaked a bare `JSONDecodeError`), logs them at the
        point of failure, and then raises the right typed error:

        * a `403`, or a `429` that outlived the retries, becomes an
          `OhsomeUnavailableError` (a public-endpoint throttle/block);
        * a `5xx` that outlived the retries becomes an `OhsomeUnavailableError`
          too — a transient server-side outage; the SDK can otherwise die with a
          raw `KeyError: 'message'` on a `5xx` body (`#790`);
        * any other non-JSON body — a rate-limit / maintenance / error page, an
          empty body, or a redirect to a landing page — becomes an
          `OhsomeResponseError` carrying the recovered evidence.

        Anything else (a transport error, or a non-`5xx` ohsome error served
        *as* JSON — including a `401`, which on this keyless endpoint signals a
        real auth-contract change, not a throttle) is left for the caller to
        re-raise unchanged, so a genuine regression still surfaces loudly.

        Args:
            exc: The exception raised by the ohsome SDK call.

        Raises:
            OhsomeUnavailableError: On a public-endpoint throttle/block (`403` /
                `429`) or a transient server-side outage (`5xx`).
            OhsomeResponseError: On any other non-JSON response body.
        """
        status = ohsome_http_status(exc)
        non_json = ohsome_response_is_non_json(exc)
        is_throttle = status in (403, 429)
        is_server_error = status is not None and 500 <= status < 600
        if not is_throttle and not is_server_error and not non_json:
            # A transport error (no status), or a genuine ohsome error already
            # served as readable JSON (including a JSON `401`) — nothing to add;
            # let the caller re-raise the original, as quietly as before.
            return

        response = ohsome_error_response(exc)
        headers = getattr(response, "headers", None)
        content_type = headers.get("Content-Type") if headers is not None else None
        body_preview = ohsome_body_preview(response)
        status_shown = status if status is not None else "unknown"
        body_note = (
            f"first {len(body_preview)} body chars: {body_preview!r}"
            if body_preview is not None
            else "no body captured"
        )
        # Log the evidence the raw decoder error discards, at the point of failure
        # — visible even when the caller skips the throttle case (`#930`). Only the
        # converted (throttle / non-JSON) branches reach here, so a JSON-served
        # error propagates without a stray warning.
        logger.warning(
            "ohsome elements/geometry request failed: "
            f"HTTP {status_shown}, Content-Type {content_type!r}, {body_note}"
        )

        if status == 403:
            raise OhsomeUnavailableError(
                "ohsome refused the elements/geometry request with HTTP 403. "
                "api.ohsome.org is a public, keyless endpoint, so this is its "
                "front proxy blocking or throttling this client (an IP / "
                "rate-limit block), not a credential problem. Wait and retry "
                "later, shrink the bbox or time window, or try from a different "
                "network.",
                status_code=status,
                content_type=content_type,
                body_preview=body_preview,
            ) from exc
        if status == 429:
            raise OhsomeUnavailableError(
                "ohsome refused the elements/geometry request with HTTP 429 (Too "
                "Many Requests) after automatic retries. api.ohsome.org is "
                "rate-limiting this client; wait before retrying, or reduce the "
                "request frequency and size.",
                status_code=status,
                content_type=content_type,
                body_preview=body_preview,
            ) from exc
        if is_server_error:
            # A 5xx that outlived the retries — a server-side outage. The SDK can
            # die with a raw `KeyError: 'message'` here (its error handler assumes
            # a `"message"` field the 5xx body lacks, `#790`), so surface a clear
            # service-unavailable error carrying the status instead.
            raise OhsomeUnavailableError(
                "ohsome could not serve the elements/geometry request: HTTP "
                f"{status_shown} after automatic retries. api.ohsome.org is a "
                "public endpoint that load-sheds its compute-heavy extraction "
                "path under load, so a 5xx is usually a transient server-side "
                "condition (load-shedding or a gateway error), not typically a "
                "problem with the request. Retry later, or check the ohsome "
                "service status.",
                status_code=status,
                content_type=content_type,
                body_preview=body_preview,
            ) from exc
        # Not a throttle or a server error, so — given the guard above — the body
        # was not JSON.
        raise OhsomeResponseError(
            "ohsome returned a non-JSON response for the elements/geometry "
            f"request (HTTP {status_shown}, Content-Type {content_type!r}); "
            f"{body_note}. api.ohsome.org served an unparseable body — typically "
            "a rate-limit, maintenance, or error page, or a redirect to a landing "
            "page — rather than the expected GeoJSON. Retry later, or check the "
            "ohsome service status.",
            status_code=status,
            content_type=content_type,
            body_preview=body_preview,
        ) from exc

    def _fetch_pbf(self, query_id: str, dataset: Dataset) -> FeatureCollection:
        """Fetch one `pbf` layer: resolve region, fetch-cache, read, bbox-clip.

        Resolves `self._region` to a Geofabrik path, downloads the extract
        (cached, via :func:`~earthlens.osm._pbf.download_extract`), and reads
        the row's `pyrosm_method` layer into a `FeatureCollection` with the
        selected engine (`self._engine`), clipping to the request bbox (`G12`,
        `G13`, `G14`). The read stays `xarray`-free (`G7`).

        Args:
            query_id: The named-query id (for logging).
            dataset: The resolved `pbf` `Dataset` row (its `pyrosm_method` and,
                for the road network, `network_type`).

        Returns:
            FeatureCollection: The layer's features, CRS `EPSG:4326`.

        Raises:
            ImportError: If the selected engine's SDK is not installed
                (`earthlens[osm-pbf]`).
            ValueError: If `self._region` is not a known region / raw path, or
                a `pyrosm` read is attempted on an oversized extract.
        """
        region_path = self._catalog.region_path(cast("str", self._region))
        logger.info(
            f"Reading {query_id!r} from Geofabrik region {region_path!r} via the "
            f"{self._engine} engine"
        )
        # Retry a dropped socket / timeout / disk hiccup mid-stream — a multi-GB
        # extract is exactly where a transient transport failure bites. Kept
        # narrow (not the broad `requests.RequestException` the siblings use) so
        # a definitive 4xx (e.g. a wrong region path -> 404) still fails fast
        # rather than retrying five times; retryable 429/5xx statuses are already
        # handled by the client's default `status_forcelist`.
        path = download_extract(region_path, self._cache_dir, http=self._pbf_client())
        return read_pbf(
            path,
            pyrosm_method=cast("str", dataset.pyrosm_method),
            network_type=dataset.network_type,
            bbox=self._pbf_bbox(),
            engine=self._engine,
        )

    def _pbf_bbox(self) -> tuple[float, float, float, float] | None:
        """Return the request bbox as `(west, south, east, north)`, or `None`.

        A `pbf` read clips to the request bbox, but the whole-Earth default a
        facade caller gets when they omit `lat_lim` / `lon_lim` means "no clip"
        (read the whole extract), so it maps to `None` rather than a redundant
        planet-sized clip.

        Returns:
            tuple[float, float, float, float] | None: `(west, south, east,
                north)`, or `None` when the bbox spans (at least) the whole
                globe.
        """
        west, south, east, north = bbox_wsen(self.space)
        if west <= -180 and south <= -90 and east >= 180 and north >= 90:
            return None
        return (west, south, east, north)

    def _ohsome_time(self) -> str:
        """Build the ohsome `time` argument from the request window.

        Returns:
            str: A single ISO date (`start` only) or a `"start/end"` range.

        Raises:
            ValueError: If no `start` was supplied — ohsome requires a time.
                (An inverted `end < start` window is already rejected upstream
                by the `TemporalExtent` validator, at construction.)
        """
        start = self.time.start_date
        end = self.time.end_date
        if start is None:
            raise ValueError(
                "an ohsome query needs a time: pass start= (and optionally "
                "end=), or time=, e.g. start='2020-01-01'."
            )
        if end is None or end == start:
            return cast("str", start.strftime("%Y-%m-%d"))
        return f"{start:%Y-%m-%d}/{end:%Y-%m-%d}"

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> FeatureCollection:
        """Run the requested OSM queries and return the combined features.

        Routes each named query to its protocol, combines the results into
        one `FeatureCollection`, emits an ODbL `LicenseWarning` (`G5`),
        writes the collection to one vector file under `path` when non-empty,
        and returns the in-memory collection.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends; OSM issues one query per named id, so this is a
                no-op.
            limit: Cap on the total features returned, across every named
                query. Applied as each query's result arrives, so a query past
                the cap is never issued — which also means the rate limit is
                not spent on it. `None` (the default) runs every query.

        Returns:
            FeatureCollection: The matched features, CRS `EPSG:4326`. Empty
                (schema-only) when nothing matched.
        """
        self._limit = self.check_limit(limit)
        collection = self._combine(self._api())
        # OSM is ODbL — warn on every result, even an empty one (the query
        # itself succeeded and the obligation rides with any data downloaded).
        warnings.warn(ODBL_NOTICE, LicenseWarning, stacklevel=2)

        if len(collection):
            out_path = self._write(collection)
            logger.info(
                f"OSM download summary: {len(collection)} feature(s) written to "
                f"{out_path}"
            )
        else:
            logger.warning(
                "OSM download summary: no features matched the request, nothing written"
            )
        return collection

    def _combine(self, collections: list[FeatureCollection]) -> FeatureCollection:
        """Concatenate per-query FeatureCollections into one (union of columns).

        Args:
            collections: The per-query collections from `_fetch` (possibly
                empty).

        Returns:
            FeatureCollection: A single collection — the lone input when
                there is exactly one, the row-wise concatenation when there
                are several, or a schema-only empty collection when there are
                none.
        """
        non_empty = [fc for fc in collections if len(fc)]
        if not non_empty:
            return empty_fc()
        if len(non_empty) == 1:
            return non_empty[0]
        import geopandas as gpd
        from pyramids.feature.collection import FeatureCollection

        merged = pd.concat(list(non_empty), ignore_index=True)
        return FeatureCollection(
            gpd.GeoDataFrame(merged, geometry="geometry", crs="EPSG:4326")
        )

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the features to one vector file under `root_dir`.

        The filename embeds the requested query ids (`osm_<ids>.<ext>`), so
        requests for different named queries land in distinct files. The bbox
        and time window are *not* part of the name, so two requests for the same
        query ids over different areas/times overwrite the same file.

        Args:
            collection: The features to write.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _DRIVERS[self._file_format]
        slug = "_".join(qid.replace(":", "-") for qid in self.vars)
        out_path = self.root_dir / f"osm_{slug}.{ext}"
        collection.to_file(str(out_path), driver=driver)
        return out_path


def __getattr__(name: str) -> Path:
    """Keep the removed `DEFAULT_PBF_CACHE_DIR` constant importable.

    It became `default_pbf_cache_dir()` so the location follows a later
    `set_cache_dir()` instead of freezing at import. Returning the resolved
    directory keeps an existing `from earthlens.osm.backend import
    DEFAULT_PBF_CACHE_DIR` working rather than failing with a bare ImportError.

    Args:
        name: The attribute being looked up.

    Returns:
        Path: The current default `.osm.pbf` cache directory.

    Raises:
        AttributeError: For any other name.
    """
    if name == "DEFAULT_PBF_CACHE_DIR":
        warnings.warn(
            "DEFAULT_PBF_CACHE_DIR is deprecated; call default_pbf_cache_dir() "
            "instead, which follows set_cache_dir() / EARTHLENS_CACHE.",
            DeprecationWarning,
            stacklevel=2,
        )
        return default_pbf_cache_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
