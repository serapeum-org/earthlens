"""Backend that assembles NEXRAD Level-II radar volumes from the chunk feed.

`Radar(AbstractDataSource)` fetches WSR-88D Level-II volumes from the
**unsigned** `unidata-nexrad-level2-chunks` AWS bucket. That bucket is a
near-real-time rolling buffer: each volume is delivered as ordered
chunks — one `S` (start, carrying the `AR2V…` volume header), many `I`
(intermediate), and a final `E` (end) — under
`{STATION}/{VOLUME}/{YYYYMMDD}-{HHMMSS}-{CHUNK:03d}-{TYPE}`. Concatenating
a volume's chunks in chunk order reconstructs a valid `.ar2v` Level-II
file (verified: the assembled stream starts with `AR2V0006`).

The request is `variables = {station_id: [...]}` (e.g.
`{"KTLX": []}`); the list value is advisory — a Level-II volume carries
every moment, so the whole volume is fetched. `start` / `end` filter
volumes by their scan start time. The result is a `GeoDataFrame`
inventory of the assembled volumes (one row per volume: station, scan
time, chunk count, local path, station-point geometry), so
`OUTPUT_KIND = "vector"`.

**Real-time only.** The chunks bucket holds roughly the last hour or
two of volumes, not a historical archive, so a request for an old date
returns nothing. (The archival `noaa-nexrad-level2` bucket denies
anonymous listing, so it is not used here.) Reading / gridding the
assembled volumes (via `pyart`) is a downstream follow-on; this backend
only fetches and inventories them.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.radar.catalog import Catalog, Station

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

#: The unsigned AWS bucket holding the real-time Level-II chunk feed.
BUCKET = "unidata-nexrad-level2-chunks"


def _s3_client(region: str) -> Any:
    """Build an unsigned `boto3` S3 client for the public chunk bucket.

    Args:
        region: AWS region of the bucket (`"us-east-1"`).

    Returns:
        An anonymous `boto3` S3 client.

    Raises:
        ImportError: When `boto3` is not installed (names `earthlens[radar]`).
    """
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config
    except ImportError as exc:
        raise ImportError(
            "the NEXRAD radar backend needs `boto3`; install "
            "`pip install earthlens[radar]`."
        ) from exc
    return boto3.client(
        "s3", region_name=region, config=Config(signature_version=UNSIGNED)
    )


def _volume_start(chunk_key: str) -> dt.datetime:
    """Parse a volume's scan start time from one of its chunk keys.

    Args:
        chunk_key: e.g. `"KTLX/871/20260524-005505-001-S"`.

    Returns:
        datetime.datetime: The volume start time (naive UTC).

    Examples:
        - Parse the scan start from a volume's `S` (start) chunk key:
            ```python
            >>> from earthlens.radar.backend import _volume_start
            >>> _volume_start("KTLX/871/20260524-005505-001-S").isoformat()
            '2026-05-24T00:55:05'

            ```
        - The leading station / volume path segments are ignored:
            ```python
            >>> from earthlens.radar.backend import _volume_start
            >>> _volume_start("KFWS/12/20240601-120000-003-E").hour
            12

            ```
    """
    stamp = chunk_key.rsplit("/", 1)[-1]  # 20260524-005505-001-S
    date_part, time_part = stamp.split("-")[:2]
    return dt.datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")


class Radar(AbstractDataSource):
    """NEXRAD Level-II radar backend (real-time chunk feed).

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a `GeoDataFrame`
            inventory of assembled volumes, not a gridded array, so the
            facade rejects `aggregate=` (raw radar volumes are not
            pyramids-reducible).
    """

    OUTPUT_KIND: OutputKind = "vector"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "raw",
        path: Path | str = "",
        fmt: str = "%Y-%m-%dT%H:%M:%S",
        *,
        region: str = "us-east-1",
        catalog: Catalog | None = None,
    ):
        """Initialise a radar backend instance.

        Args:
            start: Inclusive start of the scan-time window (parsed with
                `fmt`).
            end: Inclusive end of the scan-time window.
            variables: Mapping from WSR-88D site id to an (advisory)
                moment list, e.g. `{"KTLX": ["reflectivity"]}`.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label (radar volumes are
                `"raw"`).
            path: Output directory for the assembled `.ar2v` files.
            fmt: `strptime` format for `start` / `end`. Defaults to an
                ISO datetime (`"%Y-%m-%dT%H:%M:%S"`) since the feed is
                sub-hourly real-time.
            region: AWS region of the chunk bucket.
            catalog: Optional pre-built :class:`Catalog` (tests
                inject a faked one).

        Raises:
            ValueError: When `variables` is empty.
        """
        if not variables:
            raise ValueError(
                "Radar requires a non-empty `variables` mapping of {station_id: [...]}."
            )
        self._region = region
        self._catalog = catalog if catalog is not None else Catalog()
        self._stations: list[tuple[str, Station | None]] = [
            (site_id, self._catalog.datasets.get(site_id)) for site_id in variables
        ]
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

    def _initialize(self):
        """No-op auth hook — the chunk bucket is anonymous. Returns `None`."""
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a :class:`SpatialExtent`."""
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the scan-time window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive window start.
            end: Inclusive window end.
            temporal_resolution: Advisory cadence label.
            fmt: `strptime` format applied to `start` / `end`.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.

        Raises:
            ValueError: If `start` parses later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="raw",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _api(self) -> list[Path]:
        """Return the assembled `.ar2v` paths (the raw "paths only" entry).

        `download` is the public entry point — it returns the typed
        `GeoDataFrame` inventory. `_api` exists to satisfy the
        `AbstractDataSource` contract and exposes just the written paths
        for callers (or the C3 `_api_via_search_fetch` composition) that
        want them without the inventory frame.
        """
        return self._api_via_search_fetch()

    def _window(self) -> tuple[dt.datetime, dt.datetime]:
        """Return the inclusive scan-time window, extending `end` to its day end."""
        end = self.time.end_date
        # A date-only end (midnight) would exclude the whole day's volumes;
        # extend it to the end of that day so a same-day request matches.
        if end.hour == end.minute == end.second == 0:
            end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        return self.time.start_date, end

    def _search(self) -> list[RemoteProduct]:
        """List each station's volumes in the window; one product per volume.

        For every requested site, lists the volume prefixes under
        `{STATION}/`, then lists each volume's chunks, parses the
        volume scan start time, and keeps the volumes whose start time
        falls in the request window. The ordered chunk keys ride on the
        product metadata so :meth:`_fetch` needs no re-listing.

        Returns:
            list[RemoteProduct]: One product per in-window volume, each
                carrying `station`, `volume`, `start_time`, ordered
                `chunk_keys`, and the `station` row (for geometry).
        """
        client = _s3_client(self._region)
        start, end = self._window()
        products: list[RemoteProduct] = []
        for site_id, station in self._stations:
            volume_prefixes = self._list_prefixes(client, f"{site_id}/")
            for vp in volume_prefixes:
                # Read just the first chunk key (the `S` chunk carries the scan
                # start) to window-filter cheaply; only list a volume's full
                # chunk set once it is known to be in range (avoids the N+1
                # full-listing of every volume).
                first = self._first_key(client, vp)
                if first is None:
                    continue
                scan = _volume_start(first)
                if not (start <= scan <= end):
                    continue
                chunk_keys = sorted(self._list_keys(client, vp))
                if not chunk_keys:
                    continue
                volume = vp.rstrip("/").rsplit("/", 1)[-1]
                products.append(
                    RemoteProduct(
                        id=f"{site_id}.{volume}",
                        metadata={
                            "station_id": site_id,
                            "station": station,
                            "volume": volume,
                            "start_time": scan,
                            "chunk_keys": chunk_keys,
                        },
                    )
                )
        return products

    @staticmethod
    def _list_prefixes(client: Any, prefix: str) -> list[str]:
        """Return the immediate sub-prefixes under `prefix` (paginated)."""
        out: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": BUCKET, "Prefix": prefix, "Delimiter": "/"}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            out.extend(c["Prefix"] for c in resp.get("CommonPrefixes", []))
            token = resp.get("NextContinuationToken")
            if not resp.get("IsTruncated"):
                break
        return out

    @staticmethod
    def _first_key(client: Any, prefix: str) -> str | None:
        """Return the lexicographically first object key under `prefix`.

        Used to read a volume's `S` (start) chunk — which carries the
        scan-start timestamp — without listing the whole volume.

        Args:
            client: The S3 client.
            prefix: A volume prefix (`"KTLX/871/"`).

        Returns:
            str | None: The first key, or `None` if the prefix is empty.
        """
        resp = client.list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=1)
        contents = resp.get("Contents", [])
        return contents[0]["Key"] if contents else None

    @staticmethod
    def _list_keys(client: Any, prefix: str) -> list[str]:
        """Return all object keys under `prefix` (paginated)."""
        out: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": BUCKET, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            out.extend(o["Key"] for o in resp.get("Contents", []))
            token = resp.get("NextContinuationToken")
            if not resp.get("IsTruncated"):
                break
        return out

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Assemble each volume's chunks into one `.ar2v` file.

        Per volume: download the ordered chunks and concatenate them
        (atomically, via a `.part` rename) into a single Level-II file.
        A volume whose download fails is logged and skipped so one bad
        volume does not lose the others (mirrors the FDSN/NWP policy).

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: One assembled `.ar2v` path per successfully
                fetched volume, in product order.
        """
        return [path for _, path in self._fetch_pairs(products)]

    def _fetch_pairs(
        self, products: list[RemoteProduct]
    ) -> list[tuple[RemoteProduct, Path]]:
        """Assemble each volume, returning `(product, path)` pairs.

        Like :meth:`_fetch` but keeps each path paired with the product
        it came from, so the inventory needs no filename re-matching. A
        volume whose download fails is logged and skipped. The loop shows
        a `tqdm` bar unless `download(progress_bar=False)` disabled it.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[tuple[RemoteProduct, Path]]: One pair per successfully
                assembled volume, in product order.
        """
        client = _s3_client(self._region)
        out: list[tuple[RemoteProduct, Path]] = []
        for product in tqdm(
            products, disable=not getattr(self, "_show_progress", True), desc="radar"
        ):
            try:
                out.append((product, self._assemble(client, product)))
            except Exception as exc:  # noqa: BLE001 - skip the bad volume, keep going
                logger.warning(
                    f"radar: skipping volume {product.id} — assembly failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        return out

    def _assemble(self, client: Any, product: RemoteProduct) -> Path:
        """Download + concatenate one volume's chunks into a `.ar2v` file."""
        meta = product.metadata
        target = self.root_dir / (
            f"{meta['station_id']}_{meta['start_time']:%Y%m%d_%H%M%S}.ar2v"
        )
        tmp = target.with_name(target.name + ".part")
        try:
            with open(tmp, "wb") as handle:
                for key in meta["chunk_keys"]:
                    handle.write(
                        client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
                    )
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ):
        """Assemble the in-window volumes and return a `GeoDataFrame` inventory.

        Args:
            progress_bar: Unused (kept for interface parity).
            aggregate: Must be `None` — raw radar volumes are not
                pyramids-reducible. The facade already rejects a non-`None`
                `aggregate=` for a `"vector"` backend before reaching here.

        Returns:
            geopandas.GeoDataFrame: One row per assembled volume —
                `station_id`, `volume`, `scan_time`, `n_chunks`, `path`,
                and a station-point `geometry` (`None` for sites absent
                from the catalog). Empty (with the right columns) when no
                volume falls in the window.

                Intentionally a `GeoDataFrame` inventory (one row per
                downloaded volume + its on-disk `path`), not the
                `FeatureCollection` the event/footprint `"vector"`
                backends (FDSN, FIRMS, GDACS) return — the rows index
                bulky files rather than describe point/polygon features.
                The base `download` contract lists radar as this
                documented exception.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "Radar.download(aggregate=...) is not supported — raw Level-II "
                "volumes are not griddable by the pyramids reducer."
            )
        self._show_progress = progress_bar
        products = self._search()
        pairs = self._fetch_pairs(products)
        return self._inventory(pairs)

    @staticmethod
    def _inventory(pairs: list[tuple[RemoteProduct, Path]]):
        """Build the GeoDataFrame inventory from `(product, path)` pairs.

        Each path is already paired with the product it came from (no
        filename re-matching), so the metadata attaches unambiguously.
        """
        import geopandas as gpd
        from shapely.geometry import Point

        rows = []
        geoms = []
        for product, path in pairs:
            meta = product.metadata
            station: Station | None = meta.get("station")
            rows.append(
                {
                    "station_id": meta.get("station_id"),
                    "volume": meta.get("volume"),
                    "scan_time": meta.get("start_time"),
                    "n_chunks": len(meta.get("chunk_keys", [])),
                    "path": str(path),
                }
            )
            geoms.append(
                Point(station.longitude, station.latitude) if station else None
            )
        return gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
