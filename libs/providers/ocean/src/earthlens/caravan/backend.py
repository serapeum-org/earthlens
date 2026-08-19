"""Backend that fetches Caravan large-sample hydrology from Zenodo.

`Caravan(AbstractDataSource)` assembles per-catchment daily **streamflow** plus
**ERA5-Land** meteorological forcing into a long :class:`pandas.DataFrame`, so
`OUTPUT_KIND = "tabular"` and the :class:`earthlens.earthlens.EarthLens` facade
rejects an `aggregate=` argument.

A request names an extension (`dataset="grdc"`), a set of catchments, a time
window, and the variables wanted. Catchments are selected explicitly
(`gauge_ids=[...]`), by bounding box, or by `country=` — the last two resolved
against the archive's own `attributes_other_<source>.csv` centroid table. An
unbounded request is refused rather than silently pulling every catchment.

**Nothing is downloaded for the common case.** Every extension ships as a ZIP,
which is read in place over HTTP Range requests: one catchment out of the
8.84 GB GRDC archive costs about 3 MB. The exception is `base` at v1.6, a
24.8–29.0 GB `.tar.gz` that cannot be seeked; that row demands
`allow_full_download=True`, and `version="1.2"` offers a range-readable
alternative at the cost of being a materially older and smaller dataset.

Caravan is a **versioned historical archive, not a live feed**. Releases land
every 4–12 months and the series lag the present by a year or more, so use
`earthlens.usgs_water` (US near-real-time) or GloFAS via `earthlens.ecmwf` when
current discharge is what is needed.
"""

from __future__ import annotations

import hashlib
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.base.http import HttpClient, RangeReadError
from earthlens.caravan import _helpers
from earthlens.caravan.catalog import (
    ArchiveFile,
    Catalog,
    Extension,
    TimeseriesFormat,
    Version,
)

#: Columns the assembled frame always leads with, before the requested
#: variables.
INDEX_COLUMNS = ("gauge_id", "date")

#: The bbox that means "the whole world", i.e. the caller expressed no spatial
#: filter at all. Matched exactly rather than by area so a deliberate
#: near-global request still counts as a filter.
_GLOBAL_BBOX = (-90.0, 90.0, -180.0, 180.0)

#: Minimum seconds between requests to Zenodo. Anonymous callers get roughly
#: 60 requests/minute, and reading a catchment costs two ranged `GET`s, so an
#: unthrottled multi-catchment request walks straight into `429`s — observed
#: against the Denmark archive before this was added. One second per request
#: stays inside the published budget.
DEFAULT_MIN_INTERVAL = 1.0

#: Selecting more than this many catchments is legal but slow, so it is called
#: out before the reads start rather than discovered as a stall.
_LARGE_SELECTION = 25


class Caravan(AbstractDataSource):
    """Fetch Caravan per-catchment daily hydrology from static Zenodo archives.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is a long DataFrame of
            catchment-days, so the facade refuses an `aggregate=`.

    Examples:
        - Construction is offline; the catalog resolves the pinned release:
            ```python
            >>> from earthlens.caravan import Caravan
            >>> src = Caravan(
            ...     start="2000-01-01", end="2000-12-31",
            ...     variables=["streamflow"],
            ...     lat_lim=[-35.0, -25.0], lon_lim=[15.0, 25.0],
            ...     dataset="grdc",
            ... )
            >>> src.OUTPUT_KIND
            'tabular'
            >>> src.archive_file.archive_format
            'zip'
            >>> src.release.n_catchments
            5356

            ```
    """

    OUTPUT_KIND: OutputKind = "tabular"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]] | list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        fmt: str = "%Y-%m-%d",
        path: Path | str | None = None,
        *,
        dataset: str = "grdc",
        version: str | None = None,
        gauge_ids: list[str] | None = None,
        country: str | None = None,
        timeseries_format: str = "csv",
        with_attributes: bool = False,
        with_geometry: bool = False,
        allow_full_download: bool = False,
        write_table: bool = True,
        client: HttpClient | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        cache_root: Path | None = None,
        catalog: Catalog | None = None,
    ) -> None:
        """Build a Caravan request.

        Args:
            start: Inclusive start date of the window.
            end: Inclusive end date of the window.
            variables: Variable names to return — friendly catalog names
                (`"streamflow"`, `"total_precipitation"`) or the real archive
                column names, which pass through unchanged.
            lat_lim: `[lat_min, lat_max]`. A whole-globe box counts as no
                spatial filter.
            lon_lim: `[lon_min, lon_max]`.
            temporal_resolution: Recorded as the resolution label; Caravan is
                daily throughout.
            fmt: `strptime` format for `start` / `end`.
            path: Output directory for the written table.
            dataset: The extension key — `"grdc"` (default), `"denmark"`,
                `"israel"`, `"germany"`, or `"base"`.
            version: A specific release of that extension. `None` uses the
                catalog's pinned default. For `base`, `"1.2"` selects the
                range-readable ZIP.
            gauge_ids: Explicit catchment ids. Note GRDC's ids carry an
                uppercase prefix (`GRDC_1159100`) unlike every other source.
            country: Restrict to one country. Matched case-insensitively
                against the full English name in `attributes_other_*`
                (`"Denmark"`, `"South Africa"`).
            timeseries_format: Only `"csv"` is supported. The archives also
                publish a `.nc` variant of the same data, but decoding it would
                need an array library earthlens does not depend on, so
                `"netcdf"` raises `NotImplementedError`.
            with_attributes: Merge the static catchment attributes onto every
                row.
            with_geometry: Attach the basin polygons, returned alongside the
                frame on :attr:`geometry`.
            allow_full_download: Permit a release that can only be fetched by
                downloading the whole multi-gigabyte archive. Required for
                `base` at its default version.
            write_table: Write the assembled frame to `path`. `False` returns
                it without touching the filesystem.
            client: Transport to read through; injectable for tests. When
                `None`, a throttled :class:`HttpClient` is built (see
                `min_interval`).
            min_interval: Minimum seconds between requests to Zenodo, which
                rate-limits anonymous callers. Only used when `client` is
                `None`; an injected client keeps its own policy.
            cache_root: Cache directory for downloaded archives.
            catalog: A pre-built catalog; the bundled one when `None`.

        Raises:
            ValueError: If `dataset` or `version` is unknown, if
                `timeseries_format` is not `"csv"`, or if the release needs
                `allow_full_download=True`.
            NotImplementedError: If `timeseries_format="netcdf"` - see that
                argument's note.
        """
        self._catalog = catalog if catalog is not None else Catalog()
        self._dataset = dataset
        self._version = version
        self._gauge_ids = list(gauge_ids) if gauge_ids else []
        self._country = country
        if timeseries_format == "netcdf":
            raise NotImplementedError(
                "timeseries_format='netcdf' is not supported. Caravan's .nc "
                "members are 1-D per-catchment time series, but pyramids - which "
                "owns every array container in this ecosystem - models NetCDF as "
                "raster, so it reads them as an empty 0-band grid. Decoding them "
                "would need h5py/netCDF4/xarray, none of which earthlens depends "
                "on. Use the default timeseries_format='csv': the CSV archive "
                "carries the same catchments, columns and period."
            )
        if timeseries_format != "csv":
            raise ValueError(
                f"timeseries_format={timeseries_format!r} is not supported; "
                f"expected 'csv'."
            )
        self._timeseries_format = cast("TimeseriesFormat", timeseries_format)
        self._with_attributes = with_attributes
        self._with_geometry = with_geometry
        self._allow_full_download = allow_full_download
        self._write_table_enabled = write_table
        # A shared, throttled client — one per instance, so the interval is
        # enforced across every ranged read of the archive rather than per call.
        self._owns_client = client is None
        self._client = (
            client if client is not None else HttpClient(min_interval=min_interval)
        )
        self._cache_root = cache_root
        self._archive: _helpers.CaravanArchive | None = None
        self._selected: list[tuple[str, str]] = []
        self._columns: list[str] | None = None

        #: Basin polygons, populated by `download()` when `with_geometry`.
        self.geometry: Any = None

        super().__init__(
            start=start,
            end=end,
            variables=variables,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            temporal_resolution=temporal_resolution,
            fmt=fmt,
            path=path,
        )

    def _initialize(self) -> None:
        """Resolve the catalog row, release and archive file — offline.

        Runs before the extents are built, so a bad `dataset=` / `version=` /
        oversized-archive request fails at construction rather than after a
        network round trip.

        Returns:
            None: Nothing is bound onto `self.client` - the HTTP transport is
                built in `__init__` and handed to each archive as it opens.

        Raises:
            ValueError: If the extension or version is unknown, or the release
                is download-only and `allow_full_download` was not set.
        """
        self.extension: Extension = self._catalog.get_extension(self._dataset)
        self.release: Version = self.extension.resolve_version(self._version)
        self.archive_file: ArchiveFile = self.release.file_for(self._timeseries_format)
        self._check_download_allowed()
        return None

    def _check_download_allowed(self) -> None:
        """Refuse a whole-archive fetch that the caller did not ask for.

        Reading a ZIP costs a few megabytes, so it is always allowed. A
        `tar.gz` is a single gzip stream with no directory: reaching one
        catchment means transferring all 24.8–29.0 GB of it, which no one
        should trigger by typing a dataset name.

        Raises:
            ValueError: If the release is not range-readable and
                `allow_full_download` is `False`.
        """
        if self.archive_file.is_range_readable or self._allow_full_download:
            return
        # Ordered by release date, not by key: a lexicographic sort would rank
        # "1.10" below "1.9" and recommend the older release.
        alternatives = [
            key
            for key, _ in sorted(
                (
                    (key, release.release_date)
                    for key, release in self.extension.versions.items()
                    if release.files.get(self._timeseries_format) is not None
                    and release.file_for(self._timeseries_format).is_range_readable
                ),
                key=lambda pair: pair[1],
                reverse=True,
            )
        ]
        hint = (
            f" Pass version={alternatives[0]!r} to read a range-accessible "
            f"release instead (note it is an older, smaller release)."
            if alternatives
            else ""
        )
        raise ValueError(
            f"the {self._dataset!r} extension at version "
            f"{self._version or self.extension.default_version!r} ships as a "
            f"{self.archive_file.archive_format} "
            f"({self.archive_file.size / 1e9:.1f} GB), which cannot be read in "
            f"place - reaching one catchment means downloading all of it. Pass "
            f"allow_full_download=True to accept that transfer.{hint}"
        )

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse `[start, end]` into a :class:`TemporalExtent`.

        Caravan members hold a catchment's whole record in one file, so the
        window is a filter applied after the read rather than a per-date loop.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses later than `end`.
        """
        return self._whole_window_extent(
            start, end, fmt=fmt, resolution=temporal_resolution
        )

    @property
    def _has_bbox(self) -> bool:
        """Whether the request carries a real spatial filter.

        Returns:
            bool: `False` when the bbox is the whole globe, which is how a
                caller who simply had to pass *something* is recognised.
        """
        bounds = (self.space.south, self.space.north, self.space.west, self.space.east)
        return bounds != _GLOBAL_BBOX

    def _open_archive(self) -> _helpers.CaravanArchive:
        """Open (once) the archive this request reads from.

        Returns:
            CaravanArchive: A remote ZIP read over HTTP Range, or a downloaded
                and md5-verified tarball.
        """
        if self._archive is not None:
            return self._archive
        archive_file = self.archive_file
        if archive_file.is_range_readable:
            try:
                self._archive = _helpers.CaravanArchive.open_remote_zip(
                    archive_file.url,
                    client=self._client,
                    # A catalogued size saves the HEAD probe, but zero means the
                    # row records none - probe rather than believe the archive
                    # is empty.
                    size=archive_file.size or None,
                    label=f"caravan/{self._dataset}",
                )
            except RangeReadError:
                # A live HTTP failure is not a catalog problem; let it
                # surface with its own message and status.
                raise
            except zipfile.BadZipFile as exc:
                # Almost always a stale pin: the catalogued size no longer
                # matches what Zenodo serves, so the central directory is not
                # where the offsets say. Raised bare, that reads as a corrupt
                # download rather than a catalog problem.
                raise ValueError(
                    f"could not read the {self._dataset!r} archive "
                    f"({archive_file.name}) as a ZIP. The catalog pins record "
                    f"{archive_file.record} at {archive_file.size} bytes; if "
                    f"Zenodo now serves something else the pin is stale. Run "
                    f"`earthlens datasets refresh caravan` to check."
                ) from exc
        else:
            tarball = _helpers.ensure_archive(
                archive_file, cache_root=self._cache_root, client=self._client
            )
            self._archive = _helpers.CaravanArchive.open_local_tar(
                tarball,
                label=f"caravan/{self._dataset}",
                fingerprint=archive_file.md5,
            )
        return self._archive

    def _resolve_gauges(
        self, archive: _helpers.CaravanArchive
    ) -> list[tuple[str, str]]:
        """Resolve the request to concrete `(source, gauge_id)` pairs.

        Args:
            archive: The opened archive.

        Returns:
            list[tuple[str, str]]: The selected catchments, sorted.

        Raises:
            ValueError: If the request names no catchments at all, if an
                explicit id is absent from the archive, or if the filters match
                nothing.
        """
        if not self._gauge_ids and not self._has_bbox and self._country is None:
            raise ValueError(
                f"an unbounded Caravan request would return every catchment in "
                f"the {self._dataset!r} extension "
                f"({self.release.n_catchments}). Narrow it with gauge_ids=[...], "
                f"a lat_lim/lon_lim bounding box, or country='...'."
            )
        if self._gauge_ids:
            return self._resolve_explicit(archive)
        return self._resolve_by_filters(archive)

    def _resolve_explicit(
        self, archive: _helpers.CaravanArchive
    ) -> list[tuple[str, str]]:
        """Validate explicitly requested ids against the archive.

        Args:
            archive: The opened archive.

        Returns:
            list[tuple[str, str]]: The `(source, gauge_id)` pairs.

        Raises:
            ValueError: If any id is not in the archive. The message shows a
                sample of valid ids, since the prefix convention differs per
                source and is the usual cause.
        """
        pairs: list[tuple[str, str]] = []
        missing: list[str] = []
        for gauge_id in self._gauge_ids:
            for source in archive.sources:
                if archive.timeseries_member(source, gauge_id, self._timeseries_format):
                    pairs.append((source, gauge_id))
                    break
            else:
                missing.append(gauge_id)
        if missing:
            # `sources` is empty when nothing matched the timeseries pattern,
            # so the sample lookup - which runs inside this error path - must
            # tolerate that rather than raising over the real problem.
            sample = (
                archive.gauge_ids(archive.sources[0], self._timeseries_format)[:3]
                if archive.sources
                else []
            )
            if not sample:
                raise ValueError(
                    f"the {self._dataset!r} archive "
                    f"({self.archive_file.name}) exposes no timeseries members "
                    f"for format {self._timeseries_format!r}, so {missing} - and "
                    f"any other id - cannot be resolved. The archive layout may "
                    f"have changed; run `earthlens datasets refresh caravan`."
                )
            raise ValueError(
                f"{missing} not found in the {self._dataset!r} extension. "
                f"Ids look like {sample} - note the prefix and its casing differ "
                f"between sources."
            )
        return sorted(pairs)

    def _resolve_by_filters(
        self, archive: _helpers.CaravanArchive
    ) -> list[tuple[str, str]]:
        """Select catchments by bounding box and/or country.

        Args:
            archive: The opened archive.

        Returns:
            list[tuple[str, str]]: The matching `(source, gauge_id)` pairs.

        Raises:
            ValueError: If nothing matches, with the filters echoed back.
        """
        pairs: list[tuple[str, str]] = []
        for source in archive.sources:
            try:
                index = _helpers.attribute_index(archive, source)
            except ValueError as exc:
                # One source without a centroid table must not abort a
                # multi-source request; the others can still be resolved.
                logger.warning(f"caravan {self._dataset}: skipping {source} - {exc}")
                continue
            selected = index
            if self._has_bbox:
                selected = selected[
                    selected["gauge_lat"].between(self.space.south, self.space.north)
                    & selected["gauge_lon"].between(self.space.west, self.space.east)
                ]
            if self._country is not None:
                wanted = self._country.strip().casefold()
                selected = selected[
                    selected["country"].astype(str).str.strip().str.casefold() == wanted
                ]
            for gauge_id in selected.index:
                if archive.timeseries_member(
                    source, str(gauge_id), self._timeseries_format
                ):
                    pairs.append((source, str(gauge_id)))
        if not pairs:
            raise ValueError(
                f"no {self._dataset!r} catchment matched "
                f"lat_lim={[self.space.south, self.space.north]}, "
                f"lon_lim={[self.space.west, self.space.east]}"
                + (f", country={self._country!r}" if self._country else "")
                + ". Note country is matched on the full English name."
            )
        return sorted(pairs)

    def _search(self) -> list[RemoteProduct]:
        """Resolve the request to one product per selected catchment.

        Returns:
            list[RemoteProduct]: One product per catchment, carrying the source
                and the archive member its series lives in.

        Raises:
            ValueError: On an unbounded request, an unknown id, or no match.
        """
        archive = self._open_archive()
        pairs = self._resolve_gauges(archive)
        self._selected = pairs
        products = []
        for source, gauge_id in pairs:
            member = archive.timeseries_member(
                source, gauge_id, self._timeseries_format
            )
            products.append(
                RemoteProduct(
                    id=gauge_id,
                    href=self.archive_file.url,
                    metadata={"source": source, "member": member},
                )
            )
        logger.info(
            f"caravan {self._dataset}: {len(products)} catchment(s) selected "
            f"from {self.archive_file.name}"
        )
        if len(products) > _LARGE_SELECTION and self._limit is None:
            logger.warning(
                f"caravan {self._dataset}: {len(products)} catchments selected. "
                f"Each is a separate ranged read and Zenodo is rate-limited, so "
                f"this will take roughly {len(products) * 2 // 60 + 1} minute(s). "
                f"Narrow the filters, or pass limit= (a cap on ROWS, not "
                f"catchments) to stop reading early."
            )
        return products

    def _requested_columns(self) -> list[str]:
        """Map the requested variables onto this release's column names.

        Returns:
            list[str]: The archive column names, de-duplicated, order-stable.

        Raises:
            ValueError: If a variable is unknown, or exists only in source
                data this extension does not contain.
        """
        # The ABC advertises `dict[str, list[str]] | list[str]`. `list(a_dict)`
        # would yield its KEYS, resolving a dataset key as a variable name, so
        # the grouped values are flattened instead.
        if isinstance(self.vars, dict):
            names: list[Any] = [name for group in self.vars.values() for name in group]
        else:
            names = list(self.vars)
        columns: list[str] = []
        for name in names:
            variable = self._catalog.get_variable(self._dataset, str(name))
            column = variable.column_for(self.release.column_set)
            if column not in columns:
                columns.append(column)
        return columns

    def _fetch(self, products: list[RemoteProduct]) -> list[pd.DataFrame]:
        """Read every selected catchment and normalise to the long schema.

        Widens the inherited `-> list[Path]` contract: a tabular backend
        returns in-memory frames, not written files.

        The two transports want opposite strategies, so this branches on which
        one is in play. A ZIP member is an independent ranged read, so the
        catchments are consumed **lazily** and a `limit=` genuinely stops the
        fetch early instead of paying for reads it then discards. A tar has to
        be scanned sequentially, so there every wanted member is pulled in one
        pass and the cap is applied afterwards — re-scanning a 29 GB stream per
        catchment would be far worse than over-reading.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[pd.DataFrame]: One frame per catchment, in the same order.
                A catchment the archive turns out not to hold is logged and
                skipped rather than failing the whole request.
        """
        archive = self._open_archive()
        if self.archive_file.is_range_readable:
            frames = self._fetch_limited(products, self._limit)
        else:
            frames = self._fetch_sequential(archive, products)
        self._log_transfer(archive)
        return [frame for frame in frames if frame is not None]

    def _fetch_one(self, product: RemoteProduct) -> pd.DataFrame:
        """Read one catchment from a range-readable archive.

        Args:
            product: One product from :meth:`_search`.

        Returns:
            pandas.DataFrame: The catchment's rows within the request window,
                or an empty frame when its member cannot be read.
        """
        archive = self._open_archive()
        member = str(product.metadata["member"])
        # Resolved once per request rather than per catchment: it depends only
        # on the request, and a bbox selection can run to hundreds of members.
        if self._columns is None:
            self._columns = self._requested_columns()
        try:
            blob = archive.read(member)
        except KeyError:
            logger.warning(
                f"caravan {self._dataset}: {product.id} is listed but its member "
                f"{member} could not be read; skipping."
            )
            return pd.DataFrame(columns=[*INDEX_COLUMNS, *self._columns])
        return self._to_frame(product.id, blob, self._columns)

    def _fetch_sequential(
        self, archive: _helpers.CaravanArchive, products: list[RemoteProduct]
    ) -> list[pd.DataFrame]:
        """Read every catchment out of a tar archive in one streaming pass.

        Args:
            archive: The opened tar archive.
            products: The list returned by :meth:`_search`.

        Returns:
            list[pd.DataFrame]: One frame per readable catchment.
        """
        members = [str(p.metadata["member"]) for p in products if p.metadata["member"]]
        blobs = archive.read_many(members)
        columns = self._requested_columns()
        frames: list[pd.DataFrame] = []
        for product in products:
            blob = blobs.get(str(product.metadata["member"]))
            if blob is None:
                logger.warning(
                    f"caravan {self._dataset}: {product.id} is listed but its "
                    f"member could not be read; skipping."
                )
                continue
            frames.append(self._to_frame(product.id, blob, columns))
        return frames

    def _log_transfer(self, archive: _helpers.CaravanArchive) -> None:
        """Report what the request actually cost on the wire.

        Args:
            archive: The archive that was read.
        """
        requests, megabytes = archive.transfer_stats
        if requests:
            logger.info(
                f"caravan {self._dataset}: {requests} range request(s), "
                f"{megabytes:.2f} MB transferred (archive is "
                f"{self.archive_file.size / 1e9:.1f} GB)"
            )

    def _to_frame(self, gauge_id: str, blob: bytes, columns: list[str]) -> pd.DataFrame:
        """Parse one catchment's member into the long schema.

        Args:
            gauge_id: The catchment id, stamped onto every row.
            blob: The member's bytes.
            columns: The archive column names to keep.

        Returns:
            pandas.DataFrame: `[gauge_id, date, <columns>]`, filtered to the
                request window. Missing observations stay `NaN` — a blank
                `streamflow` is normal in Caravan and must not be dropped.
        """
        frame = self._read_member(blob)
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        window = frame["date"].between(
            pd.Timestamp(self.time.start_date), pd.Timestamp(self.time.end_date)
        )
        frame = frame.loc[window].copy()
        for absent in [column for column in columns if column not in frame.columns]:
            logger.warning(
                f"caravan {self._dataset}: column {absent!r} is absent from "
                f"{gauge_id}; returning it empty."
            )
            # A `pd.NA` column comes out `object`, which survives the concat
            # and breaks arithmetic on a column the caller asked for as numeric.
            frame[absent] = np.nan
        frame.insert(0, "gauge_id", gauge_id)
        # Requested order, not archive order: the columns the caller listed come
        # back in the order they listed them, present or not.
        return frame[[*INDEX_COLUMNS, *columns]]

    def _read_member(self, blob: bytes) -> pd.DataFrame:
        """Decode one timeseries member into a frame.

        Always CSV: `pandas` parses the member directly, with no decode step
        and no array library involved.

        Args:
            blob: The member's bytes.

        Returns:
            pandas.DataFrame: The catchment's full record, one row per day.
        """
        return pd.read_csv(BytesIO(blob))

    def _attach_attributes(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Merge the static catchment attributes onto every row.

        Args:
            frame: The assembled long frame.

        Returns:
            pandas.DataFrame: `frame` with the attribute columns joined on
                `gauge_id`.
        """
        archive = self._open_archive()
        # Only the sources actually selected: reading every source's tables is
        # wasted work, and a gauge_id duplicated across sources would fan one
        # output row out into several.
        wanted = {source for source, _ in self._selected}
        tables = [
            _helpers.merge_attributes(archive, source)
            for source in archive.sources
            if not wanted or source in wanted
        ]
        tables = [table for table in tables if not table.empty]
        if not tables:
            return frame
        attributes = pd.concat(tables)
        duplicated = attributes.index.duplicated()
        if duplicated.any():
            logger.warning(
                f"caravan {self._dataset}: {int(duplicated.sum())} gauge_id(s) "
                f"appear in more than one source's attributes; keeping the first "
                f"so the row count is preserved."
            )
            attributes = attributes[~duplicated]
        return frame.merge(attributes, how="left", left_on="gauge_id", right_index=True)

    def _load_geometry(self) -> Any:
        """Read the basin polygons for the sources this request touched.

        Every shapefile sidecar is extracted together — GDAL cannot open a
        `.shp` without at least its `.shx` and `.dbf`.

        Returns:
            Any: A `pyramids.FeatureCollection` of basin polygons, or `None`
                when the archive ships none.
        """
        import tempfile

        from pyramids.feature.collection import FeatureCollection

        archive = self._open_archive()
        wanted = {source for source, _ in self._selected}
        sources = [s for s in archive.sources if not wanted or s in wanted]
        collections: list[tuple[str, Any]] = []
        for source in sources:
            members = archive.shapefile_members(source)
            if not members:
                continue
            blobs = archive.read_many(members)
            with tempfile.TemporaryDirectory() as scratch:
                shp: Path | None = None
                for member, blob in blobs.items():
                    target = Path(scratch) / Path(member).name
                    target.write_bytes(blob)
                    if target.suffix == ".shp":
                        shp = target
                if shp is not None:
                    collections.append((source, FeatureCollection.read_file(str(shp))))
        if not collections:
            return None
        if len(collections) == 1:
            return collections[0][1]
        # Concatenate rather than pick: silently returning one source's polygons
        # for a multi-source selection loses the rest with no signal, and `base`
        # spans seven sources.
        names = [name for name, _ in collections]
        frames = [collection for _, collection in collections]
        crs_values = {str(frame.crs) for frame in frames if frame.crs is not None}
        if len(crs_values) > 1:
            raise ValueError(
                f"caravan {self._dataset}: basin shapes span more than one CRS "
                f"({sorted(crs_values)}); merging them would misplace geometries. "
                f"Request one source at a time."
            )
        logger.info(f"caravan {self._dataset}: merging basin shapes from {names}")
        # `ignore_index` because each source's frame is indexed from 0; a plain
        # concat repeats those labels and breaks `.loc` on the result. A
        # `FeatureCollection` is already a `GeoDataFrame`, so no re-wrap.
        return pd.concat(frames, ignore_index=True)

    def _create_output_path(self) -> Path:
        """Return the path the assembled table is written to.

        Returns:
            Path: `<root_dir>/caravan_<dataset>_<version>.csv`.
        """
        version = self._version or self.extension.default_version
        safe_version = version.replace(".", "-")
        # The window AND the selection are part of the identity: with only the
        # window, two requests for different catchments over the same dates
        # still overwrite each other. The selection is hashed because an
        # explicit id list can be thousands of entries long.
        window = f"{self.time.start_date:%Y%m%d}-{self.time.end_date:%Y%m%d}"
        selector = "|".join(
            [
                ",".join(sorted(self._gauge_ids)),
                str(self._country or ""),
                f"{self.space.south},{self.space.north}",
                f"{self.space.west},{self.space.east}",
            ]
        )
        # Not a security primitive: this only has to be a short, stable id
        # distinguishing one selection from another in a file name.
        digest = hashlib.sha1(selector.encode(), usedforsecurity=False).hexdigest()[:8]
        return (
            self._ensure_root_dir()
            / f"caravan_{self._dataset}_{safe_version}_{window}_{digest}.csv"
        )

    def download(
        self, progress_bar: bool = True, limit: int | None = None
    ) -> pd.DataFrame:
        """Fetch the selected catchments and return them as one long frame.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends. Members are read individually and the cost is
                dominated by the archive index, so no bar is shown.
            limit: Cap on the total rows returned. `None` returns everything.

        Returns:
            pandas.DataFrame: `[gauge_id, date, <requested variables>]`, one
                row per catchment-day. `streamflow` is in **mm/day**; blank
                values are genuine missing observations. Empty selections
                return a schema-only frame rather than `None`.

        Raises:
            ValueError: On an unbounded request, an unknown catchment id, an
                unknown variable, or a release needing `allow_full_download`.
        """
        self._limit = self.check_limit(limit)
        # Re-resolved per download so a caller who reassigns `vars` between
        # calls is not served the previous request's columns.
        self._columns = None
        # Drop the empty fragments a skipped catchment or an out-of-window
        # member leaves behind: concatenating them makes pandas infer dtypes
        # from all-NA columns, which it warns about and will change.
        frames = [frame for frame in self._api() if not frame.empty]
        if frames:
            table = pd.concat(frames, ignore_index=True)
        else:
            table = pd.DataFrame(columns=[*INDEX_COLUMNS, *self._requested_columns()])
        if self._with_attributes and not table.empty:
            table = self._attach_attributes(table)
        if self._limit is not None:
            table = table.head(self._limit)
        if self._with_geometry:
            self.geometry = self._load_geometry()
        if self._write_table_enabled:
            out_path = self._create_output_path()
            table.to_csv(out_path, index=False)
            logger.info(
                f"caravan {self._dataset}: {len(table)} row(s) written to {out_path}"
            )
        return table

    @property
    def transfer_stats(self) -> tuple[int, float]:
        """Requests issued and megabytes transferred for this request.

        The public way to check what a fetch actually cost, which is the whole
        premise of the range-read design. `(0, 0.0)` before anything is read,
        and for the tar transport, which transfers nothing at read time.

        Returns:
            tuple[int, float]: `(request_count, megabytes)`.
        """
        if self._archive is None:
            return (0, 0.0)
        return self._archive.transfer_stats

    def close(self) -> None:
        """Release the opened archive and the HTTP session behind it.

        `download()` deliberately does not call this: the archive carries the
        transfer statistics a caller may want to inspect afterwards. Use the
        backend as a context manager, or call this when done.
        """
        if self._archive is not None:
            self._archive.close()
            self._archive = None
        if self._owns_client:
            closer = getattr(self._client.session, "close", None)
            if callable(closer):
                closer()

    def __enter__(self) -> Caravan:
        """Return self, so a request can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the archive and session on leaving the block."""
        self.close()

    def _api(self) -> list[pd.DataFrame]:
        """Run the search then fetch steps.

        Returns:
            list[pd.DataFrame]: One frame per selected catchment.
        """
        return self._api_via_search_fetch()
