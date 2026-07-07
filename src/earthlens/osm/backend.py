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
  `elements/geometry` endpoint. `OhsomeClient().elements.geometry.post(...)
  .as_dataframe()` already returns a geopandas `GeoDataFrame`, wrapped straight
  into a `FeatureCollection` (no `xarray`, `G7`).
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

import datetime as dt
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.base.http import HttpClient
from earthlens.osm._helpers import (
    LicenseWarning,
    bbox_swne,
    bbox_wsen,
    empty_fc,
    overpy_to_gdf,
    to_fc,
)
from earthlens.osm._pbf import Engine, download_extract, read_pbf
from earthlens.osm.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

    from earthlens.aggregate import AggregationConfig

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

#: Default on-disk cache directory for fetched Geofabrik `.osm.pbf` extracts
#: (`G13`). A cross-run user cache (mirroring the cmip6 resolver's location) so
#: a re-run reuses a previously-downloaded extract regardless of the output
#: `path`. Overridable via the backend's `cache_dir=` argument.
DEFAULT_PBF_CACHE_DIR = Path.home() / ".earthlens" / "cache" / "osm_pbf"

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
        return requests.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Issue a POST via the module-level `requests.post`."""
        return requests.post(url, **kwargs)


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

    def __init__(
        self,
        variables: list[str] | str,
        lat_lim: list[float],
        lon_lim: list[float],
        start: str | None = None,
        end: str | None = None,
        temporal_resolution: str = "all",
        path: Path | str = "",
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
                `None` uses `DEFAULT_PBF_CACHE_DIR` (a cross-run user cache).

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
                f"file_format must be one of {sorted(_DRIVERS)}, got "
                f"{file_format!r}."
            )
        if engine not in ("pyrosm", "pyosmium"):
            raise ValueError(
                f"engine must be 'pyrosm' or 'pyosmium', got {engine!r}."
            )
        self._query = query
        self._filter = filter
        self._endpoint = endpoint
        self._user_agent = user_agent
        self._timeout = timeout
        self._file_format: FileFormat = file_format
        self._max_bbox_deg2 = max_bbox_deg2
        self._region = region
        self._engine: Engine = engine
        self._cache_dir = Path(cache_dir) if cache_dir else DEFAULT_PBF_CACHE_DIR
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
        # OSM is queried in one shot — pin the sentinel even when the facade
        # forwards its default cadence, so the attribute never misrepresents a
        # temporal cadence the backend does not have.
        self.temporal_resolution = "all"

    def _initialize(self) -> None:
        """No auth, no client — both protocols are public (`G6`).

        Returns:
            None: No per-instance client object. The `overpy` / `ohsome`
                SDKs are imported lazily in `_fetch`.
        """
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a `SpatialExtent` (no snapping).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox feeding the bbox helpers.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

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
            fmt: `strptime` format applied to `start` / `end` when set.

        Returns:
            TemporalExtent: Frozen model; `start_date` / `end_date` are
                `None` when the corresponding argument was `None`.
        """
        start_dt = dt.datetime.strptime(start, fmt) if start else None
        end_dt = dt.datetime.strptime(end, fmt) if end else None
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
            raise ValueError(
                "a pbf:* query needs a Geofabrik region: pass region= (a key "
                f"from the catalog, e.g. {self._catalog.region_ids()[:3]}, or a "
                "raw 'continent/region' path)."
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
        collections: list[FeatureCollection] = []
        for product in products:
            dataset: Dataset = product.metadata["dataset"]
            if dataset.protocol == "overpass":
                collection = self._fetch_overpass(product.id, dataset)
            elif dataset.protocol == "ohsome":
                collection = self._fetch_ohsome(product.id, dataset)
            else:
                collection = self._fetch_pbf(product.id, dataset)
            logger.info(f"{product.id}: fetched {len(collection)} feature(s)")
            collections.append(collection)
        return collections

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

        template = self._query or dataset.query_template
        south, west, north, east = bbox_swne(self.space)
        bbox_str = f"{south},{west},{north},{east}"
        # Substitute only the known placeholders — never `str.format` the whole
        # QL, which would choke on Overpass regex brace-quantifiers (e.g.
        # `~"^A.{2,5}$"`) in a raw `query=` override (`{2,5}` -> KeyError).
        ql = template.replace("{bbox}", bbox_str).replace(
            "{timeout}", str(int(self._timeout))
        )
        logger.info(f"Querying Overpass for {query_id!r} over bbox ({bbox_str})")
        http = HttpClient(
            session=_RequestsHttp(),
            user_agent=self._user_agent,
            timeout=self._timeout,
            max_retries=0,
            status_forcelist=(),
            raise_for_status=True,
        )
        response = http.post(self._endpoint, data={"data": ql})
        result = overpy.Overpass().parse_json(response.text)
        return to_fc(overpy_to_gdf(result))

    def _fetch_ohsome(self, query_id: str, dataset: Dataset) -> FeatureCollection:
        """Fetch one ohsome `elements/geometry` query into a FeatureCollection.

        Calls `OhsomeClient().elements.geometry.post(bboxes=..., time=...,
        filter=...)` with the bbox in ohsome order `W,S,E,N` and the request
        time window, then wraps the `.as_dataframe()` `GeoDataFrame` directly
        (no `xarray`, `G7`). The result's `(@osmId, @snapshotTimestamp)`
        MultiIndex is reset into columns so the history fields survive into
        the `FeatureCollection`.

        Args:
            query_id: The named-query id (for logging).
            dataset: The resolved `Dataset` row (its `ohsome_filter`, unless
                a raw `filter=` override was given).

        Returns:
            FeatureCollection: The matched features, CRS `EPSG:4326`.

        Raises:
            ImportError: If `ohsome` is not installed (`earthlens[osm]`).
            ValueError: If no `start` (and `time`) was supplied — ohsome
                requires a time.
        """
        try:
            from ohsome import OhsomeClient
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise ImportError(
                "The OSM ohsome protocol requires the `ohsome` SDK. Install it "
                "with `pip install earthlens[osm]`."
            ) from exc

        ohsome_filter = self._filter or dataset.ohsome_filter
        west, south, east, north = bbox_wsen(self.space)
        bboxes = f"{west},{south},{east},{north}"
        time = self._ohsome_time()
        logger.info(
            f"Querying ohsome for {query_id!r} over bbox ({bboxes}) at time {time!r}"
        )
        response = OhsomeClient().elements.geometry.post(
            bboxes=bboxes, time=time, filter=ohsome_filter
        )
        gdf = response.as_dataframe()
        # `as_dataframe()` carries a (@osmId, @snapshotTimestamp) MultiIndex;
        # reset it so the history fields become ordinary columns on the FC.
        if gdf.index.names and any(name is not None for name in gdf.index.names):
            gdf = gdf.reset_index()
        return to_fc(gdf)

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
        region_path = self._catalog.region_path(self._region)
        logger.info(
            f"Reading {query_id!r} from Geofabrik region {region_path!r} via the "
            f"{self._engine} engine"
        )
        http = HttpClient(user_agent=self._user_agent, timeout=self._timeout)
        path = download_extract(region_path, self._cache_dir, http=http)
        return read_pbf(
            path,
            pyrosm_method=dataset.pyrosm_method,
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
            return start.strftime("%Y-%m-%d")
        return f"{start:%Y-%m-%d}/{end:%Y-%m-%d}"

    def _api(self) -> list[FeatureCollection]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
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
            aggregate: Must be `None`. OSM features are vector, not gridded,
                so there is no meaningful aggregation. The facade already
                rejects a non-`None` `aggregate=` for a `vector` backend;
                this is the belt-and-suspenders guard for direct callers.

        Returns:
            FeatureCollection: The matched features, CRS `EPSG:4326`. Empty
                (schema-only) when nothing matched.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "OSM.download(aggregate=...) is not supported: OSM features are "
                "vector, not gridded rasters, so there is no meaningful gridded "
                "reduction. Call download() without aggregate= and post-process "
                "the returned FeatureCollection (a GeoDataFrame) directly."
            )

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
                "OSM download summary: no features matched the request, nothing "
                "written"
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
