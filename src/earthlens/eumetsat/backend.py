"""Backend that fetches EUMETSAT Data Store products via `eumdac`.

`EUMETSAT(AbstractDataSource)` accepts the same constructor surface as
the other earthlens backends — `start`, `end`, `variables`, `lat_lim`,
`lon_lim`, `temporal_resolution`, `path` — plus a few backend-specific
kwargs for the OAuth2 consumer key / secret and group disambiguation.
Each `(collection_key, [selector, ...])` pair in the `variables` mapping
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

The MVP fetches **whole native products** to disk (`G4`): `eumdac`'s
`Product.open()` stream copied to a file. Server-side bbox / band
subsetting and reprojection are the deferred Data Tailor path (`H4`); the
selectors in `variables` are informational for a whole-product fetch
(`G2`), and `bbox` is a search filter (which products intersect), not a
pixel crop. `download(aggregate=...)` therefore raises
`NotImplementedError` pointing at Data Tailor.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.eumetsat._helpers import eumdac_bbox, safe_product_filename
from earthlens.eumetsat.auth import EumetsatAuth, EumetsatCredentials
from earthlens.eumetsat.catalog import Catalog, DataStoreGroup, EumetsatCollection

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig


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

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        group: DataStoreGroup | str | None = None,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        credentials_file: Path | str | None = None,
    ):
        """Initialise an EUMETSAT backend instance.

        Resolves every requested collection key against the catalog
        **before** calling the parent constructor, so the per-instance
        `OUTPUT_KIND` is set from the resolved row(s). The parent
        `__init__` runs `_initialize` first (token mint), so the
        resolution cannot live there.

        Args:
            start: Inclusive start date as a string (parsed with `fmt`).
            end: Inclusive end date as a string.
            variables: Mapping from curated collection key to a list of
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
            ValueError: When `variables` is empty, a collection key is
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
        self._collections: list[EumetsatCollection] = self._resolve_collections(
            variables
        )
        self.OUTPUT_KIND = self._unify_output_kind(self._collections)

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

    def _resolve_collections(
        self, variables: dict[str, list[str]]
    ) -> list[EumetsatCollection]:
        """Resolve every requested collection key to a catalog row.

        Args:
            variables: The `{collection_key: [selector, ...]}` request.

        Returns:
            list[EumetsatCollection]: One row per key, in request order.

        Raises:
            ValueError: When `variables` is empty or a key is unknown
                (the catalog's did-you-mean is surfaced in the message).
        """
        if not variables:
            raise ValueError(
                "EUMETSAT requires a non-empty `variables` mapping of "
                "{collection_key: [selector, ...]}."
            )
        return [self._catalog.resolve(key, group=self._group) for key in variables]

    @staticmethod
    def _unify_output_kind(collections: list[EumetsatCollection]) -> OutputKind:
        """Return the single `output_kind` shared by every requested row.

        A backend instance carries exactly one `OUTPUT_KIND`, so a
        request mixing (say) a raster and a vector collection is
        ambiguous and rejected here.

        Args:
            collections: The resolved collection rows.

        Returns:
            OutputKind: The shared `output_kind`.

        Raises:
            ValueError: When the rows do not all share one `output_kind`.
        """
        kinds = {col.output_kind for col in collections}
        if len(kinds) > 1:
            detail = ", ".join(
                f"{col.collection_id}={col.output_kind}" for col in collections
            )
            raise ValueError(
                "all collections in one EUMETSAT request must share one "
                f"output_kind; got mixed kinds ({detail}). Split the "
                "request into one call per output kind."
            )
        return kinds.pop()

    def _initialize(self):
        """Build the `EumetsatAuth` and run `configure()` (mint the token).

        Returns `None` — `eumdac` keeps the token on the `EumetsatAuth`
        instance, so the parent class binds no opaque `self.client`.

        Raises:
            AuthenticationError: When token minting fails.
        """
        creds = EumetsatCredentials(
            consumer_key=self._consumer_key,
            consumer_secret=self._consumer_secret,
            credentials_file=self._credentials_file,
        )
        self._auth = EumetsatAuth(creds)
        self._auth.configure()
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Validate and wrap the user bbox into a `SpatialExtent`.

        The Data Store search accepts a plain bounding box, so this is a
        thin wrapper over `SpatialExtent.from_pairs`. An earthlens extent
        constrains longitude to a single `[-180, 180]` range with
        `west <= east`, so it cannot represent an antimeridian-crossing
        box and `_search` issues exactly one search bbox.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

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
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        freq_map = {"daily": "D", "monthly": "MS", "hourly": "h"}
        resolution = freq_map.get(temporal_resolution, "D")
        dates = pd.date_range(start_dt, end_dt, freq=resolution)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=resolution,
            dates=dates,
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def _search(self) -> list[RemoteProduct]:
        """Query the Data Store for products of every requested collection.

        One `Collection.search(bbox=, dtstart=, dtend=)` per resolved
        collection row, scoped to the request bbox and time window. The
        bbox is the `eumdac` `W,S,E,N` comma-string the OpenSearch
        endpoint expects. The `end` date is treated as **inclusive of its
        whole calendar day**: `dtend` is widened to `23:59:59.999999` of
        the end day so a same-day request (`start == end`) covers the
        day's products instead of collapsing to the midnight instant.
        Each returned `eumdac` product becomes one `RemoteProduct` whose
        `metadata` carries the raw product handle and its collection row,
        so `_fetch` can stream without re-querying.

        Returns:
            list[RemoteProduct]: One product per matching Data Store
                product, across every requested collection. An empty list
                (no products in the window) short-circuits the fetch.

        Raises:
            ImportError: When the `[eumetsat]` extra (`eumdac`) is not
                installed.
        """
        try:
            import eumdac  # noqa: F401 - imported to surface a friendly error
        except ImportError as exc:
            raise ImportError(
                "the EUMETSAT backend needs `eumdac`; install "
                "`pip install earthlens[eumetsat]`."
            ) from exc

        assert self._auth is not None  # set by _initialize
        self._auth.configure()
        store = self._auth.datastore()
        # A `SpatialExtent` constrains longitude to a single `[-180, 180]`
        # range with `west <= east`, so it cannot represent an
        # antimeridian-crossing box — a single search bbox always suffices.
        bbox = eumdac_bbox(
            self.space.west, self.space.south, self.space.east, self.space.north
        )
        # `end_date` parses to midnight, so a same-day request (start == end)
        # would otherwise collapse to the zero-width instant 00:00:00 and match
        # (almost) no products. Extend the end bound to the end of its calendar
        # day so an inclusive `end` covers the whole day's products.
        dtstart = self.time.start_date
        dtend = self.time.end_date.replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        products: list[RemoteProduct] = []
        for col in self._collections:
            collection = store.get_collection(col.collection_id)
            for product in collection.search(
                bbox=bbox,
                dtstart=dtstart,
                dtend=dtend,
            ):
                products.append(
                    RemoteProduct(
                        id=str(product),
                        metadata={"product": product, "collection": col},
                    )
                )
        return products

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
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Search the Data Store, fetch native products, return their paths.

        Composes `_search` and `_fetch` to pull every product matching
        the request bbox + window to `self.root_dir`.

        `aggregate=` is the server-side subset / reproject path, which on
        the EUMETSAT Data Store is **Data Tailor** (`H4`) — not part of
        the MVP. A non-`None` `aggregate` therefore raises
        `NotImplementedError` naming Data Tailor; native NetCDF products
        can be read / reduced client-side with pyramids after download.

        Args:
            progress_bar: Reserved for parity with the other backends;
                `eumdac`'s streaming download has no built-in bar.
            aggregate: Optional
                `earthlens.aggregate.AggregationConfig`. Rejected — see
                above.

        Returns:
            list[Path]: The fetched product paths.

        Raises:
            NotImplementedError: When `aggregate` is not `None` (the Data
                Tailor path, `H4`, is deferred).
        """
        if aggregate is not None:
            raise NotImplementedError(
                "EUMETSAT aggregate=/subset is the Data Tailor path (H4); "
                "it is not part of the MVP. Download native products "
                "without aggregate= and reduce NetCDF products client-side "
                "with pyramids."
            )
        self._show_progress = progress_bar
        return self._api_via_search_fetch()
