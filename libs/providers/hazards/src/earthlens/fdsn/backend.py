"""Backend that queries FDSN-event seismological networks via obspy.

`FDSN(AbstractDataSource)` fetches earthquake / seismic-event catalogs
from the six IRIS-FDSN-event networks earthlens ships with — USGS
(ComCat), EMSC (seismicportal), INGV (Italian seismic + volcano),
EarthScope (ex-IRIS DMC), ISC (global reviewed bulletin), and GeoNet
(New Zealand) — through one `obspy.clients.fdsn.Client` per network,
because they all speak the identical FDSN-event web-service
standard. Each provider key in `variables` becomes one server-side
`get_events` call; the per-network `obspy.core.event.Catalog` is mapped
to a pyramids :class:`~pyramids.feature.collection.FeatureCollection`
by :mod:`earthlens.fdsn.events`.

This is the first `vector` backend: the on-the-wire result is a table
of point features, not a gridded array, so `OUTPUT_KIND = "vector"`
and an `aggregate=` argument is refused (there is no meaningful
gridded reduction of an event table). The refusal itself lives in
:meth:`earthlens.base.AbstractDataSource._refuse_unsupported_aggregate`,
which every backend's `download` routes through, so a direct
`FDSN(...).download(aggregate=...)` is rejected identically to one
made through the :class:`earthlens.earthlens.EarthLens` facade.
`download()` returns the in-memory FeatureCollection (the union across
requested networks) and, as a side effect, writes one vector file per
network to `path`.

Optionally the backend also writes a **raster** side-output: with
`with_shakemap=True` each USGS event's gridded ShakeMap is fetched and
written as a GeoTIFF alongside the vector files. This does not change
`OUTPUT_KIND` — `download()` still returns the event FeatureCollection
and the rasters are a side effect on disk. ShakeMap is a USGS ComCat
product and is not exposed through the FDSN event standard at all, so
that path is USGS-only and costs one extra request per event — see
:mod:`earthlens.fdsn._helpers`.

Provider selection follows the FDSN-specific reading of `variables`
(see the package docstring): `variables` is a `list[str]` of network
keys (`["USGS"]`, `["USGS", "EMSC"]`); query filters arrive as
explicit constructor kwargs. The temporal window is a single
unchunked `[start, end]` `get_events` call — FDSN does not iterate per
day/month — so `temporal_resolution` carries the sentinel `"all"`.
"""

from __future__ import annotations

import os
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.base.http import HttpClient
from earthlens.fdsn import _helpers, events
from earthlens.fdsn.auth import resolve_earthscope_token
from earthlens.fdsn.catalog import Catalog, Provider

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection


FileFormat = Literal["gpkg", "geojson"]

#: Map output format to the OGR driver and file extension `to_file` uses.
_DRIVERS: dict[str, tuple[str, str]] = {
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}

#: Default network when `variables` is empty.
_DEFAULT_PROVIDERS = ["USGS"]

#: Subdirectory of `root_dir` holding the ShakeMap side-output, one
#: folder per event so a multi-event query stays navigable.
_SHAKEMAP_DIR = "shakemap"

#: Prefix of the scratch subdirectory inside an event's folder. The archive is
#: fetched and unpacked there and the whole directory is removed afterwards, so
#: no intermediate — including the `.prj` GDAL writes when the grid's CRS is
#: assigned — can survive in the user-facing output. The process id is appended
#: so two runs sharing an output root cannot delete each other's scratch space
#: mid-fetch; a crashed run's directory is left for its owner to clean up
#: rather than removed by a stranger.
_STAGING_PREFIX = "_staging"

#: Default ceiling on how many events one call fetches a ShakeMap for.
#: Each event is a separate ComCat request plus a multi-megabyte archive, so an
#: unbounded window is gigabytes; 100 events is a gigabyte or two, which is
#: a defensible accident rather than a catastrophic one.
_DEFAULT_MAX_SHAKEMAP_EVENTS = 100

#: Seconds between consecutive ComCat detail requests. ShakeMap costs one
#: request per event, so a busy window fans out where the event query
#: itself was a single call; USGS publishes no rate limit, and this is a
#: politeness floor rather than a documented requirement.
_COMCAT_MIN_INTERVAL = 0.2

#: How long a *negative* manifest entry — "this event published no ShakeMap
#: raster" — is trusted before the event is checked again. ShakeMap is
#: generated minutes to hours after an event and can lag much longer for
#: moderate ones, so caching that answer permanently would make a query over a
#: recent window return nothing forever. A positive entry does not expire: the
#: rasters are on disk, and `download(force=True)` picks up a revision.
_NEGATIVE_CACHE_SECONDS = 7 * 24 * 60 * 60

#: `(connect, read)` budget for a ComCat request. The read half is generous
#: because the archive is several megabytes; the connect half fails a dead
#: host quickly instead of stalling a long batch on its first event.
_COMCAT_TIMEOUT = (10.0, 120.0)


class FDSN(AbstractDataSource):
    """FDSN seismic-event backend (vector point-feature output).

    Wraps `obspy.clients.fdsn.Client.get_events` so a user can pull a
    space/time/magnitude window of seismic events from one or more
    FDSN networks through the same `download()` shape every other
    earthlens backend uses. Each network key in `variables` becomes
    one server-side query; the per-network catalog is mapped to a
    :class:`~pyramids.feature.collection.FeatureCollection` and the
    networks' results are unioned into the single FeatureCollection
    `download()` returns.

    The public event services need no credentials; the optional
    EarthScope token is resolved lazily and only used for a provider
    whose catalog row declares `needs_token: true`.

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of point
            features (events), so `aggregate=` is refused with
            `NotImplementedError`. It stays `"vector"` even under
            `with_shakemap=True`: `OUTPUT_KIND` describes what
            `download()` *returns*, and that is always a
            FeatureCollection. The ShakeMap GeoTIFFs are an on-disk
            side effect, not a second return shape — `"mixed"` is
            reserved for a backend whose returned format is only known
            at download time and which honours `aggregate=` itself.

    Examples:
        - Build a plain event query and inspect what it resolved:
            ```python
            >>> from earthlens.fdsn import FDSN
            >>> backend = FDSN(
            ...     start="2024-01-01",
            ...     end="2024-01-31",
            ...     variables=["USGS"],
            ...     lat_lim=[30.0, 45.0],
            ...     lon_lim=[130.0, 145.0],
            ... )
            >>> backend.vars
            ['USGS']
            >>> backend.time.resolution
            'all'
            >>> backend.space.south, backend.space.east
            (30.0, 145.0)

            ```
        - An empty network list falls back to USGS, and the products the
          query will issue carry the resolved obspy client id:
            ```python
            >>> from earthlens.fdsn import FDSN
            >>> backend = FDSN(
            ...     start="2024-01-01",
            ...     end="2024-01-02",
            ...     variables=[],
            ...     lat_lim=[-90.0, 90.0],
            ...     lon_lim=[-180.0, 180.0],
            ... )
            >>> backend.vars
            ['USGS']
            >>> [product.metadata["fdsn_id"] for product in backend._search()]
            ['USGS']

            ```
        - Asking for the ShakeMap side-output selects one grid by default
          and leaves the returned shape a vector table:
            ```python
            >>> from earthlens.fdsn import FDSN
            >>> backend = FDSN(
            ...     start="2023-02-06",
            ...     end="2023-02-07",
            ...     variables=["USGS"],
            ...     lat_lim=[35.0, 39.0],
            ...     lon_lim=[35.0, 39.0],
            ...     with_shakemap=True,
            ... )
            >>> backend.OUTPUT_KIND
            'vector'
            >>> backend._shakemap_layers
            ('mmi_mean',)

            ```
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = "seismic events are vector point features, not gridded rasters, so there is no meaningful gridded reduction — and the optional with_shakemap= rasters are a per-event side-output with no time axis to reduce over. Call download() without aggregate= and post-process the returned FeatureCollection (a GeoDataFrame) directly"

    #: Partial-failure policy for the per-provider loop; `download(errors=...)`
    #: overrides it per call.
    _errors: str = "warn"

    #: Whether `download(force=...)` asked for cached ShakeMap rasters to be
    #: refetched rather than reused.
    _force: bool = False

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
        min_magnitude: float | None = None,
        max_magnitude: float | None = None,
        min_depth: float | None = None,
        max_depth: float | None = None,
        magnitude_type: str | None = None,
        event_type: str | None = None,
        orderby: str = "time",
        limit: int | None = None,
        earthscope_token: str | None = None,
        file_format: FileFormat = "gpkg",
        with_shakemap: bool = False,
        shakemap_layers: list[str] | None = None,
        max_shakemap_events: int = _DEFAULT_MAX_SHAKEMAP_EVENTS,
    ):
        """Initialise an FDSN backend instance.

        Args:
            start: Inclusive start of the event window, as a string
                parsed with `fmt`.
            end: Inclusive end of the event window.
            variables: List of FDSN network keys to query (`["USGS"]`,
                `["USGS", "EMSC"]`). For this backend `variables`
                names the seismic *networks*, not data variables (see
                the package docstring). An empty list defaults to
                `["USGS"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: FDSN does not chunk by day/month — the
                whole `[start, end]` window is one query — so this is
                the sentinel `"all"`, not a pandas frequency alias.
            path: Output directory for the per-network vector files.
                Created by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            min_magnitude: Lower magnitude bound. `None` (the default)
                falls back per network to that provider's
                `default_min_magnitude` catalog value (USGS / EMSC /
                EarthScope / ISC = 4.5, INGV = 2.0, GeoNet = 3.0), so
                each regional network keeps a sensible floor. Pass an
                explicit number to override every network with one
                bound.
            max_magnitude: Upper magnitude bound, or `None`.
            min_depth: Lower depth bound in kilometres, or `None`.
            max_depth: Upper depth bound in kilometres, or `None`.
            magnitude_type: Restrict to a magnitude type (e.g.
                `"Mw"`), or `None` for any.
            event_type: Restrict to an event type (e.g.
                `"earthquake"`, `"volcanic eruption"`), or `None`.
            orderby: Result ordering — `"time"`, `"time-asc"`,
                `"magnitude"`, or `"magnitude-asc"`.
            limit: Maximum number of events **per network**, or `None` for no
                cap. Unlike the total-row `limit=` the tabular backends take,
                this is pushed into the FDSN query itself, so a capped request
                never transfers the events past the cap; with several networks
                the totals add up rather than being one overall ceiling.
                Rejected if zero or negative.
            earthscope_token: Optional EarthScope access token; falls
                back to `EARTHSCOPE_TOKEN` / `~/.earthscope_token`.
                Used only for a provider that requires a token.
            file_format: Output vector format — `"gpkg"` (default,
                GeoPackage) or `"geojson"`.
            with_shakemap: Also fetch each event's gridded ShakeMap and
                write it as a GeoTIFF under `path/shakemap/<event>/`.
                **USGS only** — ShakeMap is a ComCat product, not part
                of the FDSN event standard, so a non-USGS network in the
                same request contributes events but no rasters. Costs
                one extra ComCat request plus a multi-megabyte archive per
                event, so bound the event count with `limit=` before
                turning it on for a busy window; beyond
                `max_shakemap_events` the side-output stops and says how
                many events it skipped. It does not change
                `OUTPUT_KIND` — `download()` still returns the event
                FeatureCollection, and the rasters are a side effect.
            max_shakemap_events: Ceiling on how many events one call
                will fetch a ShakeMap for, guarding against a broad
                query quietly pulling gigabytes (each event is a
                separate request plus a multi-megabyte archive). Events past
                the ceiling are deferred with a warning naming the
                count — never silently. The ceiling counts *fetches*,
                not events: an event already satisfied on disk costs no
                budget, and an id that can never be fetched at all is
                dropped before counting. So re-running the same request
                takes the next batch, and repeating it eventually walks
                the whole list. The one exception is an event that fails
                on every attempt: a failure is not cached, so it is
                retried each run and keeps its place in the queue.
                Within a run the events kept are the first
                `max_shakemap_events` in the order the networks returned
                them, which `orderby=` controls (`"magnitude"` puts the
                largest first, the usual intent when capping). Raise the
                ceiling deliberately for a large job, or narrow the
                query with `limit=` / `min_magnitude=`.
            shakemap_layers: Which ShakeMap grids to write when
                `with_shakemap` is on. `None` (the default) writes
                `["mmi_mean"]` — macroseismic intensity, the headline
                shaking field. The archive carries fourteen grids
                (`mmi`, `pga`, `pgv`, `psa0p3`, `psa0p6`, `psa1p0`,
                `psa3p0`, each `_mean` and `_std`); all are reachable,
                none but the default are written unless asked for.
                Ignored when `with_shakemap` is `False`.

        Raises:
            ValueError: If `file_format` is not a supported vector
                format, if `limit` is zero or negative, or if
                `shakemap_layers` names a grid the archive does not
                carry.
            TypeError: If `variables` is a mapping — for this backend it
                selects seismic networks, not data variables.
        """
        self._min_magnitude = min_magnitude
        self._max_magnitude = max_magnitude
        self._min_depth = min_depth
        self._max_depth = max_depth
        self._magnitude_type = magnitude_type
        self._event_type = event_type
        self._orderby = orderby
        # Not `self._limit`: the base class owns that name for the
        # client-side total cap, and a provider storing its own meaning there
        # is how usgs_water silently lost its server-side limit. Validated
        # here so a zero/negative cap is refused before it reaches the FDSN
        # query, where it would be a server-side argument whose meaning varies
        # by provider rather than an obvious client-side bug.
        self._request_limit = self.check_limit(limit)
        self._earthscope_token_arg = earthscope_token
        self._earthscope_token: str | None = None
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got {file_format!r}."
            )
        self._file_format: FileFormat = file_format
        self._with_shakemap = bool(with_shakemap)
        # Validated even when the flag is off, so a typo'd layer name is a
        # construction-time error rather than a surprise the day someone
        # turns the flag on.
        self._shakemap_layers = _helpers.normalize_layers(shakemap_layers)
        # Validated inline rather than through `check_limit`, which is typed
        # for an optional cap: this one is always an int, and keeping it that
        # way lets the ceiling comparison stay a plain `>`.
        if not isinstance(max_shakemap_events, int) or isinstance(
            max_shakemap_events, bool
        ):
            raise TypeError(
                "max_shakemap_events must be an integer, got "
                f"{type(max_shakemap_events).__name__}."
            )
        if max_shakemap_events <= 0:
            raise ValueError(
                "max_shakemap_events must be a positive integer, got "
                f"{max_shakemap_events!r}."
            )
        self._max_shakemap_events: int = max_shakemap_events
        self._http: HttpClient | None = None
        if isinstance(variables, dict):
            raise TypeError(
                "FDSN `variables` must be a list of network keys (e.g. "
                "['USGS', 'EMSC']), not a mapping. For this backend "
                "`variables` selects seismic networks, not data variables; "
                "query filters are explicit FDSN(...) keyword arguments."
            )
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_DEFAULT_PROVIDERS),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """Resolve the optional EarthScope token; build no global client.

        FDSN clients are per-network and built lazily in :meth:`_fetch`,
        so there is no shared client to bind to `self.client`. The only
        global state is the optional EarthScope token, resolved here so
        a missing-token situation surfaces once up front rather than per
        query.

        Returns:
            None: No per-instance client object.
        """
        self._earthscope_token = resolve_earthscope_token(self._earthscope_token_arg)
        return None

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        FDSN issues a single `get_events` call spanning the whole
        window, so there is no per-date loop. The resolution is kept as
        the FDSN sentinel `"all"` (not a real pandas frequency alias —
        it means "single unchunked window, no per-date iteration") and
        `dates` collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Ignored beyond being recorded as the
                resolution label; FDSN always queries the full window.
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
        """One :class:`RemoteProduct` per requested network.

        Resolves each network key in `self.vars` against the bundled
        provider catalog (raising with a did-you-mean hint on an
        unknown key) and records the resolved `fdsn_id` / `needs_token`
        on the product metadata. No network call is made here.

        Returns:
            list[RemoteProduct]: One product per network key, in
                request order; `id` is the network key and `metadata`
                carries `fdsn_id` and `needs_token`.

        Raises:
            ValueError: If a key in `self.vars` is not a registered
                provider.
        """
        products: list[RemoteProduct] = []
        # De-duplicated, order preserved: a repeated key would otherwise issue
        # the same query twice and union the same events into the result.
        for key in dict.fromkeys(self.vars):
            provider: Provider = self._catalog.get_provider(key)
            products.append(
                RemoteProduct(
                    id=key,
                    metadata={
                        "fdsn_id": provider.fdsn_id,
                        "needs_token": provider.needs_token,
                        "default_min_magnitude": provider.default_min_magnitude,
                    },
                )
            )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Query every network and map each result to a FeatureCollection.

        Widens the inherited `-> list[Path]` contract: a vector backend
        returns in-memory :class:`FeatureCollection`s, not file paths.
        A network whose query matches nothing
        (`FDSNNoDataException`, HTTP 204) yields an empty
        FeatureCollection — an empty result is a legitimate answer for a
        quiet region/time.

        A network whose query *errors* (timeout, HTTP 5xx, service
        unavailable) is logged and skipped rather than aborting the
        whole request — one flaky network does not lose the events
        already fetched from healthy ones (mirrors the ECMWF/CMEMS
        "one bad item does not kill the batch" policy). The skipped
        network contributes an empty FeatureCollection so the returned
        list stays positionally aligned with `products`. Only a
        **total** failure (every network errored) raises.

        The partial-failure policy is whatever `download(errors=...)`
        recorded on the instance: `"warn"` (the default) logs each failed
        network and continues, `"raise"` propagates the first failure, and
        `"ignore"` continues silently. An all-failed batch raises
        regardless of the policy.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[FeatureCollection]: One collection per product, in the
                same order; empty collections for no-data or failed
                networks.

        Raises:
            RuntimeError: When **every** network's query failed, so a
                caller cannot silently process nothing. The message
                aggregates the failed networks and their exception
                types; the per-network errors are logged at ERROR.
        """
        collections, failed = self._run_items(
            products,
            self._query_one,
            errors=self._errors,
            label="provider query",
            describe=lambda product: repr(product.id),
            on_failure=lambda _product, _exc: events.empty_fc(),
        )

        if failed and len(failed) == len(products):
            summary = ", ".join(
                f"{pid} ({type(exc).__name__}: {exc})" for pid, exc in failed
            )
            raise RuntimeError(
                f"all {len(failed)} FDSN provider query(ies) failed: "
                f"{summary}. See the per-provider ERROR logs above."
            )
        if failed:
            summary = ", ".join(f"{pid} ({type(exc).__name__})" for pid, exc in failed)
            logger.warning(
                f"{len(failed)} of {len(products)} FDSN provider query(ies) "
                f"failed and were skipped: {summary}"
            )
        return collections

    def _query_one(self, product: RemoteProduct) -> FeatureCollection:
        """Run one network's `get_events` and map it to a FeatureCollection.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`;
                `product.id` is the network key and
                `product.metadata["fdsn_id"]` selects the obspy client.

        Returns:
            FeatureCollection: Events for this network (empty on a
                no-data response).
        """
        from obspy import UTCDateTime
        from obspy.clients.fdsn import Client
        from obspy.clients.fdsn.header import FDSNNoDataException

        from earthlens.core import __version__

        provider_key = product.id
        fdsn_id = product.metadata["fdsn_id"]
        needs_token = product.metadata.get("needs_token", False)

        # An explicit min_magnitude overrides every network; otherwise fall
        # back to this provider's catalog default so regional networks (INGV,
        # GeoNet) keep their lower floor instead of the global 4.5.
        min_magnitude = (
            self._min_magnitude
            if self._min_magnitude is not None
            else product.metadata.get("default_min_magnitude")
        )

        client_kwargs: dict[str, object] = {"user_agent": f"earthlens/{__version__}"}
        if needs_token and self._earthscope_token:
            # obspy's only token slot is `eida_token`. The bundled networks are
            # all public (needs_token=False), so this branch is opt-in: a
            # maintainer who adds a token-gated network must confirm that
            # network accepts an EIDA-style token before relying on it.
            client_kwargs["eida_token"] = self._earthscope_token
        client = Client(fdsn_id, **client_kwargs)

        logger.info(
            f"Querying FDSN provider {provider_key!r} ({fdsn_id}) for events "
            f"{self.time.start_date}..{self.time.end_date} "
            f"min_magnitude={min_magnitude}"
        )
        try:
            catalog = client.get_events(
                starttime=UTCDateTime(self.time.start_date),
                endtime=UTCDateTime(self.time.end_date),
                minlatitude=self.space.south,
                maxlatitude=self.space.north,
                minlongitude=self.space.west,
                maxlongitude=self.space.east,
                minmagnitude=min_magnitude,
                maxmagnitude=self._max_magnitude,
                mindepth=self._min_depth,
                maxdepth=self._max_depth,
                magnitudetype=self._magnitude_type,
                eventtype=self._event_type,
                orderby=self._orderby,
                limit=self._request_limit,
            )
        except FDSNNoDataException:
            logger.info(
                f"FDSN provider {provider_key!r} returned no events for the "
                "requested window — empty result."
            )
            return events.empty_fc()
        return events.catalog_to_fc(catalog, provider_key)

    def download(
        self,
        progress_bar: bool = True,
        errors: str = "warn",
        limit: int | None = None,
        force: bool = False,
    ) -> FeatureCollection:
        """Query every requested network and return the unioned events.

        Each network in `self.vars` is queried once; its events are
        written to one vector file under `path` (named after the
        network), and the per-network results are concatenated into the
        single :class:`FeatureCollection` returned. A network that
        errors is logged and skipped (its events are simply absent from
        the union) rather than aborting the whole request; only a total
        failure — every network errored — raises. An all-empty result
        (every network matched nothing) returns a schema-correct empty
        FeatureCollection and writes nothing.

        When the instance was built with `with_shakemap=True`, each USGS
        event additionally gets its ShakeMap grids written as GeoTIFFs
        under `path/shakemap/<event>/`. Those rasters are a side effect
        only — the return value stays the event FeatureCollection. An
        event whose ShakeMap cannot be fetched is logged and skipped
        under the same `errors=` policy as a failed network, so one
        missing grid never costs the event table.

        Args:
            progress_bar: Whether to show a progress bar. obspy's
                `get_events` has none, so this only governs the ShakeMap
                loop, which is the long part of a `with_shakemap=True`
                call; a plain event query ignores it.
            errors: Partial-failure policy, applied to **both** the
                per-network query loop and the per-event ShakeMap loop.
                `"warn"` (the default) logs each failure and continues,
                `"ignore"` continues silently, and `"raise"` propagates
                the first failure. Note what `"raise"` costs on a
                `with_shakemap=True` call: the events have already been
                fetched and written by then, but raising out of
                `download()` means they are not *returned*, so a single
                missing ShakeMap loses the whole in-memory event table.
                Leave it at `"warn"` unless a missing raster should
                genuinely abort the request.
            force: Refetch a ShakeMap whose GeoTIFFs are already on
                disk instead of reusing them. A `download()` argument,
                not a constructor one — `EarthLens(...).download(force=True)`,
                not `EarthLens(force=True, ...)`. The rasters are written
                atomically, so a present file is a finished one and the
                default reuse is safe; this is the escape hatch for a
                file damaged after the fact, or for picking up a
                revised ShakeMap for an event USGS has since updated.
            limit: Maximum events **per network**, overriding the constructor's
                `limit=` for this call. Same meaning as that one — pushed into
                the FDSN query itself, so a per-network server-side cap rather
                than a total across networks — and accepted here so it can be
                passed through `EarthLens(...).download(limit=...)` like the
                other bounded backends. `None` (the default) keeps whatever the
                constructor set — note that a value passed here is **sticky**:
                it replaces the constructor's for this and every later call on
                the same instance, as `force=` and `errors=` do not.

        Returns:
            FeatureCollection: The row-wise union of every requested
                network's events, CRS `EPSG:4326`. Empty (schema-only)
                when no network matched anything.

        Raises:
            RuntimeError: If **every** requested network's query failed
                (propagated from :meth:`_fetch`). A partial failure of
                the *network* loop does not raise — the healthy
                networks' events are returned. The ShakeMap loop is
                governed by the same `errors=` policy, so under the
                default `"warn"` a failed raster does not raise either;
                under `errors="raise"` it does, and because the raise
                escapes `download()` the event table is not returned
                even though its files are already on disk.
        """
        if limit is not None:
            self._request_limit = self.check_limit(limit)
        self._errors = self.check_errors_policy(errors)
        self._force = force
        products = self._search()
        collections = self._fetch(products) if products else []

        written: list[Path] = []
        for product, collection in zip(products, collections):
            if len(collection):
                written.append(self._write(product.id, collection))

        rasters: list[Path] = []
        if self._with_shakemap:
            # Before the concat: the per-network split is what says which
            # events came from USGS, and the union has thrown that away.
            try:
                rasters = self._download_shakemaps(
                    products, collections, progress_bar=progress_bar
                )
            finally:
                self._close_client()

        combined = events.concat_fcs(collections)
        # `concat_fcs` has copied every network's events, so the per-network
        # copies are dead weight from here on. This does not lower the *peak* —
        # that was reached inside the concat, with both sets live — but it stops
        # them being held through the summary, the return, and however long the
        # caller keeps the result.
        collections.clear()
        if written:
            # "available" rather than "written": a re-run reuses rasters that
            # were already on disk, and calling those written would overstate
            # what this call did.
            raster_note = (
                f" plus {len(rasters)} ShakeMap raster(s) available"
                if self._with_shakemap
                else ""
            )
            logger.info(
                f"FDSN download summary: {len(combined)} events across "
                f"{len(written)} file(s){raster_note} written to {self.root_dir}"
            )
        else:
            logger.warning(
                "FDSN download summary: no events matched the request, nothing written"
            )
        return combined

    def _client(self) -> HttpClient:
        """Return this instance's pooled ComCat client, built on first use.

        Only the ShakeMap path needs HTTP — the event query itself goes
        through obspy — so the client is built lazily and a request
        without `with_shakemap=True` never opens a session at all.

        Returns:
            HttpClient: The same instance on every later call.
        """
        if self._http is None:
            # ComCat is a single origin fetched once per event, so a dropped
            # connection is a normal event rather than a signal to give up on
            # the whole batch; retrying transport errors matches flodis/hanze.
            # The default user-agent is already `earthlens/{version}`.
            self._http = HttpClient(
                timeout=_COMCAT_TIMEOUT,
                min_interval=_COMCAT_MIN_INTERVAL,
                # ChunkedEncodingError is the one a multi-megabyte body actually hits:
                # the connection survives the handshake and dies mid-stream, which
                # is neither a ConnectionError nor a Timeout.
                retry_on_exceptions=(
                    requests.ConnectionError,
                    requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                ),
            )
        return self._http

    def _close_client(self) -> None:
        """Release the pooled ComCat client, if one was ever built.

        `HttpClient` exposes no `close()` of its own — only `HttpRangeFile`
        does — so the underlying `requests.Session` is closed directly. A
        session left open holds its connection pool for the lifetime of the
        backend instance, which matters here because the ShakeMap client is
        built per call and used for a bounded burst.
        """
        if self._http is not None:
            # `session` is annotated `requests.Session`, but `HttpClient`
            # accepts any session-like object and `RequestsGet` — the
            # per-call adapter earthlens.testing swaps in as the default
            # transport — has no `close()`. Closing unconditionally therefore
            # fails against a perfectly valid transport, which is why this is
            # a probe rather than a direct call.
            closer = getattr(self._http.session, "close", None)
            if callable(closer):
                closer()
            self._http = None

    def _download_shakemaps(
        self,
        products: list[RemoteProduct],
        collections: list[FeatureCollection],
        progress_bar: bool = True,
    ) -> list[Path]:
        """Write the ShakeMap side-output for every USGS event fetched.

        ShakeMap is a USGS ComCat product with no counterpart in the FDSN
        event standard, so a non-USGS network that returned events is
        logged once and skipped rather than silently producing nothing.

        Args:
            products: The products from :meth:`_search`.
            collections: The per-product FeatureCollections from
                :meth:`_fetch`, still split by network.
            progress_bar: Whether each archive fetch shows a progress
                bar. This is the long part of a `with_shakemap=True`
                call, so it is the only place the flag does anything.

        Returns:
            list[Path]: Every GeoTIFF available for the requested
                events — those written by this call plus any reused
                from a previous run.
        """
        event_ids: list[str] = []
        for product, collection in zip(products, collections):
            if product.metadata.get("fdsn_id") != _helpers.COMCAT_PROVIDER:
                if len(collection):
                    logger.warning(
                        f"with_shakemap=True but {product.id!r} is not "
                        f"{_helpers.COMCAT_PROVIDER}: ShakeMap is a USGS ComCat "
                        "product, so this network contributes events but no "
                        "rasters."
                    )
                continue
            event_ids.extend(str(value) for value in collection["event_id"])
        # Two networks can report the same ComCat event, and a catalog can
        # repeat one; de-duplicated so the ceiling counts each event once.
        event_ids = list(dict.fromkeys(event_ids))

        if not event_ids:
            return []

        # Drop what can never succeed before anything is counted. An id that
        # yields no ComCat id, or one whose directory fails the containment
        # assertion, is not work waiting to be done — left in, it would sit at
        # the head of the queue spending budget on every run and starve the
        # events behind it.
        event_ids = [event_id for event_id in event_ids if self._is_fetchable(event_id)]
        if not event_ids:
            return []

        # The ceiling bounds *work*, not events. An event already satisfied on
        # disk costs nothing, so spending budget on it would stall a re-run at
        # the same place forever instead of letting it advance through the list.
        pending = [event_id for event_id in event_ids if not self._is_cached(event_id)]
        if len(pending) > self._max_shakemap_events:
            deferred = set(pending[self._max_shakemap_events :])
            logger.warning(
                f"with_shakemap=True needs {len(pending)} fetches, over the "
                f"max_shakemap_events={self._max_shakemap_events} ceiling: taking "
                f"the first {self._max_shakemap_events} and deferring "
                f"{len(deferred)}. Each fetch costs a request plus a "
                "multi-megabyte archive. Re-run to take the next batch, raise "
                "max_shakemap_events= to take them all at once, or narrow the "
                "query with limit= / min_magnitude=."
            )
            event_ids = [event_id for event_id in event_ids if event_id not in deferred]

        fetching = sum(1 for event_id in event_ids if not self._is_cached(event_id))
        logger.info(
            f"ShakeMap {list(self._shakemap_layers)}: {fetching} event(s) to fetch, "
            f"{len(event_ids) - fetching} already on disk"
        )
        # A per-archive bar is useful for a single fetch and unreadable for
        # fifty, so a batch reports through the log line above instead.
        per_archive_bar = progress_bar and len(event_ids) == 1
        results, _failed = self._run_items(
            event_ids,
            lambda event_id: self._shakemap_for_event(
                event_id, progress_bar=per_archive_bar
            ),
            errors=self._errors,
            label="ShakeMap",
            describe=repr,
            on_failure=lambda _event_id, _exc: [],
        )
        written = [path for paths in results for path in paths]
        shakemap_root = self.root_dir / _SHAKEMAP_DIR
        # An empty `shakemap/` reads as "asked for, produced nothing" only if it
        # is there at all; removing it keeps a fruitless run from looking like a
        # partial success.
        with suppress(OSError):
            if shakemap_root.is_dir() and not any(shakemap_root.iterdir()):
                shakemap_root.rmdir()
        return written

    def _record_manifest(
        self,
        dest_dir: Path,
        produced: list[str],
        product_version: str | None = None,
    ) -> None:
        """Record an event's outcome, warning rather than failing if it cannot.

        The manifest is a cache, not the deliverable: a raster already written
        must not be lost because its bookkeeping could not be saved. A failure
        is surfaced, though, because the only symptom otherwise is a re-run
        that silently refetches everything.

        Args:
            dest_dir: The event's output directory.
            produced: The layers the archive actually carried.
            product_version: The ShakeMap product's `updateTime`, if known.

        Returns:
            None: The manifest is written as a side effect; a failure is
                logged rather than raised.
        """
        try:
            _helpers.write_manifest(
                dest_dir,
                self._shakemap_layers,
                produced,
                checked=time.time(),
                product_version=product_version,
            )
        except OSError as error:
            logger.warning(
                f"could not record the ShakeMap manifest in {dest_dir}: {error}. "
                "The rasters are written, but a re-run will fetch them again."
            )

    def _event_dir(self, comcat_id: str) -> Path | None:
        """Resolve an event's output directory, refusing one that escapes.

        Args:
            comcat_id: The event's ComCat id.

        Returns:
            Path | None: The directory, or `None` when it does not resolve
                inside the ShakeMap root.
        """
        shakemap_root = self.root_dir / _SHAKEMAP_DIR
        dest_dir = shakemap_root / comcat_id
        # Defence in depth. `parse_comcat_id` already refuses an id that could
        # traverse, but the id comes from an upstream server and this directory
        # is deleted from, so the containment is asserted rather than assumed.
        if not dest_dir.resolve().is_relative_to(shakemap_root.resolve()):
            logger.warning(
                f"refusing ComCat id {comcat_id!r}: it does not resolve inside "
                f"{shakemap_root} — skipping its ShakeMap."
            )
            return None
        return dest_dir

    def _cached_rasters(self, dest_dir: Path) -> list[Path] | None:
        """Decide whether an event can be served from disk, without logging.

        Split from :meth:`_reuse_existing` so the fan-out ceiling can ask the
        same question about an event it may not process, without emitting a
        skip line for work it never started.

        Args:
            dest_dir: The event's output directory.

        Returns:
            list[Path] | None: The rasters to reuse — empty for an event known
                to publish none — or `None` when the event must be fetched.
        """
        if self._force:
            return None
        manifest = _helpers.read_manifest(dest_dir)
        if manifest is None:
            return None
        # A previous call may have asked for fewer layers than this one. Reuse
        # only when the earlier request covered everything now wanted.
        previously = set(manifest.get("requested", []))
        if not set(self._shakemap_layers).issubset(previously):
            return None
        recorded = set(manifest.get("produced", []))
        produced = [layer for layer in self._shakemap_layers if layer in recorded]
        missing = [layer for layer in self._shakemap_layers if layer not in recorded]
        if missing and self._record_is_stale(manifest):
            # Any requested layer the last run did not produce is a negative
            # result, and negative results age out — whether or not some *other*
            # requested layer happens to be on disk. Checking this only when the
            # intersection was empty meant a partially-published event cached its
            # missing grids permanently, which is exactly the case the manifest
            # was introduced for.
            return None
        cached = [dest_dir / f"{layer}.tif" for layer in produced]
        if not all(self._is_complete(path) for path in cached):
            return None
        if missing:
            logger.warning(
                f"ShakeMap layer(s) {missing} are not available for this event and "
                f"were last checked recently; returning {len(cached)} of "
                f"{len(self._shakemap_layers)} requested. Pass force=True to "
                "re-check now."
            )
        return cached

    @staticmethod
    def _record_is_stale(manifest: dict[str, object]) -> bool:
        """Report whether a manifest's negative half should be re-checked.

        Args:
            manifest: The parsed manifest.

        Returns:
            bool: `True` when the record is older than the negative-cache
                window, or carries a timestamp that cannot be trusted — a
                future one, or a non-finite one. An unusable clock reading
                fails towards refetching rather than towards caching an
                answer forever.
        """
        stamp = manifest.get("checked", 0.0)
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return True
        age = time.time() - float(stamp)
        # Written as a range test so a negative age (a future stamp) and a NaN
        # age (every comparison False) both come out stale.
        return not (0.0 <= age <= _NEGATIVE_CACHE_SECONDS)

    def _is_fetchable(self, event_id: str) -> bool:
        """Report whether an event could ever yield a ShakeMap.

        Separated from :meth:`_is_cached` because the two answer different
        questions: this one asks whether the event is *addressable* at all,
        and an event that is not can never become cached, so counting it as
        pending work would let it block the queue indefinitely.

        Args:
            event_id: The event's `event_id` column value.

        Returns:
            bool: `False` when no ComCat id can be parsed, or the resolved
                directory would escape the ShakeMap root.
        """
        comcat_id = _helpers.parse_comcat_id(event_id)
        if comcat_id is None:
            logger.warning(
                f"cannot resolve a ComCat id from event_id {event_id!r} — "
                "skipping its ShakeMap."
            )
            return False
        return self._event_dir(comcat_id) is not None

    def _is_cached(self, event_id: str) -> bool:
        """Report whether an event needs no work this run.

        Args:
            event_id: The event's `event_id` column value.

        Returns:
            bool: `True` when the event is already satisfied on disk, so the
                fan-out ceiling should not spend budget on it.
        """
        comcat_id = _helpers.parse_comcat_id(event_id)
        if comcat_id is None:
            return False
        dest_dir = self._event_dir(comcat_id)
        if dest_dir is None:
            return False
        return self._cached_rasters(dest_dir) is not None

    def _reuse_existing(self, dest_dir: Path, comcat_id: str) -> list[Path] | None:
        """Return an event's cached rasters, or `None` to fetch it again.

        Reuse is decided from the event's manifest rather than from the
        requested layer names, because an archive need not carry every
        layer that was asked for. Without the manifest, an event whose
        archive permanently lacks one requested grid could never satisfy
        an "all requested rasters present" check and would re-download
        its multi-megabyte archive on every single run.

        Args:
            dest_dir: The event's output directory.
            comcat_id: The event's ComCat id, for the log line.

        Returns:
            list[Path] | None: The rasters already on disk for this
                request — possibly empty, when the event publishes no
                ShakeMap at all — or `None` when the event must be
                fetched.
        """
        cached = self._cached_rasters(dest_dir)
        if cached is None:
            return None
        if cached:
            logger.info(f"ShakeMap for {comcat_id} already on disk — skipping.")
        else:
            logger.info(
                f"as of the last run, event {comcat_id} published no ShakeMap "
                "raster for the requested layer(s) — skipping."
            )
        return cached

    def _shakemap_for_event(
        self, event_id: str, progress_bar: bool = True
    ) -> list[Path]:
        """Fetch, unpack, and convert one event's requested ShakeMap grids.

        Skips the network when the event's manifest says this run's
        layers are already accounted for — either their GeoTIFFs are on
        disk, or the archive was checked recently and carries none of
        them. The skip is decided from what a previous run *recorded*,
        not from whether every requested file exists, because an archive
        need not carry every layer that was asked for.

        Everything the fetch and the conversion touch stays inside a
        staging directory that is removed wholesale, so no intermediate
        — the archive, the `.flt` / `.hdr` pairs, or the `.prj` GDAL
        writes when the CRS is assigned — reaches the output folder.

        Args:
            event_id: The event's `event_id` column value, a QuakeML
                resource identifier carrying the ComCat id.
            progress_bar: Whether the archive fetch shows a progress bar.

        Returns:
            list[Path]: The GeoTIFFs written for this event; empty when
                the identifier carries no ComCat id, the event has no
                ShakeMap product, or the archive lacked every requested
                grid.
        """
        comcat_id = _helpers.parse_comcat_id(event_id)
        if comcat_id is None:
            logger.warning(
                f"cannot resolve a ComCat id from event_id {event_id!r} — "
                "skipping its ShakeMap."
            )
            return []

        dest_dir = self._event_dir(comcat_id)
        if dest_dir is None:
            return []

        reused = self._reuse_existing(dest_dir, comcat_id)
        if reused is not None:
            return reused

        detail = self._client().get_json(_helpers.detail_url(comcat_id))
        url = _helpers.shakemap_raster_url(detail)
        if url is None:
            logger.info(
                f"event {comcat_id} publishes no ShakeMap raster — skipping it."
            )
            # Recorded so a re-run does not re-request this event's detail
            # document only to reach the same conclusion.
            self._record_manifest(dest_dir, [])
            return []

        # Everything the fetch and the conversion touch is confined to a staging
        # directory that is removed wholesale, rather than cleaned up by globbing
        # the user's output directory for suffixes. That keeps GDAL's own
        # by-products out of the result: opening the `EHdr` grid read-write to
        # assign its CRS makes the driver drop a `<layer>.prj` beside the `.flt`,
        # which a suffix-based sweep would leave behind in the output folder.
        staging = dest_dir / f"{_STAGING_PREFIX}-{os.getpid()}"
        archive = staging / "raster.zip"
        written: list[Path] = []
        try:
            staging.mkdir(parents=True, exist_ok=True)
            # `expect_magic` keeps an HTML error page served with a 200 from
            # landing as a .zip and failing later as a confusing bad-archive.
            self._client().download(
                url, archive, expect_magic=b"PK", progress=progress_bar
            )
            extracted = _helpers.extract_layers(archive, self._shakemap_layers, staging)
            for layer, flt_path in extracted.items():
                written.append(
                    _helpers.flt_to_geotiff(flt_path, dest_dir / f"{layer}.tif")
                )
            # Only after every conversion succeeded: a manifest written on a
            # partial failure would cache an incomplete result as final.
            self._record_manifest(
                dest_dir,
                list(extracted),
                product_version=_helpers.shakemap_product_version(detail),
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if staging.exists():
                # Reported, not swallowed. On Windows this means something still
                # holds a handle on an extracted grid; silently leaving it would
                # accumulate one scratch directory per failed event inside the
                # user's output.
                logger.warning(
                    f"could not remove the ShakeMap staging directory {staging} — "
                    "it is left behind; remove it by hand if it persists."
                )
            # An event whose archive was unusable leaves no rasters, and an
            # empty directory named after it reads like a successful fetch.
            with suppress(OSError):
                if dest_dir.is_dir():
                    remaining = list(dest_dir.iterdir())
                    if not remaining:
                        dest_dir.rmdir()
                    elif all(
                        item.name.startswith(_STAGING_PREFIX) for item in remaining
                    ):
                        # Another process's scratch space, or one left by a run
                        # that died. Not ours to delete, but silence would let it
                        # accumulate unnoticed and keep this directory alive.
                        logger.warning(
                            f"{dest_dir} holds only orphaned scratch "
                            f"director(ies) {[item.name for item in remaining]} "
                            "from an interrupted run; remove them by hand."
                        )
        return written

    def _write(self, provider_key: str, collection: FeatureCollection) -> Path:
        """Write one network's events to a vector file under `root_dir`.

        Args:
            provider_key: The network key, used as the filename stem.
            collection: The network's events.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _DRIVERS[self._file_format]
        out_path = self.root_dir / f"{provider_key.lower()}.{ext}"
        collection.to_file(str(out_path), driver=driver)
        return out_path
