"""Backend that fetches EUMETSAT Data Store products via `eumdac`.

`EUMETSAT(AbstractDataSource)` accepts the same constructor surface as
the other earthlens backends — `start`, `end`, `variables`, `lat_lim`,
`lon_lim`, `temporal_resolution`, `path` — plus a few backend-specific
kwargs for the OAuth2 consumer key / secret and group disambiguation.
Each `(dataset_key, [selector, ...])` pair in the `variables` mapping
names one curated Data Store collection to search (bbox + window) and
fetch.

**This backend's `OUTPUT_KIND` is per-instance, not fixed (`G1`).**
EUMETSAT spans gridded imagery (SEVIRI / FCI L1.5/L1c, OLCI / SLSTR
grids) and swath / sounding products (S5P TROPOMI L2, ASCAT, IASI). The
class default is `"raster"`; `__init__` resolves the requested
collection row(s) and copies the row's `output_kind` onto
`self.OUTPUT_KIND`. The `earthlens.earthlens.EarthLens` facade reads that
per-instance value at `download()` time to gate `aggregate=`.

A single request may name several collections, but they must all share
one `output_kind` — a mixed raster+vector request is rejected at
construction.

By default the backend fetches **whole native products** to disk (`G4`):
`eumdac`'s `Product.open()` stream copied to a file. The selectors in
`variables` are informational for a whole-product fetch (`G2`), and `bbox`
is a search filter (which products intersect), not a pixel crop.

Server-side subset / reproject / reformat is EUMETSAT's **Data Tailor**
service, reached with `download(tailor=TailorConfig(...))` (`H4`). That
routes each matching product through Data Tailor (submit → poll → stream →
delete) and returns the customised GeoTIFF / NetCDF paths instead of the
native product; every customisation is deleted afterwards for quota
hygiene (`G7`). Only catalog rows carrying a `tailor_product_type` are
Data-Tailor-eligible (`G5`).

`tailor=` is **spatial**; the temporal reducer is a separate `aggregate=`
knob (`G1`). `download(aggregate=...)` still raises `NotImplementedError`
— EUMETSAT native NetCDF products can be reduced client-side with pyramids
after download, and the two knobs compose (tailor server-side, then reduce
client-side).
"""

from __future__ import annotations

import contextlib
import functools
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, TypeVar

from loguru import logger
from pydantic import SecretStr

from earthlens.base import (
    CADENCE_ALIASES,
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    end_is_date_only,
    expand_bare_date_end,
)
from earthlens.eumetsat._helpers import eumdac_bbox, safe_product_filename
from earthlens.eumetsat.auth import EumetsatAuth, EumetsatCredentials
from earthlens.eumetsat.catalog import Catalog, DataStoreGroup, EumetsatDataset
from earthlens.eumetsat.tailor import TailorConfig

#: Total wall-clock budget for polling one Data Tailor customisation to a
#: terminal state before giving up (`G8`). A stuck job must not hang forever.
TAILOR_POLL_TIMEOUT_S = 1800.0
#: First poll delay; grows by `TAILOR_POLL_BACKOFF` up to `TAILOR_POLL_MAX_S`.
TAILOR_POLL_INITIAL_S = 5.0
#: Ceiling on the poll delay.
TAILOR_POLL_MAX_S = 30.0
#: Multiplicative backoff applied to the poll delay after each check.
TAILOR_POLL_BACKOFF = 1.5
#: How many times to retry a *transient* customisation submit (`G8`).
TAILOR_SUBMIT_RETRIES = 3
#: Base backoff between submit retries (scaled by the attempt number).
TAILOR_SUBMIT_BACKOFF_S = 2.0
#: Non-terminal ("still working") Data Tailor `Customisation.status` values
#: (`A1`). Polling continues only while the status is one of these; any other
#: status (`DONE`, `FAILED`, `KILLED`, or an unexpected one like `INACTIVE`)
#: is treated as terminal so an unknown stuck state fails fast instead of
#: polling until the timeout.
_TAILOR_ACTIVE = frozenset({"QUEUED", "RUNNING"})
#: Substrings that mark a submit error as transient (worth retrying) — the
#: EUMETSAT EPCS endpoint intermittently 502s (`A1`/`G8`). A bounded-call
#: timeout (`TAILOR_HTTP_TIMEOUT_S`) surfaces as a `requests` timeout whose
#: message contains `"timed out"` / `"connection"`, so a stalled *submit* is
#: retried by this same set (#1146).
_TRANSIENT_MARKERS = (
    "502",
    "bad gateway",
    "server-side",
    "timed out",
    "timeout",
    "connection",
)
#: Per-call `(connect, read)` socket timeout injected into every eumdac HTTP
#: request (Data Store + Data Tailor). `eumdac` sets no timeout and exposes no
#: session to configure, so without this a stalled socket blocks forever and
#: defeats `TAILOR_POLL_TIMEOUT_S` (#1146). The read bound is per socket read,
#: so it caps a *stalled* stream without killing a slow-but-alive one.
TAILOR_HTTP_TIMEOUT_S: tuple[float, float] = (30.0, 300.0)
#: Substrings in a `FAILED` customisation's log tail that mark the failure as a
#: transient EUMETSAT-side infrastructure fault (NFS / disk), worth resubmitting
#: rather than surfacing — as opposed to a bad request, which fails fast (#1145).
_TRANSIENT_OUTCOME_MARKERS = (
    "stale file handle",
    "errno 116",
    "no space left",
    "input/output error",
)

_P = ParamSpec("_P")
_R = TypeVar("_R")


@contextlib.contextmanager
def _bounded_http(timeout: tuple[float, float] = TAILOR_HTTP_TIMEOUT_S):
    """Give every `requests` call a default `(connect, read)` timeout.

    `eumdac` issues module-level `requests.get` / `post` / … (it owns no
    shared `requests.Session` to configure), and every one of those funnels
    through `requests.Session.request`. Wrapping that method so a call which
    passes no `timeout` gets `timeout` bounds every Data Store / Data Tailor
    socket operation — connect and each read — for the duration of the block,
    without touching `eumdac` internals. An explicit `timeout=` is preserved.

    The wrap is re-entrant (a nested block reuses the outer wrap) and restored
    on exit. It is not concurrency-safe across threads — earthlens backends
    run synchronously, so two downloads customising in parallel is out of
    scope; the guard only covers nesting on one thread.

    Args:
        timeout: The `(connect, read)` timeout in seconds to inject.

    Yields:
        None: for the duration of the bounded block.
    """
    import requests  # lazy — guaranteed by earthlens-core, only needed live

    session = requests.sessions.Session
    original = session.request
    if getattr(original, "_earthlens_bounded", False):
        yield  # an outer block already wrapped it
        return

    @functools.wraps(original)
    def _request(self, *args, **kwargs):
        # Inject the default only when the caller gave no timeout — as a keyword
        # or positionally. `timeout` is the 9th positional parameter of
        # `Session.request` (method, url, params, data, headers, cookies, files,
        # auth, timeout), i.e. `args[8]`; guarding on `len(args)` avoids a
        # "multiple values for 'timeout'" TypeError if it is ever passed
        # positionally.
        if "timeout" not in kwargs and len(args) < 9:
            kwargs["timeout"] = timeout
        return original(self, *args, **kwargs)

    _request._earthlens_bounded = True  # type: ignore[attr-defined]
    session.request = _request  # type: ignore[method-assign]
    try:
        yield
    finally:
        session.request = original  # type: ignore[method-assign]


def _bounded_http_calls(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Decorator: run `method` with every `requests` call time-bounded (#1146).

    Applied to the backend methods that reach the network (`_search`,
    `_fetch`, `_tailor_one`) so a stalled Data Store / Data Tailor socket
    cannot hang the download.

    Args:
        method: The backend method to wrap.

    Returns:
        The wrapped method, with its signature preserved.
    """

    @functools.wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with _bounded_http():
            return method(*args, **kwargs)

    return wrapper


class EUMETSAT(AbstractDataSource):
    """EUMETSAT Data Store backend (per-collection output kind).

    Wraps `eumdac` so a user can search a curated EUMETSAT collection by
    bbox + window and fetch its native products through the same
    `download()` shape every other earthlens backend uses. One OAuth2
    consumer key / secret authenticates across every collection.

    Attributes:
        OUTPUT_KIND: Class default `"raster"`, **overridden per instance**
            in `__init__` from the resolved collection row's
            `output_kind` (`G1`). The facade reads this instance value to
            gate `aggregate=`.
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = (
        "the temporal reducer is not wired for this backend. Download the "
        "products (optionally with tailor= for a server-side subset / "
        "reproject) and reduce the NetCDF ones client-side with pyramids"
    )

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        group: DataStoreGroup | str | None = None,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        credentials_file: Path | str | None = None,
    ):
        """Initialise an EUMETSAT backend instance.

        Resolves every requested dataset key against the catalog
        **before** calling the parent constructor, so the per-instance
        `OUTPUT_KIND` is set from the resolved row(s). The parent
        `__init__` runs `_initialize` first (token mint), so the
        resolution cannot live there.

        Args:
            start: Inclusive start date as a string (parsed with `fmt`).
            end: Inclusive end date as a string.
            variables: Mapping from curated dataset key to a list of
                selectors, e.g. `{"msg-hrseviri": ["HRSEVIRI"]}`.
                Selectors are informational for the whole-product fetch
                (`G2`).
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory cadence label. Defaults to
                `"daily"`.
            path: Output directory. Created by the parent class if it
                does not exist.
            fmt: `strptime` format for `start` / `end`. Defaults to
                `"%Y-%m-%d"`.
            group: Optional `DataStoreGroup` (or its string value) used to
                assert which Data Store group the requested collection(s)
                belong to (`G2`).
            consumer_key: EUMETSAT consumer key. Falls back to
                `EUMETSAT_CONSUMER_KEY`, then `~/.eumdac/credentials`.
            consumer_secret: EUMETSAT consumer secret. Falls back to
                `EUMETSAT_CONSUMER_SECRET`, then the credentials file.
            credentials_file: Optional explicit path to a `key,secret`
                credentials file.

        Raises:
            ValueError: When `variables` is empty, a dataset key is
                unknown, or the requested collections do not all share
                one `output_kind`.
        """
        self._group = group
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._credentials_file = (
            Path(credentials_file) if credentials_file is not None else None
        )
        self._auth: EumetsatAuth | None = None
        self._show_progress = True

        self._catalog = Catalog()
        self._datasets: list[EumetsatDataset] = self._resolve_datasets(variables)
        self.OUTPUT_KIND = self._unify_output_kind(self._datasets)

        super().__init__(
            start=start,
            end=end,
            variables=variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _resolve_datasets(
        self, variables: dict[str, list[str]]
    ) -> list[EumetsatDataset]:
        """Resolve every requested dataset key to a catalog row.

        Args:
            variables: The `{dataset_key: [selector, ...]}` request.

        Returns:
            list[EumetsatDataset]: One row per key, in request order.

        Raises:
            ValueError: When `variables` is empty or a key is unknown
                (the catalog's did-you-mean is surfaced in the message).
        """
        if not variables:
            raise ValueError(
                "EUMETSAT requires a non-empty `variables` mapping of "
                "{dataset_key: [selector, ...]}."
            )
        return [self._catalog.resolve(key, group=self._group) for key in variables]

    @staticmethod
    def _unify_output_kind(datasets: list[EumetsatDataset]) -> OutputKind:
        """Return the single `output_kind` shared by every requested row.

        A backend instance carries exactly one `OUTPUT_KIND`, so a
        request mixing (say) a raster and a vector dataset is ambiguous
        and rejected here.

        Args:
            datasets: The resolved dataset rows.

        Returns:
            OutputKind: The shared `output_kind`.

        Raises:
            ValueError: When the rows do not all share one `output_kind`.
        """
        kinds = {ds.output_kind for ds in datasets}
        if len(kinds) > 1:
            detail = ", ".join(
                f"{ds.collection_id}={ds.output_kind}" for ds in datasets
            )
            raise ValueError(
                "all datasets in one EUMETSAT request must share one "
                f"output_kind; got mixed kinds ({detail}). Split the "
                "request into one call per output kind."
            )
        return kinds.pop()

    def _initialize(self):
        """Build the `EumetsatAuth`; defer token minting.

        Returns `None` — `eumdac` keeps the token on the `EumetsatAuth`
        instance, so the parent class binds no opaque `self.client`. The
        token minting (`EumetsatAuth.configure`, which contacts the auth
        server) is deferred out of construction: it runs on the first
        :meth:`_search` (the `eumdac` collection search authenticates via
        the idempotent `configure()`), so constructing the backend never
        authenticates — but note that a dry-run `search()` does, since the
        `eumdac` data store needs a token.
        """
        creds = EumetsatCredentials(
            consumer_key=self._consumer_key,
            consumer_secret=(
                SecretStr(self._consumer_secret)
                if self._consumer_secret is not None
                else None
            ),
            credentials_file=self._credentials_file,
        )
        self._auth = EumetsatAuth(creds)
        return None

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the date range into a `TemporalExtent`.

        Args:
            start: Inclusive start date as a string.
            end: Inclusive end date as a string.
            temporal_resolution: Advisory cadence label; mapped to a
                pandas frequency for the `dates` index when known.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Raises:
            ValueError: If `temporal_resolution` is not one of the cadences
                `earthlens.base.CADENCE_ALIASES` accepts.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        self._end_is_date_only = end_is_date_only(end)
        return self._cadence_extent(
            start,
            end,
            fmt=fmt,
            cadence=temporal_resolution,
            accepted=CADENCE_ALIASES,
        )

    @_bounded_http_calls
    def _search(self) -> list[RemoteProduct]:
        """Query the Data Store for products of every requested collection.

        One `Collection.search(bbox=, dtstart=, dtend=)` per resolved
        collection row, scoped to the request bbox and time window. The
        bbox is the `eumdac` `W,S,E,N` comma-string the OpenSearch
        endpoint expects.

        How `end` is interpreted depends on whether it carries a time of
        day. A **date-only** `end` parses to midnight, which would collapse
        a same-day request (`start == end`) to a zero-width instant, so it
        is read as *inclusive of its whole calendar day* and `dtend` is
        widened to `23:59:59.999999`. An `end` that **names a time** means
        that instant and is passed through unchanged — widening it would
        pull every later product of the day, which for a 10-minute
        full-disk cadence is tens of gigabytes the caller never asked for.

        Each returned `eumdac` product becomes one `RemoteProduct` whose
        `metadata` carries the raw product handle and its collection row,
        so `_fetch` can stream without re-querying.

        Returns:
            list[RemoteProduct]: One product per matching Data Store
                product, across every requested collection. An empty list
                (no products in the window) short-circuits the fetch.

        Raises:
            ImportError: When the `[eumetsat]` extra (`eumdac`) is not
                installed — surfaced by `EumetsatAuth.datastore()`.
        """
        assert self._auth is not None  # set by _initialize
        self._auth.configure()
        store = self._auth.datastore()
        # A `SpatialExtent` constrains longitude to a single `[-180, 180]`
        # range with `west <= east`, so it cannot represent an
        # antimeridian-crossing box — a single search bbox always suffices.
        bbox = eumdac_bbox(
            self.space.west, self.space.south, self.space.east, self.space.north
        )
        dtstart = self.time.start_date
        dtend = expand_bare_date_end(
            self.time.end_date, date_only=self._end_is_date_only
        )
        products: list[RemoteProduct] = []
        for ds in self._datasets:
            collection = store.get_collection(ds.collection_id)
            for product in collection.search(
                bbox=bbox,
                dtstart=dtstart,
                dtend=dtend,
            ):
                products.append(
                    RemoteProduct(
                        id=str(product),
                        metadata={"product": product, "dataset": ds},
                    )
                )
        return products

    @_bounded_http_calls
    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Stream every product `_search` returned to a local file.

        Each `eumdac` product is opened (`Product.open()` — a streaming
        context manager) and copied to `self.root_dir / <id>`, where the
        product id is reduced to a safe basename (`safe_product_filename`)
        so a server-supplied id with a path separator cannot write outside
        the output directory.

        Args:
            products: The products from `_search`.

        Returns:
            list[Path]: Local paths of every fetched product, in search
                order.
        """
        out_paths: list[Path] = []
        for rp in products:
            product = rp.metadata["product"]
            target = self.root_dir / safe_product_filename(str(product))
            with product.open() as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out_paths.append(target)
        return out_paths

    def download(
        self,
        progress_bar: bool = True,
        tailor: TailorConfig | None = None,
    ) -> list[Path]:
        """Search the Data Store and return product paths (native or tailored).

        With no `tailor=`, composes `_search` and `_fetch` to pull every
        product matching the request bbox + window to `self.root_dir` as
        whole native products (unchanged behaviour).

        With `tailor=TailorConfig(...)`, each matching product is routed
        through EUMETSAT **Data Tailor** (server-side subset / reproject /
        reformat, `H4`): submit a customisation, poll it to `DONE`, stream
        the customised output(s) to `self.root_dir`, and delete the
        customisation (`G7`). The returned paths are then the customised
        GeoTIFF / NetCDF files, not the native products (`G6`).

        `aggregate=` is the **temporal** reducer (`G1`) and is a separate
        operation from the spatial `tailor=`; it is not implemented for
        EUMETSAT, so a non-`None` `aggregate` raises `NotImplementedError`.
        The two knobs compose — tailor server-side here, then reduce the
        result client-side with pyramids.

        The tailor branch is **fail-fast per batch**: if one product's
        customisation fails, the error propagates and paths already streamed
        for earlier products are not returned (their files remain on disk).
        This mirrors the native fetch; keep batches small when a partial
        result would be costly to recompute.

        Args:
            progress_bar: Reserved for parity with the other backends;
                `eumdac`'s streaming download has no built-in bar.
            tailor: Optional `TailorConfig` routing the request through
                Data Tailor. `None` (the default) keeps the native fetch.

        Returns:
            list[Path]: The native product paths, or — when `tailor=` is
                given — the customised output paths.

        Raises:
            ValueError: When `tailor=` names a dataset that is not
                Data-Tailor-eligible (`G5`).
        """
        self._show_progress = progress_bar
        if tailor is not None:
            return self._tailor(tailor)
        return self._api_via_search_fetch()

    def _tailor(self, tailor: TailorConfig) -> list[Path]:
        """Run the Data Tailor branch for every matching product (`G2`).

        Rejects a non-eligible request up front (`G5`), then searches the
        Data Store and customises each product in turn, returning the
        flattened list of customised output paths.

        Args:
            tailor: The `TailorConfig` describing the customisation.

        Returns:
            list[Path]: Every customised output path, in search order.

        Raises:
            ValueError: When any requested dataset lacks a
                `tailor_product_type` (not Data-Tailor-eligible).
        """
        ineligible = [ds for ds in self._datasets if ds.tailor_product_type is None]
        if ineligible:
            names = ", ".join(ds.collection_id for ds in ineligible)
            raise ValueError(
                f"{names} not Data-Tailor-eligible; download native (no "
                "tailor=) and reduce client-side with pyramids."
            )
        assert self._auth is not None  # set by _initialize
        self._auth.configure()
        datatailor = self._auth.datatailor()
        products = self._search()
        out_paths: list[Path] = []
        used_dirs: set[str] = set()
        for rp in products:
            out_paths.extend(self._tailor_one(rp, tailor, datatailor, used_dirs))
        return out_paths

    @staticmethod
    def _dedupe_name(name: str, used: set[str]) -> str:
        """Return `name`, suffixed if needed so it is unique within `used`.

        Guarantees a distinct on-disk name even when two products share an
        id (`L3`) or two customisation outputs sanitise to one basename
        (`L2`). Adds the chosen name to `used`.

        Args:
            name: The candidate (already path-safe) name.
            used: The set of names already taken; mutated in place.

        Returns:
            str: A name not already in `used`.
        """
        candidate = name
        counter = 1
        while candidate in used:
            candidate = f"{name}_{counter}"
            counter += 1
        used.add(candidate)
        return candidate

    @_bounded_http_calls
    def _tailor_one(
        self,
        product: RemoteProduct,
        tailor: TailorConfig,
        datatailor,
        used_dirs: set[str],
    ) -> list[Path]:
        """Customise one product via Data Tailor; always clean up (`G7`).

        Builds the `eumdac` `Chain` from `tailor`, the product's catalog
        row (`tailor_product_type`), and the request ROI (`tailor.bbox`
        else `self.space`), submits it, polls to a terminal state, streams
        every output to `self.root_dir`, and deletes the customisation in
        a `finally` — even on failure — so quota is always freed. Every
        network call runs time-bounded (`@_bounded_http_calls`, #1146).

        A customisation that ends `FAILED` on a transient EUMETSAT-side
        infrastructure fault — its log tail matching
        `_TRANSIENT_OUTCOME_MARKERS` (a stale NFS handle, a full disk) — is
        **resubmitted** within the `TAILOR_SUBMIT_RETRIES` budget rather
        than surfaced, the abandoned job being deleted first so quota is not
        leaked (#1145). A `FAILED` from a bad request (no marker), and any
        `KILLED` / other terminal status, still fails fast on the first
        attempt.

        A `tailor.crs` of `None` means "do not reproject": `Chain.projection`
        is `None`, which `Chain.asdict()` drops before the request is built,
        matching what the native output formats require.

        Args:
            product: One `RemoteProduct` from `_search` (its `metadata`
                carries the raw `eumdac` product handle and catalog row).
            tailor: The customisation request.
            datatailor: The live `eumdac.DataTailor` client.
            used_dirs: Shared per-batch set of subdirectory names already
                taken, mutated here to keep each product's output directory
                unique across the request (`L3`).

        Returns:
            list[Path]: The customised output paths for this product.

        Raises:
            ValueError: When the product's dataset is not eligible (`G5`).
            RuntimeError: When the customisation ends `FAILED` / `KILLED`
                (with the server log), or a transient submit keeps failing.
            TimeoutError: When polling exceeds `TAILOR_POLL_TIMEOUT_S`.
        """
        import eumdac  # lazy — the [eumetsat] extra

        dataset: EumetsatDataset = product.metadata["dataset"]
        if dataset.tailor_product_type is None:
            raise ValueError(
                f"{dataset.collection_id!r} is not Data-Tailor-eligible; "
                "download native (no tailor=) and reduce client-side."
            )
        nswe = tailor.nswe or TailorConfig.nswe_from_extent(
            self.space.north, self.space.south, self.space.west, self.space.east
        )
        # `crs=None` means "do not reproject" (TailorConfig already forbids pairing
        # it with a native format). `Chain` is a dataclass with `projection: str |
        # None = None`, and its `asdict()` -- what actually gets serialised into the
        # request -- drops every `None` field (`eumdac.tailor_models.AsDictMixin`).
        # So passing `projection=None` here and omitting the argument entirely
        # produce the identical request; no conditional kwarg-building is needed.
        chain = eumdac.tailor_models.Chain(
            product=dataset.tailor_product_type,
            format=tailor.format,
            projection=tailor.crs,
            # eumdac types NSWE as Optional[str], but the Data Tailor ROI takes
            # a north/south/west/east list (see TailorConfig.nswe).
            roi=eumdac.tailor_models.RegionOfInterest(NSWE=nswe),  # type: ignore[arg-type]
            filter=(
                eumdac.tailor_models.Filter(bands=list(tailor.filter))
                if tailor.filter
                else None
            ),
            # eumdac types quicklook as a Quicklook/dict; the API accepts a truthy flag.
            quicklook=tailor.quicklook or None,  # type: ignore[arg-type]
        )
        product_handle = product.metadata["product"]
        # Choose the per-product output subdir *before* submitting, so nothing
        # between the submit and the `try/finally` can raise and orphan the
        # customisation (quota hygiene, G7). Namespacing avoids cross-granule
        # basename collisions (H1); the name is de-duped against this batch and
        # against any pre-existing native file of the same basename (L3).
        subdir = self._dedupe_name(
            safe_product_filename(str(product_handle)), used_dirs
        )
        while (self.root_dir / subdir).exists() and not (
            self.root_dir / subdir
        ).is_dir():
            subdir = self._dedupe_name(subdir, used_dirs)
        product_dir = self.root_dir / subdir
        # Submit → poll → (stream on DONE). A FAILED on a transient EUMETSAT-side
        # infrastructure fault is resubmitted within the shared retry budget,
        # deleting the abandoned job first so quota is not leaked (#1145).
        for attempt in range(1, TAILOR_SUBMIT_RETRIES + 1):
            cust = self._submit_customisation(datatailor, product_handle, chain)
            try:
                status = self._poll_customisation(cust)
                if status == "DONE":
                    product_dir.mkdir(parents=True, exist_ok=True)
                    written: list[Path] = []
                    used_names: set[str] = set()
                    for name in cust.outputs:
                        # De-dupe within a customisation so two outputs sharing
                        # a basename do not overwrite each other (L2).
                        out_name = self._dedupe_name(
                            safe_product_filename(str(name)), used_names
                        )
                        target = product_dir / out_name
                        with cust.stream_output(name) as src, open(target, "wb") as fh:
                            shutil.copyfileobj(src, fh)
                        written.append(target)
                    return written
                tail = self._logfile_tail(cust)
                if (
                    status == "FAILED"
                    and attempt < TAILOR_SUBMIT_RETRIES
                    and any(m in tail.lower() for m in _TRANSIENT_OUTCOME_MARKERS)
                ):
                    logger.warning(
                        f"Data Tailor customisation {cust} ended FAILED on a "
                        f"transient infrastructure error; resubmitting (attempt "
                        f"{attempt}/{TAILOR_SUBMIT_RETRIES}): {tail}"
                    )
                    # Delete the abandoned job BEFORE resubmitting so the retry
                    # does not leak quota (G7); null it so `finally` does not
                    # double-delete.
                    self._safe_delete(cust)
                    cust = None
                else:
                    raise RuntimeError(
                        f"Data Tailor customisation {cust} ended {status}: {tail}"
                    )
            finally:
                if cust is not None:
                    self._safe_delete(cust)  # ALWAYS free quota (G7)
            time.sleep(TAILOR_SUBMIT_BACKOFF_S * attempt)
        # The final attempt always returns (DONE) or raises (non-retryable),
        # so control never reaches here; kept to satisfy the type checker.
        raise RuntimeError(  # pragma: no cover
            "Data Tailor customisation did not resolve within the retry budget."
        )

    @staticmethod
    def _safe_delete(cust) -> None:
        """Delete a customisation, never letting cleanup mask the real error.

        Called from the `_tailor_one` `finally` (`G7`). A `delete()` that
        itself raises must not replace the original `RuntimeError` /
        `TimeoutError`, nor abort the remaining products in the batch — so
        the failure is logged and swallowed.

        Args:
            cust: The `eumdac` `Customisation` handle to delete.
        """
        try:
            cust.delete()
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask the cause
            logger.warning(f"Data Tailor customisation {cust} delete failed: {exc}")

    @staticmethod
    def _submit_customisation(datatailor, product, chain):
        """Submit a customisation, retrying transient EPCS failures (`G8`).

        The EUMETSAT EPCS endpoint intermittently returns `502 Bad
        Gateway`; a submit that fails with a transient marker is retried
        up to `TAILOR_SUBMIT_RETRIES` times with a linear backoff. A
        non-transient error (e.g. an invalid product id) is re-raised
        immediately.

        Note:
            If a create actually succeeds server-side but its response is
            lost (a dropped connection / timeout — the classified-transient
            cases), the retry submits a **second** customisation and the
            first is orphaned: the client never gets its handle, so it is
            not polled or deleted and lingers against the quota. The EPCS
            API offers no idempotency key to prevent this; recover by
            sweeping stale jobs (`eumdac.DataTailor(token).customisations`).

        Args:
            datatailor: The live `eumdac.DataTailor` client.
            product: The `eumdac` product handle to customise.
            chain: The `eumdac.tailor_models.Chain` describing the job.

        Returns:
            The `eumdac` `Customisation` handle for the submitted job.

        Raises:
            RuntimeError: When a transient submit keeps failing after
                `TAILOR_SUBMIT_RETRIES` attempts.
            Exception: Any non-transient submit error, re-raised as-is.
        """
        last_exc: Exception | None = None
        for attempt in range(1, TAILOR_SUBMIT_RETRIES + 1):
            try:
                return datatailor.new_customisation(product, chain)
            except Exception as exc:  # noqa: BLE001 - classified below
                message = str(exc).lower()
                if not any(mark in message for mark in _TRANSIENT_MARKERS):
                    raise
                last_exc = exc
                if attempt < TAILOR_SUBMIT_RETRIES:
                    time.sleep(TAILOR_SUBMIT_BACKOFF_S * attempt)
        raise RuntimeError(
            f"Data Tailor submit failed after {TAILOR_SUBMIT_RETRIES} "
            f"transient attempts: {last_exc}"
        ) from last_exc

    @staticmethod
    def _poll_customisation(cust) -> str:
        """Poll a customisation until it stops being active; return its status.

        Polls `cust.status` while it is `_TAILOR_ACTIVE` (`QUEUED` /
        `RUNNING`), sleeping `TAILOR_POLL_INITIAL_S` and growing the delay
        by `TAILOR_POLL_BACKOFF` up to `TAILOR_POLL_MAX_S`. Any other status
        is terminal and is returned immediately — so `DONE` succeeds and
        `FAILED` / `KILLED` / an unexpected stuck state (e.g. `INACTIVE`)
        fail fast rather than polling to the timeout. Gives up after
        `TAILOR_POLL_TIMEOUT_S` so a job stuck *active* cannot hang forever
        (`G8`); the final sleep is clamped to the remaining budget so the
        wall-clock never overshoots the timeout.

        Each `cust.status` read is a bounded HTTP call (the caller runs under
        `_bounded_http`, #1146). A single poll that hits a **transient**
        transport error — a stalled read/connect or a dropped connection — is
        **not** fatal: the job may still be progressing server-side, so it is
        treated like "still active" and polling continues until the wall-clock
        deadline, which is what actually caps the wait now that the deadline is
        a real wall-clock bound (#1146). A **permanent** error is not caught and
        fails fast: eumdac wraps an HTTP 4xx/5xx response into an `EumdacError`
        (not a `requests` exception), which propagates straight out.

        Args:
            cust: The `eumdac` `Customisation` handle to poll.

        Returns:
            str: The terminal status (`"DONE"`, `"FAILED"`, `"KILLED"`, or
                any non-active value the service reports).

        Raises:
            TimeoutError: When the job is still active (or its status could
                not be read) after `TAILOR_POLL_TIMEOUT_S`.
        """
        import requests  # lazy — guaranteed by earthlens-core, only needed live

        # Only a genuinely *transient* transport error is ridden out: a stalled
        # read/connect or a dropped connection. A permanent error is NOT caught
        # so it fails fast — eumdac already wraps an HTTP 4xx/5xx response into a
        # (non-`RequestException`) `EumdacError`, and any other `requests` error
        # (a malformed URL, say) is a real fault, not a blip.
        transient = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            TimeoutError,
        )
        deadline = time.monotonic() + TAILOR_POLL_TIMEOUT_S
        delay = TAILOR_POLL_INITIAL_S
        while True:
            try:
                status = str(cust.status).upper()
            except transient:
                # A single poll HTTP call stalled or dropped; the customisation
                # may still be running, so keep polling until the wall-clock
                # deadline rather than aborting a healthy long-running job.
                status = None
            if status is not None and status not in _TAILOR_ACTIVE:
                return status
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(
                    f"Data Tailor customisation {cust} did not finish "
                    f"within {TAILOR_POLL_TIMEOUT_S:.0f}s (last status "
                    f"{status!r})."
                )
            time.sleep(min(delay, deadline - now))
            delay = min(delay * TAILOR_POLL_BACKOFF, TAILOR_POLL_MAX_S)

    @staticmethod
    def _logfile_tail(cust, limit: int = 1500) -> str:
        """Return the tail of a customisation's server log, if any.

        Args:
            cust: The `eumdac` `Customisation` handle.
            limit: Maximum number of trailing characters to return.

        Returns:
            str: The last `limit` characters of `cust.logfile`, or a
                placeholder when no log is available.
        """
        try:
            log = cust.logfile
        except Exception:  # noqa: BLE001 - a missing log must not mask the failure
            log = None
        if not log:
            return "(no customisation log available)"
        return str(log)[-limit:]
