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
and the :class:`earthlens.earthlens.EarthLens` facade rejects an
`aggregate=` argument (there is no meaningful gridded reduction of an
event table). `download()` returns the in-memory FeatureCollection
(the union across requested networks) and, as a side effect, writes
one vector file per network to `path`.

Provider selection follows the FDSN-specific reading of `variables`
(see the package docstring): `variables` is a `list[str]` of network
keys (`["USGS"]`, `["USGS", "EMSC"]`); query filters arrive as
explicit constructor kwargs. The temporal window is a single
unchunked `[start, end]` `get_events` call — FDSN does not iterate per
day/month — so `temporal_resolution` carries the sentinel `"all"`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.fdsn import events
from earthlens.fdsn.auth import resolve_earthscope_token
from earthlens.fdsn.catalog import Catalog, Provider

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

    from earthlens.aggregate import AggregationConfig


FileFormat = Literal["gpkg", "geojson"]

#: Map output format to the OGR driver and file extension `to_file` uses.
_DRIVERS: dict[str, tuple[str, str]] = {
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}

#: Default network when `variables` is empty.
_DEFAULT_PROVIDERS = ["USGS"]


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
            features (events), so the facade rejects `aggregate=`
            with `NotImplementedError`. This backend is the first
            end-to-end exercise of that facade guard.
    """

    OUTPUT_KIND: OutputKind = "vector"

    #: Partial-failure policy for the per-provider loop; `download(errors=...)`
    #: overrides it per call.
    _errors: str = "warn"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "all",
        path: Path | str = "",
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
            limit: Maximum number of events per network, or `None`.
            earthscope_token: Optional EarthScope access token; falls
                back to `EARTHSCOPE_TOKEN` / `~/.earthscope_token`.
                Used only for a provider that requires a token.
            file_format: Output vector format — `"gpkg"` (default,
                GeoPackage) or `"geojson"`.
        """
        self._min_magnitude = min_magnitude
        self._max_magnitude = max_magnitude
        self._min_depth = min_depth
        self._max_depth = max_depth
        self._magnitude_type = magnitude_type
        self._event_type = event_type
        self._orderby = orderby
        self._limit = limit
        self._earthscope_token_arg = earthscope_token
        self._earthscope_token: str | None = None
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got {file_format!r}."
            )
        self._file_format: FileFormat = file_format
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
        for key in self.vars:
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

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[FeatureCollection]: One collection per product, in the
                same order; empty collections for no-data or failed
                networks.

        Args:
            progress_bar: Whether to show per-provider progress.
            aggregate: Rejected by the facade for a vector backend.
            errors: Partial-failure policy for the per-provider loop —
                `"warn"` (default) logs each failed network and continues,
                `"raise"` propagates the first failure, `"ignore"` continues
                silently. An all-failed batch still raises regardless.

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
                limit=self._limit,
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
        aggregate: AggregationConfig | None = None,
        errors: str = "warn",
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

        Args:
            progress_bar: Accepted for signature parity with the other
                backends; obspy's `get_events` has no progress bar, so
                this is currently a no-op.
            aggregate: Must be `None`. Seismic events are vector, not
                gridded, so there is no meaningful aggregation. The
                facade already rejects a non-`None` `aggregate=` for a
                `vector` backend; this is the belt-and-suspenders guard
                for direct backend callers.

        Returns:
            FeatureCollection: The row-wise union of every requested
                network's events, CRS `EPSG:4326`. Empty (schema-only)
                when no network matched anything.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
            RuntimeError: If **every** requested network's query failed
                (propagated from :meth:`_fetch`). A partial failure does
                not raise — the healthy networks' events are returned.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "FDSN.download(aggregate=...) is not supported: seismic "
                "events are vector point features, not gridded rasters, so "
                "there is no meaningful gridded reduction. Call download() "
                "without aggregate= and post-process the returned "
                "FeatureCollection (a GeoDataFrame) directly."
            )

        self._errors = self.check_errors_policy(errors)
        products = self._search()
        collections = self._fetch(products) if products else []

        written: list[Path] = []
        for product, collection in zip(products, collections):
            if len(collection):
                written.append(self._write(product.id, collection))

        combined = events.concat_fcs(collections)
        # `concat_fcs` has copied every network's events, so the per-network
        # copies are dead weight from here on. This does not lower the *peak* —
        # that was reached inside the concat, with both sets live — but it stops
        # them being held through the summary, the return, and however long the
        # caller keeps the result.
        collections.clear()
        if written:
            logger.info(
                f"FDSN download summary: {len(combined)} events across "
                f"{len(written)} file(s) written to {self.root_dir}"
            )
        else:
            logger.warning(
                "FDSN download summary: no events matched the request, nothing written"
            )
        return combined

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
