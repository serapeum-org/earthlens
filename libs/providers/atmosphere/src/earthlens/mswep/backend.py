"""Backend that fetches GloH2O MSWEP / MSWX granules from an approved Drive share.

`MSWEP(AbstractDataSource)` downloads raw NetCDF granules from the
Google-Drive folder GloH2O shares with an approved non-commercial user.
`OUTPUT_KIND = "raster"` and `download()` returns the `list[Path]`
written — the granules are shipped as-is, and reading or regridding them
is pyramids' job, so this module never imports `xarray`.

Three properties shape the backend:

* **Two path shapes.** MSWEP is `<root>/<variant>/<temporal>/`; MSWX
  inserts a variable level, `<root>/<variant>/<variable>/<temporal>/`.
  The shape comes from the catalog's per-product `path_template`, never
  from a hard-coded f-string, so an MSWX request cannot silently build
  an MSWEP-shaped path.
* **The variant is date-determined.** `Past` / `Past_nogauge` end
  2024-12-31 and `NRT` starts 2025-01-01, so a window is routed to the
  variant that can serve it — and a window straddling the cut-over spans
  both rather than half-failing. An explicit `variant=` that cannot
  cover the window raises, naming the one that can.
* **Granules are resolved by name, never by listing.** `Past/Hourly/`
  holds roughly 400,000 files; :mod:`earthlens.mswep.drive` asks Drive
  for exactly the names wanted, in chunks.

A granule that is genuinely absent — expected near the NRT edge, whose
latency is about two hours — is logged and skipped. That is a
deliberately narrow allowance: transport failures must not take the same
path, or a request would return a silently partial time series.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.biodiversity import warn_license
from earthlens.mswep.auth import MswepAuth, MswepCredentials
from earthlens.mswep.catalog import Catalog
from earthlens.mswep.drive import (
    RootResolver,
    download_media,
    find_children_by_name,
    find_folder,
)

#: User-facing resolution to the pandas offset alias its date axis steps
#: on. Keys match the catalog's `resolutions:` block.
CADENCES: dict[str, str] = {
    "hourly": "h",
    "3hourly": "3h",
    "daily": "D",
    "monthly": "MS",
}


class MSWEP(AbstractDataSource):
    """Download MSWEP / MSWX NetCDF granules from an approved GloH2O share.

    Attributes:
        OUTPUT_KIND: Fixed `"raster"` — every granule is a 0.1 degree
            global grid.
        SUPPORTS_AGGREGATE: `False`; the backend ships raw granules and
            never reduces them, so a non-`None` `aggregate=` is refused.

    Examples:
        - Construct against an injected Drive service (no network):
            ```python
            >>> from earthlens.mswep import MSWEP  # doctest: +SKIP
            >>> src = MSWEP(  # doctest: +SKIP
            ...     start="2020-04-25", end="2020-04-26",
            ...     variables=["precipitation"],
            ...     lat_lim=[-90, 90], lon_lim=[-180, 180],
            ...     temporal_resolution="daily",
            ...     folder_id="1AbC", service=fake_drive,
            ... )
            >>> src.download()  # doctest: +SKIP
            [PosixPath('2020116.nc'), PosixPath('2020117.nc')]

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"
    SUPPORTS_AGGREGATE = False

    #: Whether `_fetch` renders a progress bar. Set by `download`; a
    #: class-level default keeps `_api()` callable on its own.
    _progress: bool = True

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]] | list[str] | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "daily",
        fmt: str = "%Y-%m-%d",
        path: Path | str = "",
        *,
        product: str = "mswep",
        variant: str | None = None,
        version: str | None = None,
        credentials: MswepCredentials | None = None,
        folder_id: str | None = None,
        service: Any = None,
        catalog: Catalog | None = None,
    ) -> None:
        """Build a request against one product, version, variant and cadence.

        Args:
            start: Inclusive start of the window.
            end: Inclusive end of the window.
            variables: Variables to fetch. Defaults to `precipitation`
                for MSWEP; for MSWX each entry names a **variable
                folder** (`"Temp"`), so an n-variable request enumerates
                n granule sets.
            lat_lim: `[lat_min, lat_max]`. Advisory only — granules are
                global and GloH2O offers no server-side crop; clip with
                pyramids after download. Defaults to global.
            lon_lim: `[lon_min, lon_max]`. Advisory, as `lat_lim`.
            temporal_resolution: One of `hourly`, `3hourly`, `daily`,
                `monthly`.
            fmt: `strptime` format for `start` / `end`.
            path: Output directory.
            product: `"mswep"` or `"mswx"`.
            variant: `Past`, `Past_nogauge` or `NRT`. `None` routes each
                date to the variant whose window covers it.
            version: Catalog version key; `None` uses the product
                default.
            credentials: Where to find the Drive credential. `None`
                resolves everything from the environment.
            folder_id: Drive id of the shared folder; overrides
                `credentials.folder_id`.
            service: Pre-built Drive client, injected by the test suite.
            catalog: Catalog override; `None` loads the bundled YAML.

        Raises:
            ValueError: If `product`, `temporal_resolution` or `variant`
                is unknown, or an explicit `variant` cannot cover the
                requested window.
        """
        self._catalog = catalog or Catalog()
        self._product_key = product
        self._product = self._catalog.get_product(product)
        self._version = version
        self._variant = variant

        if variant is not None and variant not in self._product.variants:
            raise ValueError(
                f"{variant!r} is not a {product} variant. Known variants: "
                f"{sorted(self._product.variants)}."
            )

        creds = credentials or MswepCredentials()
        if folder_id is not None:
            creds = creds.model_copy(update={"folder_id": folder_id})
        self._auth = MswepAuth(creds, service=service)
        self._resolver: RootResolver | None = None
        self._folder_cache: dict[tuple[str, ...], str | None] = {}

        super().__init__(
            start=start,
            end=end,
            variables=variables if variables is not None else ["precipitation"],
            lat_lim=lat_lim if lat_lim is not None else [-90.0, 90.0],
            lon_lim=lon_lim if lon_lim is not None else [-180.0, 180.0],
            temporal_resolution=temporal_resolution,
            fmt=fmt,
            path=path,
        )

    def _initialize(self) -> Any:
        """Authenticate and return the Drive v3 client.

        Returns:
            Any: The Drive client, bound by the parent as `self.client`.

        Raises:
            AuthenticationError: When no credential or folder id
                resolves.
        """
        self._auth.configure()
        return self._auth.service

    def _create_grid(self, lat_lim: list[float], lon_lim: list[float]) -> SpatialExtent:
        """Record the requested bbox.

        GloH2O serves whole-globe granules with no server-side subsetting,
        so the extent is carried for provenance and downstream clipping
        rather than used to shape the request.

        Args:
            lat_lim: `[lat_min, lat_max]`.
            lon_lim: `[lon_min, lon_max]`.

        Returns:
            SpatialExtent: The frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Expand the window into one entry per granule timestep.

        Args:
            start: Inclusive start of the window.
            end: Inclusive end of the window.
            temporal_resolution: One of :data:`CADENCES`.
            fmt: `strptime` format tried first for a string bound.

        Returns:
            TemporalExtent: The window, with `dates` holding one entry
                per granule.

        Raises:
            ValueError: If `temporal_resolution` is not a known cadence.
        """
        accepted = {
            key: CADENCES[key] for key in self._product.resolutions if key in CADENCES
        }
        return self._cadence_extent(
            start, end, fmt=fmt, cadence=temporal_resolution, accepted=accepted
        )

    @property
    def resolver(self) -> RootResolver:
        """Return the version to root-folder resolver, built on first use."""
        if self._resolver is None:
            self._resolver = RootResolver(
                self._auth.service, self._auth.folder_id, self._catalog
            )
        return self._resolver

    def _variables(self) -> list[str]:
        """Return the requested variable keys as a flat list."""
        if isinstance(self.vars, dict):
            return [name for values in self.vars.values() for name in values]
        return list(self.vars)

    def _resolution_row(self):
        """Return the catalog row for the requested cadence.

        Raises:
            ValueError: If the cadence is not offered for this product.
        """
        try:
            return self._product.resolutions[self.temporal_resolution]
        except KeyError:
            raise ValueError(
                f"{self.temporal_resolution!r} is not a {self._product_key} "
                f"resolution. Known: {sorted(self._product.resolutions)}."
            ) from None

    def _variant_for(self, day: dt.date) -> str:
        """Return the variant serving `day`, honouring an explicit choice.

        Args:
            day: The granule's date.

        Returns:
            str: The variant key.

        Raises:
            ValueError: When an explicit variant cannot cover `day`, or
                no variant covers it at all.
        """
        if self._variant is not None:
            row = self._product.variants[self._variant]
            if row.covers(day):
                return self._variant
            better = self._product.variant_for(day)
            hint = (
                f" Use variant={better!r} for that date."
                if better
                else " No variant covers that date."
            )
            raise ValueError(
                f"variant={self._variant!r} covers "
                f"[{row.start or '-inf'}, {row.end or 'now'}], which excludes "
                f"{day.isoformat()}.{hint}"
            )
        resolved = self._product.variant_for(day)
        if resolved is None:
            raise ValueError(
                f"no {self._product_key} variant covers {day.isoformat()}. "
                f"Known variants: {sorted(self._product.variants)}."
            )
        return resolved

    def _folder_id_for(self, segments: list[str]) -> str | None:
        """Walk named folders from the share root, memoised per instance.

        Args:
            segments: Folder names to descend, starting at a share root.

        Returns:
            str | None: The leaf folder id, or `None` when any level is
                absent.
        """
        key = tuple(segments)
        if key in self._folder_cache:
            return self._folder_cache[key]
        root = self.resolver.resolve(self._product_key, self._version)
        current = root.id
        for name in segments[1:]:
            entry = find_folder(self._auth.service, current, name)
            if entry is None:
                self._folder_cache[key] = None
                return None
            current = entry.id
        self._folder_cache[key] = current
        return current

    def _plan(self) -> dict[tuple[str, ...], list[tuple[str, dt.datetime]]]:
        """Group the window's granule names by the folder holding them.

        Returns:
            dict: Folder-segment tuple to the `(file name, timestamp)`
                pairs expected inside it.
        """
        resolution = self._resolution_row()
        self._catalog.check_not_provisional(
            resolution, f"the {self._product_key} {self.temporal_resolution} folder"
        )
        root = self.resolver.resolve(self._product_key, self._version)

        requested = self._variables()
        unknown = [name for name in requested if name not in self._product.variables]
        if unknown:
            subject = (
                f"{unknown[0]!r} is not a {self._product_key} variable"
                if len(unknown) == 1
                else f"{unknown!r} are not {self._product_key} variables"
            )
            raise ValueError(f"{subject}. Known: {sorted(self._product.variables)}.")

        if self._product.needs_variable_folder:
            # MSWX shards by variable, so "no variables" selects nothing. Left
            # unguarded this returns an empty list rather than an error, which
            # is indistinguishable from "the window is not published yet".
            if not requested:
                raise ValueError(
                    f"{self._product_key} shards its granules by variable, so at "
                    "least one must be requested. Known variables: "
                    f"{sorted(self._product.variables)}."
                )
            variables: list[str | None] = list(requested)
            for name in requested:
                self._catalog.check_not_provisional(
                    self._product.variables[name],
                    f"the {self._product_key} variable folder {name!r}",
                )
        else:
            # MSWEP has no variable level; the request still names its single
            # field, and an unknown name was rejected above rather than
            # silently downloading precipitation under another label.
            variables = [None]

        grouped: dict[tuple[str, ...], list[tuple[str, dt.datetime]]] = {}
        for stamp in self.time.dates:
            day = stamp.date() if hasattr(stamp, "date") else stamp
            variant = self._variant_for(day)
            self._catalog.check_not_provisional(
                self._product.variants[variant],
                f"the {self._product_key} {variant!r} variant window",
            )
            for variable in variables:
                segments = [root.name, variant]
                if variable is not None:
                    segments.append(variable)
                segments.append(resolution.folder)
                filename = f"{stamp.strftime(resolution.stem)}.nc"
                grouped.setdefault(tuple(segments), []).append((filename, stamp))
        return grouped

    def _search(self) -> list[RemoteProduct]:
        """Resolve every expected granule to a Drive file id.

        Walks the folder chain once per `(variant, variable)` group, then
        resolves granule ids in chunked name queries. A name Drive does
        not return is logged and omitted — expected near the NRT edge,
        whose latency is about two hours.

        Returns:
            list[RemoteProduct]: One product per granule that exists,
                carrying its Drive file id and target file name.
        """
        plan = self._plan()
        total = sum(len(names) for names in plan.values())
        threshold = self._catalog.granule_warn_threshold
        if threshold and total > threshold:
            logger.warning(
                f"mswep: this request spans {total} granules. The Drive API is "
                "not built for bulk transfer and GloH2O asks non-commercial "
                "users to use rclone; consider `rclone sync` for a window this "
                "large."
            )

        products: list[RemoteProduct] = []
        for segments, entries in plan.items():
            folder_id = self._folder_id_for(list(segments))
            if folder_id is None:
                logger.warning(
                    f"mswep: folder {'/'.join(segments)} is not in the share; "
                    f"skipping its {len(entries)} granule(s)."
                )
                continue
            names = [name for name, _ in entries]
            found = find_children_by_name(self._auth.service, folder_id, names)
            # segments = (root, variant[, variable], temporal); only a gap in
            # the NRT stream is routine, so only that one is explained away.
            is_nrt = len(segments) > 1 and segments[1] == "NRT"
            reason = (
                "not published yet (NRT latency is ~2 h)"
                if is_nrt
                else "absent from the share"
            )
            for name, stamp in entries:
                entry = found.get(name)
                if entry is None:
                    logger.warning(
                        f"mswep: granule {'/'.join(segments)}/{name} is "
                        f"{reason}; skipping."
                    )
                    continue
                products.append(
                    RemoteProduct(
                        id=entry.id,
                        metadata={
                            "name": name,
                            "folder": "/".join(segments),
                            "timestamp": stamp,
                        },
                    )
                )
        return products

    def is_under_revision(self, stamp: dt.datetime, folder: str) -> bool:
        """Return whether a granule is still being revised upstream.

        GloH2O rewrites NRT granules for roughly ten days as better
        inputs land — *"Users should redownload upgraded files"* — so a
        copy already on disk inside that window is stale, not cached.
        Only the NRT stream is revised; the historical record is stable.

        Args:
            stamp: The granule's timestamp.
            folder: The `/`-joined folder chain it lives under.

        Returns:
            bool: `True` when the granule must be re-fetched even though
                it exists locally.
        """
        window = self._catalog.nrt_revision_days
        if not window or "/NRT/" not in f"/{folder}/":
            return False
        moment = stamp if isinstance(stamp, dt.datetime) else None
        if moment is None:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        age = self._now() - moment
        return age <= dt.timedelta(days=window)

    @staticmethod
    def _now() -> dt.datetime:
        """Return the current UTC time (overridden in tests)."""
        return dt.datetime.now(dt.UTC)

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download each resolved granule, mirroring the share's layout.

        Output goes to `<path>/<root>/<variant>[/<variable>]/<temporal>/`
        rather than a flat directory, because granule names are **only
        unique within their folder**: MSWEP and MSWX use the same
        `YYYYDOY.nc` stem, and every one of MSWX's ten variables repeats
        it. Flattening would make a two-variable request write both to
        one path — and the reuse check below would then return the first
        file twice, labelled as both. Mirroring also matches what
        `rclone sync` produces, so a bulk pull and a targeted one can
        share a tree.

        A granule already on disk is reused, **except** inside the NRT
        revision window, where the local copy is known to be superseded.

        Args:
            products: The plan from :meth:`_search`.

        Returns:
            list[Path]: The granules written or already present, in plan
                order.
        """
        root = self._ensure_root_dir()
        written: list[Path] = []
        for product in tqdm(products, desc="mswep", disable=not self._progress):
            name = str(product.metadata["name"])
            folder = str(product.metadata["folder"])
            destination = root.joinpath(*folder.split("/"), name)
            stamp = product.metadata["timestamp"]
            if destination.exists() and not self.is_under_revision(stamp, folder):
                logger.debug(f"mswep: {folder}/{name} already present; skipping.")
                written.append(destination)
                continue
            download_media(self._auth.service, product.id, destination)
            written.append(destination)
        return written

    def _api(self) -> list[Path]:
        """Compose `_search` + `_fetch`.

        Returns:
            list[Path]: The granules written.
        """
        return self._api_via_search_fetch()

    def download(self, progress_bar: bool = True) -> list[Path]:
        """Fetch the requested granules and return their paths.

        `aggregate=` is deliberately not a parameter: `SUPPORTS_AGGREGATE`
        is `False`, and the base class refuses a non-`None` value
        centrally while absorbing `aggregate=None` for backends that do
        not name it.

        Args:
            progress_bar: Show a per-granule progress bar.

        Returns:
            list[Path]: The granules written. Empty when nothing in the
                window is published yet.

        Raises:
            AuthenticationError: When no credential or folder id
                resolves.
        """
        self._progress = progress_bar
        warn_license(
            self._catalog.license_id,
            "mswep",
            detail=(
                "GloH2O MSWEP / MSWX is CC-BY-NC 4.0: non-commercial use only, "
                f"and every product must cite {self._catalog.attribution}"
            ),
        )
        return self._api()
