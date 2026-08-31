"""Backend that fetches DWD RADKLIM / RADOLAN radar precipitation over HTTPS.

`RADKLIM(AbstractDataSource)` downloads DWD's gauge-adjusted radar precipitation
over Germany from DWD Open Data (`opendata.dwd.de`, anonymous HTTPS), in two
streams selected by the `dataset=` product:

* **reproc** (`radklim-rw` / `radklim-yw`) — the climatologically consistent
  reprocessing **RADKLIM**, served as one yearly NetCDF archive
  (`{CODE}2017.002_{year}_netcdf.tar.gz`) per year. The full 2001- archive; use
  it for statistics. A `[start, end]` window enumerates the yearly archives that
  cover it.
* **operational** (`radolan-rw` / `radolan-yw`) — the near-real-time **RADOLAN**
  stream, per-timestamp granules (`raa01-{code}_10000-{YYMMDDHHMM}-dwd---bin.
  {hdf5|bz2}`) on a rolling ~2-day retention window. `_search` reads the stream's
  Apache directory listing and keeps the granules whose scan time falls in the
  window. Less homogeneous than RADKLIM (no return periods).

The request is `variables = {product: [...]}` (usually via the facade's
`dataset=`); the list value is advisory — a RADOLAN grid carries a single
precipitation field, so the whole granule is fetched. `OUTPUT_KIND = "raster"`
and `download()` returns the `list[Path]` of raw granules. Reading them (NetCDF
/ HDF5 via pyramids) is a downstream follow-on; this backend never imports
`wradlib` / `xarray` / `netCDF4`, and the RADOLAN `.bz2` binary (an opt-in
format) is a wradlib/pyramids decode job.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import cast

import requests
from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    HttpClient,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
    safe_filename,
)
from earthlens.radklim._helpers import (
    FORMAT_EXTENSION,
    FORMAT_MAGIC,
    operational_dir_url,
    operational_granule_url,
    parse_listing,
    reproc_archive_url,
    timestamp_from_name,
)
from earthlens.radklim.catalog import Catalog, RadklimProduct

#: The fixed RADOLAN grid's Germany envelope as `(west, south, east, north)` in
#: degrees — a generous bounding box used only to reject a request that cannot
#: possibly intersect the composite. earthlens ships the native-grid granule and
#: does not subset, so a request just has to overlap Germany.
GERMANY_ENVELOPE: tuple[float, float, float, float] = (1.0, 45.0, 18.5, 57.5)


class RADKLIM(AbstractDataSource):
    """DWD RADKLIM / RADOLAN radar-precipitation backend (anonymous HTTPS).

    Wraps DWD Open Data so a user pulls a product / date window of gauge-adjusted
    radar precipitation through the same `download()` shape every other earthlens
    backend uses. Anonymous (no auth); Germany-only (the request must overlap the
    fixed RADOLAN grid); raw granules out (no decode).

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`. The facade rejects `aggregate=` (each
            granule is a native-grid file the reducer has no assembled time axis
            for; reduce the read stack yourself).
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]] | list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "raw",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        data_format: str | None = None,
        now: dt.datetime | None = None,
        catalog: Catalog | None = None,
        client: HttpClient | None = None,
    ):
        """Initialise a RADKLIM / RADOLAN backend instance.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`).
            end: Inclusive end of the date window.
            variables: The product(s) to fetch — a `{product: [variable, ...]}`
                mapping (the facade's `dataset=` builds a single-key one). The
                variable list is advisory (a RADOLAN grid carries one field). A
                bare list is also accepted and read as product keys.
            lat_lim: `[lat_min, lat_max]` in degrees; must overlap Germany.
            lon_lim: `[lon_min, lon_max]` in degrees; must overlap Germany.
            temporal_resolution: Advisory label (the cadence is fixed by the
                product). Defaults to `"raw"`.
            path: Output directory for the fetched granules.
            fmt: `strptime` format for `start` / `end`.
            data_format: Override the fetched format — `"hdf5"` or `"bin"` (the
                RADOLAN `.bz2` binary) for the operational products. Defaults to
                the product's `default_format` (`nc` reproc, `hdf5`
                operational).
            now: Reference time for the operational retention guard. Defaults to
                the current UTC time (matching the naive-UTC granule timestamps);
                an explicit value pins it (a deterministic clock for reproducible
                enumeration) and a tz-aware value is normalised to naive UTC.
            catalog: Optional pre-built :class:`Catalog`; defaults to the bundled
                catalog.
            client: Optional :class:`~earthlens.base.HttpClient`; defaults to a
                fresh one. Inject one to share a session or supply a fake
                transport.

        Raises:
            ValueError: If `variables` is empty, a product is unknown, the
                requested `data_format` is not offered by a product, or the bbox
                does not overlap Germany.
        """
        keys = list(variables)
        if not keys:
            raise ValueError(
                "RADKLIM requires a non-empty product selection, e.g. "
                "dataset='radklim-yw' or variables={'radklim-yw': []}."
            )
        self._catalog = catalog if catalog is not None else Catalog()
        self._fmt_override = data_format
        self._now = now
        self._http: HttpClient = client if client is not None else HttpClient()
        self._show_progress = True

        self._products: list[RadklimProduct] = []
        for key in keys:
            product = self._catalog.get_product(key)
            self._format_for(product)  # validate the override up front
            self._products.append(product)

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

    def _create_grid(self, lat_lim: list[float], lon_lim: list[float]) -> SpatialExtent:
        """Validate the bbox overlaps Germany, then wrap it in a `SpatialExtent`.

        RADKLIM/RADOLAN cover a fixed grid over Germany and earthlens does not
        reproject or subset, so a request whose bbox cannot intersect the
        composite is rejected rather than returning a full-grid granule for an
        area it does not cover.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: The validated bbox.

        Raises:
            ValueError: If the bbox does not overlap the Germany envelope.
        """
        west, south, east, north = GERMANY_ENVELOPE
        if (
            lon_lim[1] < west
            or lon_lim[0] > east
            or lat_lim[1] < south
            or lat_lim[0] > north
        ):
            raise ValueError(
                f"the requested bbox lat={lat_lim} lon={lon_lim} does not overlap "
                f"Germany {GERMANY_ENVELOPE} (west, south, east, north); RADKLIM / "
                "RADOLAN cover a fixed grid over Germany only."
            )
        return SpatialExtent.from_pairs(lat_lim, lon_lim)

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the date window into a `TemporalExtent` (whole-window shape).

        The backend enumerates the granule set itself (yearly for reproc,
        per-timestamp for operational), so the extent carries just the two
        bounds rather than a pandas cadence axis.

        Args:
            start: Inclusive window start.
            end: Inclusive window end.
            temporal_resolution: Advisory label (ignored).
            fmt: `strptime` format tried first for a string bound.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="all")

    def _format_for(self, product: RadklimProduct) -> str:
        """Resolve the format token to fetch for one product.

        Args:
            product: The resolved product row.

        Returns:
            str: `"nc"`, `"hdf5"`, or `"bin"`.

        Raises:
            ValueError: When `data_format=` names a format the product is not
                served in.
        """
        if self._fmt_override is None:
            return product.default_format
        if self._fmt_override not in product.formats:
            raise ValueError(
                f"data_format={self._fmt_override!r} is not available for "
                f"{product.product!r}; it is served as {product.formats}."
            )
        return self._fmt_override

    def _current_time(self) -> dt.datetime:
        """Return the naive-UTC reference time for the retention guard (injectable).

        The operational granule timestamps are naive UTC, so the guard's clock is
        naive UTC too — a naive local `datetime.now()` would skew the cutoff by the
        machine's UTC offset near the retention boundary.
        """
        if self._now is not None:
            if self._now.tzinfo is not None:
                return self._now.astimezone(dt.UTC).replace(tzinfo=None)
            return self._now
        return dt.datetime.now(dt.UTC).replace(tzinfo=None)

    def _search(self) -> list[RemoteProduct]:
        """Enumerate the granule set for every requested product over the window.

        Reproc products enumerate the yearly archives covering the window
        (deterministic, no network). Operational products read the stream's
        directory listing and keep the granules whose scan time falls in the
        window (after a rolling-retention guard).

        Returns:
            list[RemoteProduct]: One item per granule to fetch, each carrying
                its `href` and `product` / `format` metadata.
        """
        products: list[RemoteProduct] = []
        for product in self._products:
            if product.stream == "reproc":
                products.extend(self._search_reproc(product))
            else:
                products.extend(self._search_operational(product))
        return products

    def _search_reproc(self, product: RadklimProduct) -> list[RemoteProduct]:
        """Enumerate the yearly RADKLIM archives covering the window.

        Args:
            product: A reproc product row.

        Returns:
            list[RemoteProduct]: One item per year in range (clamped below to the
                archive's first year and above to the current year), each an
                archive URL.
        """
        low = _period_start_year(product.data_period)
        start_year = (
            max(self.time.start_date.year, low) if low else self.time.start_date.year
        )
        end_year = min(self.time.end_date.year, self._current_time().year)
        years = list(range(start_year, end_year + 1))
        if not years:
            logger.warning(
                f"radklim: {product.product} window "
                f"[{self.time.start_date:%Y-%m-%d}, {self.time.end_date:%Y-%m-%d}] falls "
                "outside the reprocessing coverage (2001 to the current year); no yearly "
                "archive is enumerated, returning none."
            )
        elif low and self.time.start_date.year < low:
            logger.warning(
                f"radklim: {product.product} window starts "
                f"{self.time.start_date:%Y-%m-%d} — before the reprocessing coverage; "
                f"only the {low}- portion is enumerated (mirrors the operational "
                "retention straddle)."
            )
        fmt = self._format_for(product)
        out: list[RemoteProduct] = []
        for year in years:
            href = reproc_archive_url(
                product.cdc_frequency, product.version, product.code, year
            )
            out.append(
                RemoteProduct(
                    id=f"{product.product}-{year}",
                    href=href,
                    metadata={"product": product.product, "year": year, "format": fmt},
                )
            )
        return out

    def _search_operational(self, product: RadklimProduct) -> list[RemoteProduct]:
        """Enumerate the operational granules in the window from the listing.

        Args:
            product: An operational product row.

        Returns:
            list[RemoteProduct]: One item per in-window granule the directory
                listing offers; empty when the window predates the rolling
                retention.
        """
        start_date = self.time.start_date
        end_date = _inclusive_end(self.time.end_date)
        now = self._current_time()
        if start_date > now:
            logger.warning(
                f"radklim: {product.product} window starts {start_date:%Y-%m-%d} — in "
                f"the future (after {now:%Y-%m-%d}); no granules exist yet, returning "
                "none."
            )
            return []
        cutoff = now - dt.timedelta(days=product.retention_days)
        if end_date < cutoff:
            logger.warning(
                f"radklim: {product.product} window ends {end_date:%Y-%m-%d} — before "
                f"the ~{product.retention_days}-day operational retention "
                f"(since {cutoff:%Y-%m-%d}); nothing is retained, returning none. "
                "Use a RADKLIM reproc product (radklim-rw / radklim-yw) for the "
                "archive."
            )
            return []
        if start_date < cutoff:
            logger.warning(
                f"radklim: {product.product} window starts {start_date:%Y-%m-%d} — before "
                f"the ~{product.retention_days}-day operational retention "
                f"(since {cutoff:%Y-%m-%d}); only the retained tail is returned. Use a "
                "RADKLIM reproc product for the earlier part of the window."
            )
        fmt = self._format_for(product)
        ext = FORMAT_EXTENSION[fmt]
        html = self._http.get(operational_dir_url(product.code)).text
        out: list[RemoteProduct] = []
        for name in parse_listing(html, product.code, ext):
            when = timestamp_from_name(name)
            if start_date <= when <= end_date:
                out.append(
                    RemoteProduct(
                        id=f"{product.product}-{when:%Y%m%d%H%M}",
                        href=operational_granule_url(product.code, name),
                        metadata={"product": product.product, "format": fmt},
                    )
                )
        return out

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download every enumerated granule, skipping any that is not published.

        Each granule is streamed to `self.root_dir` under its own file name, with
        a leading-bytes check for the format (so a `200` error page never lands
        under a granule name). A `404` (a year not yet published, a granule that
        rotated out mid-run) is logged and skipped.

        Args:
            products: The granules from :meth:`_search`.

        Returns:
            list[Path]: The written granule paths, in order.
        """
        out: list[Path] = []
        for rp in tqdm(
            products, disable=not self._show_progress, desc="radklim", unit="file"
        ):
            fetched = self._fetch_one(rp)
            if fetched is not None:
                out.append(fetched)
        return out

    def _fetch_one(self, product: RemoteProduct) -> Path | None:
        """Download one granule, or return `None` when it is not published.

        Args:
            product: One :class:`~earthlens.base.RemoteProduct` from
                :meth:`_search`.

        Returns:
            Path | None: The written path, or `None` when the URL 404s.
        """
        assert product.href is not None  # every radklim product carries a granule URL
        dest = self.root_dir / safe_filename(product.href.rsplit("/", 1)[-1])
        magic = FORMAT_MAGIC[product.metadata["format"]]
        try:
            # `Accept-Encoding: identity`: the granule is already compressed
            # (gzip `.tar.gz` / bzip2 / HDF5), so a transparent `Content-Encoding:
            # gzip` from the server would make `requests` decode the body and the
            # `expect_magic` byte check fire against the wrong (decoded) bytes.
            self._http.download(
                product.href,
                dest,
                progress=self._show_progress,
                expect_magic=magic,
                headers={"Accept-Encoding": "identity"},
            )
        except requests.HTTPError as exc:
            if _is_missing(exc):
                logger.warning(
                    f"radklim: skipping {product.id} — not published ({product.href})."
                )
                return None
            raise
        return dest

    def download(self, progress_bar: bool = True) -> list[Path]:
        """Fetch the requested granules and return the written paths.

        Runs the cheap :meth:`_search` (granule enumeration) then :meth:`_fetch`,
        streaming each granule to `path`.

        Args:
            progress_bar: Show a per-granule progress bar. Defaults to `True`.

        Returns:
            list[Path]: The written granule paths — RADKLIM yearly NetCDF
                archives and/or operational HDF5 / binary granules. Empty when
                nothing in the window was available.
        """
        self._show_progress = progress_bar
        return cast("list[Path]", self._api_via_search_fetch())


def _inclusive_end(end_date: dt.datetime) -> dt.datetime:
    """Extend a date-only (midnight) end bound to the end of that day.

    The operational stream is compared at per-scan (sub-day) granularity, but a
    window parsed with the default `%Y-%m-%d` lands `end` at `00:00:00`, which
    would drop every granule after midnight on the end day. A midnight end bound
    is therefore treated as "the whole end day" (`23:59:59`); a bound that
    carries a time of day is respected as-is.

    Args:
        end_date: The parsed inclusive end of the window.

    Returns:
        datetime.datetime: `end_date` at `23:59:59` when it was midnight, else
            `end_date` unchanged.
    """
    if end_date.time() == dt.time(0):
        return end_date.replace(hour=23, minute=59, second=59)
    return end_date


def _period_start_year(data_period: str) -> int | None:
    """Return the first calendar year of a `data_period` like `2001-01-01/`.

    Args:
        data_period: The catalog `data_period` string, possibly empty.

    Returns:
        int | None: The leading year, or `None` when the period is blank / has
            no leading year.
    """
    head = data_period.strip()[:4]
    return int(head) if head.isdigit() else None


def _is_missing(exc: requests.HTTPError) -> bool:
    """Return whether an `HTTPError` is a `404 Not Found`.

    Args:
        exc: The error raised by a granule download.

    Returns:
        bool: `True` when the response status is `404`.
    """
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404
