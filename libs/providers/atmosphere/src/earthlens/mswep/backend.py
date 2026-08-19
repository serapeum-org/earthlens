"""Backend that fetches GloH2O MSWEP / MSWX granules from an approved Drive share.

`MSWEP(AbstractDataSource)` downloads raw NetCDF granules from the
Google-Drive folder GloH2O shares with an approved non-commercial user.
`OUTPUT_KIND = "raster"` and `download()` returns the `list[Path]`
written — the granules are shipped as-is, and reading or regridding them
is pyramids' job, so this module never imports `xarray`.

Properties that shape the backend:

* **Two path shapes.** MSWEP is `<root>/<variant>/<temporal>/`; MSWX
  inserts a variable level, `<root>/<variant>/<variable>/<temporal>/`.
  The shape comes from the catalog's per-product `path_template`, never
  from a hard-coded f-string, so an MSWX request cannot silently build
  an MSWEP-shaped path.
* **Analysis vs forecast.** The `Past` / `Past_nogauge` / `NRT` variants
  are analysis — one granule per valid time, date-routed (`Past` ends
  2024-12-31, `NRT` starts 2025-01-01; a window straddling the cut-over
  spans both). MSWX also has ensemble **forecast** variants (`Mid`,
  `Long`) addressed by `forecast_path_template`, which adds an
  initialisation-time and an ensemble-member level:
  `<variant>/<variable>/<init>/<member>/<temporal>/<valid>.nc`. A
  forecast is requested with `variant=Mid`, `init=<date>` and
  `members=[…]`; `start` / `end` then select the valid (lead) times.
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
    LazyClientMixin,
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


def _parse_init(value: str, fmt: str) -> dt.datetime:
    """Parse a forecast initialisation time from a string.

    Accepts the request's own `fmt` first, then ISO-8601 (so a
    date-only `"2026-08-01"` works). GloH2O initialises at 00Z, so a
    date with no time defaults to midnight.

    Args:
        value: The initialisation date/time string.
        fmt: The `strptime` format the request uses for `start` / `end`.

    Returns:
        datetime.datetime: The parsed initialisation time.

    Raises:
        ValueError: When the value parses under neither `fmt` nor ISO.
    """
    for parse in (
        lambda: dt.datetime.strptime(value, fmt),
        lambda: dt.datetime.fromisoformat(value),
    ):
        try:
            return parse()
        except ValueError:
            continue
    raise ValueError(
        f"could not parse init={value!r} as a date (tried fmt={fmt!r} and ISO-8601)."
    )


def _safe_destination(root: Path, folder: str, name: str) -> Path:
    """Join a Drive-derived folder chain and file name under `root`.

    The first folder segment is the share's own root-folder name, read
    verbatim from Drive metadata; the rest and `name` are catalog-derived.
    A component of `..`, an absolute part, or one carrying a path
    separator could otherwise write outside `root`, so each is rejected
    and the resolved path is confirmed to stay under `root`.

    Args:
        root: The output directory the granule must land under.
        folder: The `/`-joined folder chain from the plan.
        name: The granule file name.

    Returns:
        Path: `root` joined with the folder chain and name.

    Raises:
        ValueError: When a component is unsafe or the result escapes
            `root`.
    """
    parts = [*folder.split("/"), name]
    for part in parts:
        if (
            part in ("", ".", "..")
            or "/" in part
            or "\\" in part
            or Path(part).is_absolute()
        ):
            raise ValueError(
                f"refusing unsafe path component {part!r} from the Drive layout."
            )
    destination = root.joinpath(*parts)
    if not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError(
            f"refusing a Drive path that escapes the output directory: {destination}."
        )
    return destination


class MSWEP(LazyClientMixin, AbstractDataSource):
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
        path: Path | str | None = None,
        *,
        product: str = "mswep",
        variant: str | None = None,
        version: str | None = None,
        init: str | None = None,
        members: list[int] | None = None,
        credentials: MswepCredentials | None = None,
        folder_id: str | None = None,
        service: Any = None,
        catalog: Catalog | None = None,
    ) -> None:
        """Build a request against one product, version, variant and cadence.

        Args:
            start: Inclusive start of the window. For a forecast variant
                this is the **valid**-time window (which lead times to
                keep), not the initialisation.
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
                `monthly` (forecast streams offer `3hourly` / `daily`).
            fmt: `strptime` format for `start` / `end`.
            path: Output directory.
            product: `"mswep"` or `"mswx"`.
            variant: `Past`, `Past_nogauge`, `NRT`, or a forecast stream
                (`Mid` / `Long`). `None` routes each date to the analysis
                variant whose window covers it; a forecast variant must
                be named explicitly.
            version: Catalog version key; `None` uses the product
                default.
            init: For a forecast variant, the initialisation time
                (`"2026-08-01"`; GloH2O initialises at 00Z). Required for
                a forecast, ignored otherwise.
            members: For a forecast variant, the ensemble members to
                fetch (`[1, 2, 3]`); `None` fetches all of them.
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
        self._members = members

        if variant is not None and variant not in self._product.variants:
            raise ValueError(
                f"{variant!r} is not a {product} variant. Known variants: "
                f"{sorted(self._product.variants)}."
            )

        self._is_forecast = (
            variant is not None and self._product.variants[variant].is_forecast
        )
        self._init: dt.datetime | None = None
        if self._is_forecast:
            if init is None:
                raise ValueError(
                    f"the forecast variant {variant!r} requires init= (the "
                    "initialisation time, e.g. '2026-08-01'). start/end then "
                    "select which valid times of that forecast to fetch."
                )
            self._init = _parse_init(init, fmt)
        elif init is not None:
            raise ValueError(
                f"init= applies only to a forecast variant; {variant!r} is an "
                "analysis variant. Drop init=, or request Mid / Long."
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

    def _initialize(self) -> None:
        """Keep construction offline; the Drive client opens lazily.

        `MSWEP` mixes in :class:`~earthlens.base.LazyClientMixin`, so
        authentication is deferred to first :attr:`client` access (via
        `download` / `search` / `authenticate`) rather than run here.
        Returns `None` so the parent binds no eager client and
        constructing the backend — or a bare `EarthLens("mswep", ...)` —
        never touches the network.
        """
        return None

    def _open_client(self) -> Any:
        """Authenticate and return the Drive v3 client (opened lazily).

        Called once, on first access to :attr:`client`, and cached by
        :class:`~earthlens.base.LazyClientMixin`.

        Returns:
            Any: The authenticated Drive v3 client.

        Raises:
            AuthenticationError: When no credential or folder id resolves.
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
        """Return the version to root-folder resolver, built on first use.

        Reading :attr:`client` first opens the Drive connection lazily
        (authenticating), so the resolver is always built on a live
        service.
        """
        if self._resolver is None:
            self._resolver = RootResolver(
                self.client, self._auth.folder_id, self._catalog
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

    def _shadowed_realtime_variant(self) -> tuple[str, str] | None:
        """Return `(chosen, alternative)` when auto-routing shadows a real-time stream.

        Auto-routing (`variant=None`) picks the first analysis variant
        whose window covers each date. When another analysis variant with
        an **open end** (`end is None`, i.e. a near-real-time stream) also
        covers that date, it is never auto-selected — so a request for
        dates only it serves (recent MSWX, absent from the auto-picked
        `Past`) resolves to nothing. This returns the auto-chosen variant
        and that shadowed real-time alternative so the caller can fail
        loud instead of returning an empty series.

        Returns:
            tuple[str, str] | None: `(chosen, alternative)`, or `None`
                when routing is unambiguous — an explicit `variant=`, or
                MSWEP's dated `Past` / `NRT` split, where no open-ended
                variant is shadowed.
        """
        if self._variant is not None:
            return None
        analysis = {
            key: row
            for key, row in self._product.variants.items()
            if not row.is_forecast
        }
        for stamp in self.time.dates:
            day = stamp.date() if hasattr(stamp, "date") else stamp
            chosen = self._product.variant_for(day)
            if chosen is None:
                continue
            for key, row in analysis.items():
                if key != chosen and row.end is None and row.covers(day):
                    return chosen, key
        return None

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

    def _validated_variables(self) -> list[str | None]:
        """Return the requested variables, validated against the catalog.

        Returns:
            list[str | None]: The variable folder names, or `[None]` for a
                product with no variable level (MSWEP).

        Raises:
            ValueError: On an unknown variable, or (for a variable-sharded
                product) an empty request — which would otherwise select
                nothing and read as "not published yet".
        """
        requested = self._variables()
        unknown = [name for name in requested if name not in self._product.variables]
        if unknown:
            subject = (
                f"{unknown[0]!r} is not a {self._product_key} variable"
                if len(unknown) == 1
                else f"{unknown!r} are not {self._product_key} variables"
            )
            raise ValueError(f"{subject}. Known: {sorted(self._product.variables)}.")

        for name in requested:
            self._catalog.check_not_provisional(
                self._product.variables[name],
                f"the {self._product_key} {name!r} variable",
            )

        if not self._product.needs_variable_folder:
            return [None]
        if not requested:
            raise ValueError(
                f"{self._product_key} shards its granules by variable, so at "
                "least one must be requested. Known variables: "
                f"{sorted(self._product.variables)}."
            )
        return list(requested)

    def _member_folders(self, count: int) -> list[str]:
        """Return the member sub-folder names (`01` … `NN`) to fetch.

        Args:
            count: The stream's ensemble size, from the catalog.

        Returns:
            list[str]: Zero-padded member folder names.

        Raises:
            ValueError: When an explicit `members=` entry is out of range.
        """
        if self._members is None:
            numbers: list[int] = list(range(1, count + 1))
        else:
            out_of_range = [m for m in self._members if m < 1 or m > count]
            if out_of_range:
                raise ValueError(
                    f"ensemble member(s) {out_of_range} are out of range 1..{count} "
                    f"for this {self._product_key} forecast stream."
                )
            numbers = list(self._members)
        return [f"{n:02d}" for n in numbers]

    def _plan(self) -> dict[tuple[str, ...], list[tuple[str, dt.datetime]]]:
        """Group the window's granule names by the folder holding them.

        Routes to the analysis or the forecast layout depending on the
        requested variant.

        Returns:
            dict: Folder-segment tuple to the `(file name, timestamp)`
                pairs expected inside it.
        """
        resolution = self._resolution_row()
        self._catalog.check_not_provisional(
            resolution, f"the {self._product_key} {self.temporal_resolution} folder"
        )
        root = self.resolver.resolve(self._product_key, self._version)
        variables = self._validated_variables()

        if self._is_forecast:
            return self._forecast_plan(root, resolution, variables)
        return self._analysis_plan(root, resolution, variables)

    def _analysis_plan(
        self, root: Any, resolution: Any, variables: list[str | None]
    ) -> dict[tuple[str, ...], list[tuple[str, dt.datetime]]]:
        """Group granules for an analysis variant (`Past` / `NRT`)."""
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

    def _forecast_plan(
        self, root: Any, resolution: Any, variables: list[str | None]
    ) -> dict[tuple[str, ...], list[tuple[str, dt.datetime]]]:
        """Group granules for a forecast variant (`Mid` / `Long`).

        A forecast granule adds an initialisation-time and an ensemble-
        member level: `<variant>/<variable>/<init>/<member>/<temporal>/`,
        with the leaf still named by **valid** time (the `start` / `end`
        window). One folder per `(variable, member)`; the window selects
        the lead times inside it.
        """
        variant = self._variant
        assert variant is not None  # guaranteed by _is_forecast
        row = self._product.variants[variant]
        self._catalog.check_not_provisional(
            row, f"the {self._product_key} {variant!r} forecast variant"
        )
        if not self._product.forecast_path_template:
            raise ValueError(
                f"{self._product_key} declares no forecast layout, so {variant!r} "
                "cannot be fetched."
            )
        assert self._init is not None  # required for a forecast in __init__
        init_str = self._init.strftime("%Y%m%d_%H")
        members = self._member_folders(row.members)

        grouped: dict[tuple[str, ...], list[tuple[str, dt.datetime]]] = {}
        for variable in variables:
            for member in members:
                for stamp in self.time.dates:
                    segments = (
                        root.name,
                        variant,
                        str(variable),
                        init_str,
                        member,
                        resolution.folder,
                    )
                    filename = f"{stamp.strftime(resolution.stem)}.nc"
                    grouped.setdefault(segments, []).append((filename, stamp))
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
            destination = _safe_destination(root, folder, name)
            stamp = product.metadata["timestamp"]
            if destination.exists() and not self.is_under_revision(stamp, folder):
                logger.debug(f"mswep: {folder}/{name} already present; skipping.")
                written.append(destination)
                continue
            download_media(self._auth.service, product.id, destination)
            written.append(destination)
        return written

    def gauge_metadata_folder(self) -> str:
        """Locate the `Gauge_metadata` folder, returning its Drive id.

        The folder sits directly under the version root — confirmed in
        the v3.16 share, alongside `Past` / `NRT`. Since `folder_id` is
        that root, this is a single child lookup.

        Returns:
            str: Drive id of the folder.

        Raises:
            FileNotFoundError: When the root does not hold it (older
                versions, e.g. v2.80, ship no gauge metadata).
        """
        folder = self._catalog.gauge_metadata.folder
        root = self.resolver.resolve(self._product_key, self._version)
        entry = find_folder(self._auth.service, root.id, folder)
        if entry is not None:
            return entry.id

        raise FileNotFoundError(
            f"{folder!r} is not in the shared folder {root.name!r}. Gauge "
            "metadata ships under the v3.16 root; older versions (e.g. v2.80) "
            "do not include it."
        )

    def fetch_gauge_metadata(self, names: list[str] | None = None) -> list[Path]:
        """Download the auxiliary gauge-metadata CSVs.

        These describe the rain gauges behind MSWEP's gauge-correction
        step — station coordinates, per-gauge date ranges, and the
        inferred reporting-time offsets. They are static and not part of
        any time series, which is why they have their own method rather
        than riding `download()`: forcing them through the date-window
        machinery would be meaningless, and `OUTPUT_KIND` stays `raster`
        for the granules.

        Files are shipped raw, like the granules. Read them with pandas.

        Args:
            names: File names to fetch; `None` fetches all five. Names
                are validated against the catalog.

        Returns:
            list[Path]: The CSVs written, under
                `<path>/Gauge_metadata/`.

        Raises:
            ValueError: When a requested name is not a catalog entry, or
                the product is not `mswep` (gauge correction is MSWEP's).
            FileNotFoundError: When the folder is not in the share.
        """
        if self._product_key != "mswep":
            raise ValueError(
                "gauge metadata describes MSWEP's gauge-correction step and is "
                f"published under MSWEP, not {self._product_key!r}."
            )

        catalogued = self._catalog.gauge_metadata.files
        wanted = list(catalogued) if names is None else list(names)
        unknown = [n for n in wanted if n not in catalogued]
        if unknown:
            raise ValueError(
                f"{unknown!r} are not gauge-metadata files. Known: "
                f"{sorted(catalogued)}."
            )

        self._auth.configure()
        folder_id = self.gauge_metadata_folder()
        found = find_children_by_name(self._auth.service, folder_id, wanted)

        destination_root = self._ensure_root_dir() / self._catalog.gauge_metadata.folder
        written: list[Path] = []
        for name in wanted:
            entry = found.get(name)
            if entry is None:
                logger.warning(
                    f"mswep: gauge-metadata file {name} is absent from the share; "
                    "skipping."
                )
                continue
            written.append(
                download_media(self._auth.service, entry.id, destination_root / name)
            )
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
            list[Path]: The granules written. For MSWEP and any explicit
                `variant=`, this is empty when nothing in the window is
                published yet. An auto-routed MSWX request raises instead
                of returning empty (see Raises): its `Past` and `NRT`
                streams overlap, so a bare empty result would hide which
                one to ask for.

        Raises:
            AuthenticationError: When no credential or folder id
                resolves.
            ValueError: When a `variant=None` request auto-routed to an
                analysis variant that served nothing, while an open-ended
                near-real-time stream (which auto-routing does not pick)
                could also cover the window — the empty result is
                ambiguous between a historical gap and recent dates that
                need `variant="NRT"`.
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
        result = self._api()
        if not result:
            shadowed = self._shadowed_realtime_variant()
            if shadowed is not None:
                chosen, alt = shadowed
                raise ValueError(
                    f"no {self._product_key} granules resolved for the requested "
                    f"window under the auto-selected {chosen!r} variant. Two "
                    f"explanations: the dates are absent from {chosen!r} (a "
                    f"historical gap), or they are recent and served by the "
                    f"near-real-time {alt!r} stream, which auto-routing does not "
                    f"pick. Pass variant={alt!r} for recent dates, or "
                    f"variant={chosen!r} to confirm a historical gap."
                )
        return result
