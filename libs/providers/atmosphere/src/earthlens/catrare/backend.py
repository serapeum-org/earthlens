"""Backend that fetches DWD CatRaRE heavy-rainfall events by date and bbox.

`CatRaRE(AbstractDataSource)` downloads one CatRaRE FileGDB (`.gdb.zip`) from
DWD's open-data host (`opendata.dwd.de`, CC-BY-4.0 / GeoNutzV, no credentials),
reads one of its layers with pyramids — the `EventZones` footprint polygons
(default) or the `RRmaxPoints` maximum-rainfall points — reprojects it from the
DWD RADOLAN grid (which the file does not carry) to EPSG:4326, filters the
events by date window and/or bbox, and returns them carrying the event
attributes (area, duration, severity) as a
:class:`~pyramids.feature.collection.FeatureCollection` (or, with
`geometry=False`, a geometry-dropped :class:`pandas.DataFrame`).

Two threshold selections are served: `T5` (events with a return period of at
least 5 years) and `W3` (a severity-weighted selection). This is a static
2001-2025 archive, so a missing `start` / `end` is legal
(`REQUIRES_TIME_WINDOW = False`) and simply returns every event; when supplied,
the dates filter the events. It is a `vector` backend: the
:class:`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument.
The downloaded FileGDB is cached under `cache_dir`, so repeated requests reuse
it. The companion to the `radklim` grids.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd
import requests  # noqa: F401  # runtime seam so tests can monkeypatch this module's `requests`
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    to_datetime,
)
from earthlens.base.http import HttpClient
from earthlens.catrare import _helpers
from earthlens.catrare.catalog import Catalog
from earthlens.config import cache_dir as _shared_cache_dir

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

#: Zip local-file-header magic; a downloaded body must start with it.
_ZIP_MAGIC = b"PK\x03\x04"

#: Global sentinel bounds — CatRaRE covers Germany; a narrower bbox filters the
#: events, a global one keeps them all.
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


class CatRaRE(AbstractDataSource):
    """CatRaRE heavy-rainfall event-catalogue backend (vector event output).

    Downloads and caches one threshold's FileGDB, reads a layer with pyramids,
    reprojects from the DWD RADOLAN grid to EPSG:4326, filters by date and bbox,
    and returns the events carrying their attributes. Needs no credentials.
    `aggregate=` is rejected — the data is a vector event table, not a gridded
    raster.

    Attributes:
        OUTPUT_KIND: `"vector"` (a `FeatureCollection`) by default, or
            `"tabular"` (a `DataFrame`) when constructed with `geometry=False`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = (
        "CatRaRE returns individual heavy-rainfall events as vector features, "
        "not a gridded raster, so there is no meaningful gridded reduction. Call "
        "download() without aggregate= and post-process the returned "
        "FeatureCollection (a GeoDataFrame) directly"
    )

    #: CatRaRE is a static 2001-2025 archive; a missing `start` / `end` simply
    #: returns every event, so a time window is not required.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        threshold: str = "t5",
        geometry_layer: str = "zones",
        geometry: bool = True,
        cache_dir: Path | str | None = None,
        timeout: float = 120.0,
    ):
        """Initialise a CatRaRE backend instance.

        Args:
            start: Inclusive start of the date window to keep events for; `None`
                (the default) applies no lower bound.
            end: Inclusive end of the date window; `None` applies no upper bound.
            lat_lim: `[lat_min, lat_max]` bbox latitudes; `None` keeps every
                event. A narrower box filters the returned events.
            lon_lim: `[lon_min, lon_max]` bbox longitudes; `None` keeps all.
            temporal_resolution: Recorded as the resolution label only.
            path: Output directory for the written vector file. When omitted it
                falls back to the configured earthlens output directory
                (`set_output_dir()` / `EARTHLENS_DATA_DIR`); see
                `earthlens.config`.
            fmt: `strptime` format for `start` / `end`.
            threshold: Which CatRaRE selection to fetch — `"t5"` (return period
                >= 5 yr) or `"w3"` (severity-weighted).
            geometry_layer: Which FileGDB layer to read — `"zones"` (the event
                footprint polygons, default) or `"points"` (the maximum-rainfall
                points).
            geometry: `True` (default) returns a `FeatureCollection`; `False`
                returns a geometry-dropped `DataFrame` (`OUTPUT_KIND="tabular"`).
            cache_dir: Directory for the downloaded FileGDB. Defaults to
                `catrare/` under the shared earthlens cache directory
                (`set_cache_dir()` / `EARTHLENS_CACHE`), not under `path`.
            timeout: Per-request timeout in seconds for the download.

        Raises:
            ValueError: If `threshold` or `geometry_layer` is unknown.
        """
        self._catalog = Catalog()
        # Resolve/validate the threshold + geometry layer up front so a bad
        # request fails at construction, not mid-download.
        self._threshold = threshold
        self._dataset = self._catalog.get(threshold)
        self._geometry_layer = geometry_layer
        self._layer_name = self._catalog.layer_name(threshold, geometry_layer)
        self._geometry = geometry
        self._timeout = timeout
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None

        # Per-instance output shape: drop-geometry is a tabular DataFrame.
        self.OUTPUT_KIND = "vector" if geometry else "tabular"

        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=[self._dataset.threshold],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Record the (optional) date window used to filter events.

        The dates are not a download loop — they subset the events after the
        read — so they are parsed only when supplied and otherwise left `None`.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string `start` / `end`.

        Returns:
            TemporalExtent: Frozen model; `start_date` / `end_date` are `None`
                when the corresponding argument was `None`.
        """
        start_dt = to_datetime(start, fmt) if start else None
        end_dt = to_datetime(end, fmt) if end else None
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=pd.DatetimeIndex([]),
        )

    def _search(self) -> list[RemoteProduct]:
        """Pin the one product: the FileGDB URL and the layer + columns.

        Returns:
            list[RemoteProduct]: A single product whose `metadata` carries the
                resolved `layer` name and `event_columns`.
        """
        return [
            RemoteProduct(
                id=self._threshold,
                href=self._catalog.download_url(self._threshold),
                metadata={
                    "layer": self._layer_name,
                    "event_columns": self._catalog.event_columns,
                },
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Download + read the FileGDB layer and build the filtered collection.

        Widens the inherited `-> list[Path]` contract: a vector backend returns
        an in-memory :class:`FeatureCollection`. The download is cached under
        :attr:`_cache_root`, so a repeat request reuses the FileGDB.

        Args:
            products: The single-element list from :meth:`_search`.

        Returns:
            list[FeatureCollection]: One trimmed, filtered collection.

        Raises:
            requests.HTTPError: If the download returns a non-2xx status.
        """
        product = products[0]
        gdb_path = self._cached_gdb(product)
        layer = cast("str", product.metadata["layer"])
        event_columns = cast("list[str]", product.metadata["event_columns"])
        source = _helpers.read_events(gdb_path, layer, self._catalog.source_crs)
        trimmed = _helpers.build_feature_collection(source, event_columns)
        filtered = _helpers.filter_events(
            trimmed,
            self.space,
            self.time.start_date,
            self.time.end_date,
            self._catalog.date_columns,
        )
        return [filtered]

    @property
    def _cache_root(self) -> Path:
        """The directory holding the downloaded FileGDB zips.

        Defaults to `catrare/` under the shared earthlens cache directory
        (`set_cache_dir()` / `EARTHLENS_CACHE`); overridden
        by `cache_dir`. The download lands here, not directly under the output
        `root_dir`, so a `geometry=False` request — which skips the GeoPackage
        write — leaves the output directory free of result files.
        """
        return self._cache_dir or (_shared_cache_dir() / "catrare")

    def _cached_gdb(self, product: RemoteProduct) -> Path:
        """Return the local FileGDB zip path, downloading it on a cache miss.

        Args:
            product: The product from :meth:`_search` (carries the download URL
                in `href`).

        Returns:
            Path: The cached `.gdb.zip` on disk.

        Raises:
            requests.HTTPError: If the download returns a non-2xx status.
        """
        cache_dir = self._cache_root
        cache_dir.mkdir(parents=True, exist_ok=True)
        gdb_path = cache_dir / f"catrare_{self._threshold}.gdb.zip"
        if gdb_path.exists():
            with open(gdb_path, "rb") as handle:
                head = handle.read(4)
            if head == _ZIP_MAGIC:
                logger.info(f"CatRaRE: using cached {gdb_path.name}")
                return gdb_path
            logger.warning(
                f"CatRaRE: cached {gdb_path.name} is not a valid zip "
                "(empty / truncated / foreign); re-downloading."
            )
        url = cast("str", product.href)
        logger.info(f"CatRaRE: downloading the {self._threshold!r} FileGDB")
        HttpClient(timeout=self._timeout).download(
            url, gdb_path, expect_magic=_ZIP_MAGIC, progress=False
        )
        return gdb_path

    def download(
        self,
        progress_bar: bool = True,
    ) -> FeatureCollection | pd.DataFrame:
        """Fetch the selection and return the heavy-rainfall events.

        Issues the (cached) download, reads / reprojects / filters the FileGDB
        layer, writes the result to one vector file under `path` (when
        `geometry=True`), and returns the in-memory collection — or a
        geometry-dropped `DataFrame` when built with `geometry=False`.

        Args:
            progress_bar: Accepted for signature parity; one file is fetched, so
                this is a no-op.

        Returns:
            A :class:`~pyramids.feature.collection.FeatureCollection` (also
            written to `root_dir`) or, when `geometry=False`, a
            :class:`pandas.DataFrame` of the events and their attributes.

        Raises:
            requests.HTTPError: If the download returns a non-2xx status.
        """
        collections = self._api()
        collection = collections[0]
        logger.info(
            f"CatRaRE {self._threshold}/{self._geometry_layer}: "
            f"{len(collection)} event(s)."
        )
        logger.info(
            f"CatRaRE source: {self._catalog.attribution} "
            f"(licence {self._catalog.license})."
        )
        if not self._geometry:
            frame = pd.DataFrame(collection.drop(columns=collection.geometry.name))
            if frame.empty:
                logger.warning("CatRaRE: no event matched the request (empty table).")
            return frame
        if not len(collection):
            logger.warning("CatRaRE: no event matched the request; nothing written.")
            return collection
        out_path = self._write(collection)
        logger.info(f"CatRaRE: wrote {len(collection)} event(s) to {out_path}")
        return collection

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the collection to one GeoPackage under `root_dir`.

        The filename embeds the threshold, geometry layer, the date window,
        and a bbox tag so distinct requests do not overwrite one another.

        Args:
            collection: The collection to write.

        Returns:
            Path: The written file path.
        """
        date_tag = ""
        start, end = self.time.start_date, self.time.end_date
        if start is not None or end is not None:
            lo = start.strftime("%Y%m%d") if start is not None else "min"
            hi = end.strftime("%Y%m%d") if end is not None else "max"
            date_tag = f"_{lo}-{hi}"
        bbox_tag = ""
        if not _helpers._is_global(self.space):
            box = self.space
            bbox_tag = (
                f"_bbox{box.latitude_min:g}_{box.longitude_min:g}_"
                f"{box.latitude_max:g}_{box.longitude_max:g}"
            )
        stem = f"catrare_{self._threshold}_{self._geometry_layer}{date_tag}{bbox_tag}"
        out_path = self.root_dir / f"{stem}.gpkg"
        collection.to_file(str(out_path), driver="GPKG")
        return out_path
