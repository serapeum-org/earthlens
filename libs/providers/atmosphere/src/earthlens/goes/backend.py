"""Backend that fetches raw NOAA GOES-R ABI NetCDF granules from S3.

`GOES(AbstractDataSource)` pulls GOES-R Advanced Baseline Imager (ABI)
imagery from the public, anonymous `noaa-goes*` AWS buckets and returns
the `list[Path]` of raw **NetCDF** granules whose scan-start time falls
in the requested window. It does **not** decode them — reading /
reprojecting the geostationary NetCDF is pyramids' (or `satpy`'s) job
downstream, so this module never imports `xarray` / `netCDF4` / `goes2go`.

The request is three-axis:

* **satellite** — `satellite="east"` / `"west"` resolve to the current
  operational bucket (`noaa-goes19` / `noaa-goes18`); `"16"` / `"18"` /
  `"19"` name a bucket explicitly. The role→bucket map lives in the
  catalog because East/West rotate as new GOES satellites commission.
* **product** — `dataset="abi-l2-mcmip"` (an ABI product family) picks a
  `product_group` (`ABI-L2-MCMIP`).
* **domain** — `domain="C"` CONUS (5-min) / `"F"` Full Disk (10-min) /
  `"M1"` / `"M2"` Mesoscale (1-min). The two mesoscale subsectors share
  one `...M` S3 prefix and are split by a filename token.

`_search` crosses every hour in `[start, end]` with the resolved
satellite / product / domain, lists the `<Product>/<YYYY>/<DDD>/<HH>/`
prefix, keeps granules whose `_s<scan-start>` parses into the window
(and, for the band-split products, whose channel is requested), and
`_fetch` downloads them whole (unsigned boto3, atomic `.part`). A whole
granule is served — there is no server-side spatial / band subset; a
spatial crop / band extraction is a downstream pyramids read.

GOES rides the shipped `[s3]` extra (unsigned `boto3`) imported lazily,
so the package imports — and `GOES(...)` constructs — without it (a
friendly `ImportError` naming `earthlens[s3]` surfaces at `download()`).
GOES data is US Government public domain: no licence gate.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    RemoteProduct,
    TemporalExtent,
    end_is_date_only,
    expand_bare_date_end,
    to_datetime,
)
from earthlens.goes._helpers import (
    BUCKET_REGION,
    download_key,
    list_prefix_keys,
    parse_scan_start,
    unsigned_s3_client,
)
from earthlens.goes.catalog import Catalog, GOESDomain, GOESProduct

#: `_search` lists one S3 prefix per hour of the window; beyond this many
#: hours (30 days) it logs a warning so a wide window is not a silent
#: many-round-trip surprise before the download even starts.
WIDE_WINDOW_HOURS = 720

#: Extracts the ABI channel from a band-split granule name's `-M<mode>C<nn>_`
#: field (e.g. `OR_ABI-L1b-RadC-M6C02_G19_…` → `02`). Anchored on the mode
#: digit so `C02` is never confused with `C12` / `C20`.
_CHANNEL_IN_NAME = re.compile(r"-M\dC(\d{2})_")


def enumerate_hours(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    """Enumerate the hour buckets spanning `[start, end]` inclusive.

    ABI granules are stored under their scan-start hour
    (`<Product>/<YYYY>/<DDD>/<HH>/`), so listing every hour from `start`
    (floored) to `end` covers the window; the exact-minute filter is
    applied later against each granule's scan-start.

    Args:
        start: Inclusive window start.
        end: Inclusive window end.

    Returns:
        list[datetime.datetime]: One naive-UTC datetime per hour,
            ascending.

    Raises:
        ValueError: If `start` is later than `end`.

    Examples:
        - Three hours across a window:
            ```python
            >>> import datetime as dt
            >>> from earthlens.goes.backend import enumerate_hours
            >>> hrs = enumerate_hours(
            ...     dt.datetime(2026, 7, 3, 12, 5), dt.datetime(2026, 7, 3, 14, 1)
            ... )
            >>> [h.hour for h in hrs]
            [12, 13, 14]

            ```
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}.")
    cursor = start.replace(minute=0, second=0, microsecond=0)
    out: list[dt.datetime] = []
    while cursor <= end:
        out.append(cursor)
        cursor += dt.timedelta(hours=1)
    return out


def normalize_channel(token: str) -> str:
    """Normalise a channel selector to its ABI `C<nn>` token.

    Accepts `"C02"`, `"c2"`, `"2"`, `"02"`, or a `CMI_C02` form and
    returns the canonical `"C02"`.

    Args:
        token: A user channel selector.

    Returns:
        str: The canonical `C<nn>` token, or the upper-cased input when it
            carries no channel number (so an unknown selector matches
            nothing rather than every granule).

    Examples:
        - Several spellings collapse to the canonical token:
            ```python
            >>> from earthlens.goes.backend import normalize_channel
            >>> [normalize_channel(t) for t in ("C2", "2", "cmi_c02")]
            ['C02', 'C02', 'C02']

            ```
    """
    match = re.search(r"(\d{1,2})", token)
    if match is None:
        return token.upper()
    return f"C{int(match.group(1)):02d}"


class GOES(AbstractDataSource):
    """NOAA GOES-R ABI backend (raw geostationary NetCDF granules).

    Wraps the unsigned `noaa-goes*` buckets so a user pulls a
    satellite / product / domain / time window of raw ABI NetCDF
    through the same `download()` shape every other file-writing
    earthlens backend uses. `download()` returns the `list[Path]` of the
    fetched granules; decoding them is a downstream pyramids / `satpy`
    step.

    Attributes:
        OUTPUT_KIND: `"raster"` — the on-disk artefact is a gridded
            (geostationary) NetCDF. `aggregate=` is rejected: the
            granules are raw and undecoded, so a time-window reduce would
            need the downstream reader.
    """

    OUTPUT_KIND = "raster"

    AGGREGATE_REFUSAL_REASON = "the granules are raw, undecoded geostationary NetCDF; read and reduce them downstream with pyramids / satpy"

    def __init__(
        self,
        start: str,
        end: str,
        lat_lim: list[float],
        lon_lim: list[float],
        dataset: str = "abi-l2-mcmip",
        variables: list[str] | str | None = None,
        temporal_resolution: str = "raw",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        satellite: str = "east",
        domain: str | None = None,
        region: str = BUCKET_REGION,
        catalog: Catalog | None = None,
    ):
        """Initialise a GOES ABI backend instance.

        Args:
            start: Inclusive start of the scan-time window. A string
                (parsed with `fmt`, falling back to ISO-8601) or a
                `datetime` / `date`. GOES is sub-hourly, so pass a time
                (e.g. `"2026-07-03 12:00"` with `fmt="%Y-%m-%d %H:%M"`)
                for a tight window; a bare date spans the whole UTC day.
            end: Inclusive end of the scan-time window, same accepted
                forms as `start`.
            lat_lim: `[lat_min, lat_max]` in degrees. Captured for
                context only — S3 serves whole granules, so there is no
                server-side spatial subset (a crop is a downstream read).
            lon_lim: `[lon_min, lon_max]` in degrees.
            dataset: An ABI product-family key from the catalog
                (`"abi-l2-mcmip"`, `"abi-l1b-rad"`, …).
            variables: For a band-split product (`abi-l1b-rad`,
                `abi-l2-cmip`), the ABI channels to fetch (`["C02",
                "C13"]`) — selects which granule files. For a combined
                product (`abi-l2-mcmip`) it is informational (the whole
                multi-band granule is fetched). `None` fetches every
                granule.
            temporal_resolution: Advisory label (GOES cadence is fixed by
                the domain).
            path: Output directory for the fetched NetCDF granules.
            fmt: `strptime` format tried first for string `start` / `end`.
            satellite: `"east"` / `"west"` (current operational role) or
                `"16"` / `"18"` / `"19"` (explicit satellite / archive).
            domain: `"C"` / `"F"` / `"M1"` / `"M2"`. `None` uses the
                product's `default_domain`.
            region: AWS region of the buckets.
            catalog: Optional pre-built :class:`Catalog` (tests inject
                one); defaults to the bundled catalog.

        Raises:
            ValueError: If `dataset` / `satellite` is unknown, or `domain`
                is not published by the product.
        """
        self._catalog = catalog if catalog is not None else Catalog()
        self._satellite = satellite
        self._bucket = self._catalog.bucket_for(satellite)
        self._product: GOESProduct = self._catalog.get_product(dataset)
        domain_key = domain if domain is not None else self._product.default_domain
        if domain_key not in self._product.domains:
            raise ValueError(
                f"domain {domain_key!r} is not published by product "
                f"{dataset!r}; it carries {self._product.domains}."
            )
        self._domain_key = domain_key
        self._domain: GOESDomain = self._catalog.get_domain(domain_key)
        self._region = region
        self._show_progress = True

        if isinstance(variables, str):
            variables = [variables]
        self._channels = (
            [normalize_channel(v) for v in variables]
            if self._product.band_split and variables
            else []
        )

        super().__init__(
            start=start,
            end=end,
            variables=variables if variables is not None else [],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    # -- abstract hooks ------------------------------------------------

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the scan-time window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive window start.
            end: Inclusive window end.
            temporal_resolution: Advisory cadence label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model whose `dates` are the hour
                buckets the search enumerates.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = expand_bare_date_end(
            to_datetime(end, fmt), date_only=end_is_date_only(end)
        )
        hours = enumerate_hours(start_dt, end_dt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="h",
            dates=pd.DatetimeIndex(hours),
        )

    # -- search / fetch ------------------------------------------------

    def _client(self) -> Any:
        """Build (once) the unsigned `boto3` client for the public buckets.

        Returns:
            An anonymous `boto3` S3 client, cached on the instance.

        Raises:
            ImportError: When `boto3` is not installed (names
                `earthlens[s3]`).
        """
        client = self.__dict__.get("_s3_client")
        if client is None:
            client = unsigned_s3_client(self._region)
            self.__dict__["_s3_client"] = client
        return client

    def _prefix(self) -> str:
        """Return the product/domain S3 key prefix (`ABI-L2-MCMIPC`)."""
        return f"{self._product.product_group}{self._domain.prefix_suffix}"

    def _keep_key(self, key: str) -> bool:
        """Return whether a listed key matches the domain subsector + channels.

        Two filters apply on the filename (both no-ops when not
        configured):

        * **Mesoscale subsector** — the `M1` / `M2` domains share one
          `...M` prefix, so keep only keys whose basename carries the
          product's `...M1` / `...M2` token.
        * **Channel** — for a band-split product with `variables=`, parse
          the ABI channel out of the filename's `-M<mode>C<nn>_` field and
          keep only keys whose channel was requested.

        Args:
            key: A listed S3 key.

        Returns:
            bool: `True` when the key passes every active filter.
        """
        name = key.rsplit("/", 1)[-1]
        if self._domain.subsector:
            token = f"{self._product.product_group}{self._domain.subsector}"
            if token not in name:
                return False
        if self._channels:
            match = _CHANNEL_IN_NAME.search(name)
            if match is None or f"C{match.group(1)}" not in self._channels:
                return False
        return True

    def _search(self) -> list[RemoteProduct]:
        """Enumerate the in-window granule keys for the request.

        Lists each hour prefix across `[start, end]` for the resolved
        satellite / product / domain, applies the subsector / channel
        filename filter, and keeps granules whose parsed scan-start lands
        in the window. An hour prefix that lists nothing is logged (not
        silently skipped).

        Returns:
            list[RemoteProduct]: One product per matching granule, each
                carrying `href` (the S3 key) and `bucket` / `product` /
                `domain` / `scan_start` metadata.
        """
        prefix = self._prefix()
        start_dt = self.time.start_date
        end_dt = self.time.end_date
        n_hours = len(self.time.dates)
        if n_hours > WIDE_WINDOW_HOURS:
            logger.warning(
                f"goes: the request spans {n_hours} hours — _search issues one "
                f"S3 LIST per hour (~{n_hours} round-trips) before any download. "
                "Narrow the window if this is unintended."
            )
        products: list[RemoteProduct] = []
        empty_hours = 0
        for hour in self.time.dates:
            hour_prefix = f"{prefix}/{hour:%Y}/{hour:%j}/{hour:%H}/"
            keys = list_prefix_keys(self._client(), self._bucket, hour_prefix)
            if not keys:
                # Per-hour at DEBUG so a wide/backfill window does not flood the
                # WARNING channel; a single summary warning fires after the loop.
                empty_hours += 1
                logger.debug(
                    f"goes: no granules under s3://{self._bucket}/{hour_prefix}"
                )
                continue
            for key in keys:
                if not self._keep_key(key):
                    continue
                scan_start = parse_scan_start(key)
                if scan_start is None or not (start_dt <= scan_start <= end_dt):
                    continue
                products.append(
                    RemoteProduct(
                        id=key.rsplit("/", 1)[-1],
                        href=key,
                        metadata={
                            "bucket": self._bucket,
                            "product": self._product.product,
                            "domain": self._domain_key,
                            "scan_start": scan_start,
                        },
                    )
                )
        if empty_hours:
            logger.warning(
                f"goes: {empty_hours} of {n_hours} hour prefix(es) had no granules "
                f"for {prefix} on {self._bucket} (not yet published or an outage)."
            )
        logger.info(
            f"goes: planned {len(products)} granule(s) for {prefix} on {self._bucket}"
        )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download each granule whole to `self.root_dir` (unsigned boto3).

        Args:
            products: The granules from :meth:`_search`.

        Returns:
            list[Path]: The written NetCDF paths, in order.
        """
        client = self._client()
        out: list[Path] = []
        for product in tqdm(
            products, disable=not self._show_progress, desc="goes", unit="granule"
        ):
            dest = self.root_dir / product.id
            download_key(client, product.metadata["bucket"], product.href, dest)
            out.append(dest)
        return out

    def download(
        self,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Fetch the in-window ABI granules and return the written paths.

        Runs the cheap :meth:`_search` (key enumeration) then
        :meth:`_fetch` (whole-granule download). The granules are raw,
        undecoded NetCDF — reading / cropping / band extraction is a
        downstream pyramids (or `satpy`) step.

        Args:
            progress_bar: Show a per-granule progress bar. Defaults to
                `True`.

        Returns:
            list[Path]: The written granule paths, or an empty list when
                nothing in the window was available.
        """
        self._show_progress = progress_bar
        return self._api_via_search_fetch()
