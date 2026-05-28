"""GHSL backend — JRC Global Human Settlement Layer over open HTTPS.

`GHSL` is a download-and-localise raster backend (`OUTPUT_KIND="raster"`).
A request is a bbox + time window + a list of GHSL **product keys**
(`variables=["GHS_POP", ...]`, canonical or friendly aliases) plus
`release` / `epoch(s)` / `resolution` / `crs` / `tiling` / `api` kwargs. The
backend resolves the matching epochs against the bundled availability
catalog, builds the deterministic JRC `.zip` URL(s) (or STAC-searches),
downloads the intersecting Mollweide tiles (or the whole-globe file) over
anonymous HTTPS, then uses `pyramids` to mosaic, reproject (Mollweide →
the output CRS), and crop to the AOI — writing one GeoTIFF per
`(product, epoch)`.

The provider is **open, attribution-only** — no credentials (see
`earthlens.ghsl.auth`). The GIS work happens locally in `pyramids`, so this
is a genuine pyramids-consuming backend.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from earthlens.base import OutputKind
from earthlens.base.abstractdatasource import (
    AbstractDataSource,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.ghsl.auth import GhslAuth
from earthlens.ghsl.catalog import Catalog

#: Allowed values for the `api=` access-path selector.
_API_MODES: frozenset[str] = frozenset({"direct", "stac"})
#: Allowed values for the `tiling=` selector.
_TILING_MODES: frozenset[str] = frozenset({"auto", "global"})


class GHSL(AbstractDataSource):
    """Download GHSL products from the JRC over open HTTPS, localised via pyramids.

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; every product yields a gridded
            GeoTIFF, so the facade forwards `aggregate=` (reduced across
            epochs).
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "yearly",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        *,
        release: str = "R2023A",
        epoch: int | None = None,
        epochs: list[int] | None = None,
        resolution: str | None = None,
        crs: str = "EPSG:4326",
        tiling: str = "auto",
        api: str = "direct",
        catalog: Catalog | None = None,
    ):
        """Initialise a GHSL backend instance.

        Resolves and statically validates every requested product against
        the availability catalog **before** the parent constructor runs
        (the parent calls `_initialize` first, where `self.vars` is not yet
        set). Epoch validation is deferred until the epoch list is derived
        from the date window.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`);
                its year selects the first GHSL epoch in range.
            end: Inclusive end of the date window.
            variables: GHSL product keys — canonical (`"GHS_POP"`) or
                friendly aliases (`"population"`, `"settlement_model"`, …).
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label only (GHSL epochs are
                5-yearly points); accepted for facade parity. Defaults to
                `"yearly"`.
            path: Output directory. Created by the parent class.
            fmt: `strptime` format for `start` / `end`.
            release: GHSL release id — `"R2023A"` (default), `"R2022A"`
                (LAND), or `"R2025A"` (WUP projections).
            epoch: A single reference year to fetch (overrides the date
                window). Mutually informative with `epochs`.
            epochs: An explicit list of reference years (overrides the date
                window and `epoch`).
            resolution: Friendly resolution label (`"100m"`, `"1km"`,
                `"3ss"`, `"30ss"`, `"10m"`). `None` uses each product's
                `default_resolution`.
            crs: Output CRS as an EPSG string / code (default `"EPSG:4326"`).
                The source CRS is implied by `resolution`; the backend
                reprojects to this when they differ.
            tiling: `"auto"` (tile-select + mosaic for fine resolutions) or
                `"global"` (always download the whole-globe file).
            api: Access path — `"direct"` (deterministic URL builder,
                default) or `"stac"` (JRC STAC search, when available).
            catalog: Optional pre-built `Catalog` (tests inject a faked
                one); defaults to the bundled catalog.

        Raises:
            ValueError: When `variables` is empty, a product / alias is
                unknown, the release or resolution is unavailable for a
                product, or `crs` / `tiling` / `api` is malformed.
        """
        if not variables:
            raise ValueError(
                "GHSL requires a non-empty `variables` list of product keys, "
                'e.g. ["GHS_POP"] or ["population"].'
            )
        if api not in _API_MODES:
            raise ValueError(
                f"api must be one of {sorted(_API_MODES)}; got {api!r}."
            )
        if tiling not in _TILING_MODES:
            raise ValueError(
                f"tiling must be one of {sorted(_TILING_MODES)}; got {tiling!r}."
            )

        self._catalog = catalog if catalog is not None else Catalog()
        self._release = release
        self._epoch_arg = epoch
        self._epochs_arg = epochs
        self._resolution_arg = resolution
        self._crs = crs
        self._output_epsg = _epsg_int(crs)
        self._tiling = tiling
        self._api = api
        self._auth = GhslAuth()
        # Resolve + statically validate (product / release / resolution) up
        # front; epoch validation happens once the epoch list is derived.
        self._codes: list[str] = [self._catalog.resolve(v) for v in variables]
        self._validate_static()

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

    def _validate_static(self) -> None:
        """Check release + resolution availability for every product.

        Epoch validation is deferred (the epoch list is not known until the
        date window is parsed); this catches the static dimensions early so
        a bad `release` / `resolution` fails at construction.

        Raises:
            ValueError: If a product has no such release, or the requested
                (or its default) resolution is not available for it.
        """
        for code in self._codes:
            product = self._catalog.get(code)
            if self._release not in product.releases:
                raise ValueError(
                    f"{code} has no release {self._release!r}; "
                    f"available releases: {sorted(product.releases)}."
                )
            avail = product.releases[self._release]
            resolution = self._resolution_for(code)
            if resolution not in avail.resolutions:
                raise ValueError(
                    f"{code} ({self._release}) has no resolution "
                    f"{resolution!r}; available: {avail.resolutions}."
                )

    def _resolution_for(self, code: str) -> str:
        """Return the resolution to use for one product.

        Args:
            code: A canonical product code.

        Returns:
            str: The explicit `resolution=` if given, else the product's
                `default_resolution`.

        Raises:
            ValueError: If neither is available (the product declares no
                default and the request omitted `resolution=`).
        """
        if self._resolution_arg is not None:
            return self._resolution_arg
        default = self._catalog.get(code).default_resolution
        if default is None:
            raise ValueError(
                f"{code} declares no default_resolution; pass resolution=."
            )
        return default

    def _initialize(self):
        """Configure the (no-op) auth; no network client is created.

        Returns:
            None: GHSL is open + anonymous, so the parent binds no
                `self.client`.
        """
        self._auth.configure()
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a `SpatialExtent`.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox (WGS84).
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the date window into a `TemporalExtent`.

        The window's years select the GHSL epochs in range (the discrete
        5-yearly steps); the per-epoch expansion happens in `_epochs`.

        Args:
            start: Inclusive start of the window.
            end: Inclusive end of the window.
            temporal_resolution: Advisory label (ignored).
            fmt: `strptime` format applied to `start` / `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        dates = pd.date_range(start_dt, end_dt, freq="YS")
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="YS",
            dates=dates,
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(self, progress_bar: bool = True, aggregate=None) -> list[Path]:
        """Fetch the requested products as AOI-cropped GeoTIFFs.

        Args:
            progress_bar: Whether per-download progress is shown.
            aggregate: Optional `earthlens.aggregate.AggregationConfig`;
                reduces the per-epoch raster stack (`C6`).

        Returns:
            list[Path]: One GeoTIFF per `(product, epoch)`, or — when
                `aggregate` is set — the per-window reduced rasters.
        """
        self._show_progress = progress_bar
        self._aggregate_cfg = aggregate
        return self._api_via_search_fetch()


def _epsg_int(crs: str | int) -> int:
    """Parse an EPSG string / code to its integer code.

    Args:
        crs: An EPSG code as `4326`, `"4326"`, or `"EPSG:4326"`
            (case-insensitive).

    Returns:
        int: The numeric EPSG code.

    Raises:
        ValueError: If `crs` is not a recognised EPSG code.

    Examples:
        - Accepts the common spellings:
            ```python
            >>> from earthlens.ghsl.backend import _epsg_int
            >>> _epsg_int("EPSG:4326")
            4326
            >>> _epsg_int(3035)
            3035

            ```
    """
    if isinstance(crs, int):
        return crs
    text = str(crs).strip().upper()
    if text.startswith("EPSG:"):
        text = text[len("EPSG:") :]
    try:
        return int(text)
    except ValueError:
        raise ValueError(
            f"could not parse {crs!r} as an EPSG code "
            "(expected e.g. 4326 or 'EPSG:4326')."
        ) from None
