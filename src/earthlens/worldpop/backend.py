"""WorldPop backend — open population data hub over anonymous HTTPS.

`WorldPop` is a download-and-localise backend (`OUTPUT_KIND="mixed"`). A
request is an AOI (ISO3 / bbox / `GeoDataFrame`) + time window + a list of
WorldPop **product aliases** (`variables=["pop", ...]`, canonical or
friendly) plus `constrained` / `unadjusted` / `resolution` / `scope` /
`generation` / `year(s)` / `crs` / `api` selectors. The backend resolves
each product to a concrete REST sub-alias against the bundled catalog,
queries the WorldPop REST API for the matching per-country GeoTIFF URLs,
downloads them over anonymous HTTPS, and uses `pyramids` to mosaic + crop
(and reproject only when `crs != 4326`) — writing population GeoTIFFs and,
for demographic products (`age_structures`), per-cohort rasters plus a tidy
age/sex table.

The provider is **open, CC-BY-4.0** — no credentials (see
`earthlens.worldpop.auth`). The default `api="rest"` path needs only the
core dependencies (`requests` + `pyramids`); the optional
`api="worldpoppy"` path imports `worldpoppy` lazily and consumes only its
**file cache** (never its `xarray` return), so the package imports without
the `[worldpop]` extra.
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
from earthlens.worldpop._helpers import (
    epsg_int,
    iso3_for_bbox,
    load_iso3_bbox,
    normalise_iso3,
)
from earthlens.worldpop.auth import WorldPopAuth
from earthlens.worldpop.catalog import GENERATIONS, Catalog

#: Allowed values for the `api=` access-path selector.
_API_MODES: frozenset[str] = frozenset({"rest", "worldpoppy"})
#: Allowed values for the `resolution=` selector.
_RESOLUTIONS: frozenset[str] = frozenset({"100m", "1km"})
#: Allowed values for the `scope=` selector.
_SCOPES: frozenset[str] = frozenset({"countries", "global"})


class WorldPop(AbstractDataSource):
    """Download WorldPop population + demographic products, localised via pyramids.

    Attributes:
        OUTPUT_KIND: Fixed `"mixed"` — population products yield GeoTIFFs
            and demographic products (`age_structures`) additionally yield a
            tidy age/sex table, so the facade forwards `aggregate=`.
    """

    OUTPUT_KIND: OutputKind = "mixed"

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
        aoi: str | list[str] | list[float] | object | None = None,
        constrained: bool = False,
        unadjusted: bool = True,
        resolution: str = "100m",
        scope: str = "countries",
        generation: str = "R2021",
        year: int | None = None,
        years: list[int] | None = None,
        crs: str = "EPSG:4326",
        api: str = "rest",
        catalog: Catalog | None = None,
    ):
        """Initialise a WorldPop backend instance.

        Resolves and statically validates every requested product +
        sub-alias selector against the catalog **before** the parent
        constructor runs (the parent calls `_initialize` first). The AOI is
        resolved to a set of ISO3 codes here too, since `_initialize`
        receives no bbox.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`);
                its year selects the first WorldPop year in range.
            end: Inclusive end of the date window.
            variables: WorldPop product keys — canonical (`"pop"`) or
                friendly aliases (`"population"`, `"age_sex"`, …).
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label only (WorldPop years are
                annual points); accepted for facade parity. Defaults to
                `"yearly"`.
            path: Output directory. Created by the parent class.
            fmt: `strptime` format for `start` / `end`.
            aoi: Explicit AOI — an ISO3 string, a list of ISO3 strings, a
                `[w, s, e, n]` bbox, or a `GeoDataFrame`. `None` (default)
                derives the ISO3 set from `lat_lim` / `lon_lim`.
            constrained: Settlement-masked *constrained* variant (`True`)
                vs *unconstrained* (`False`, default).
            unadjusted: Raw variant (`True`, default → `wpgp`) vs the
                UN-adjusted variant (`False` → `wpgpunadj`).
            resolution: `"100m"` (default) or `"1km"`.
            scope: `"countries"` (per-ISO3, default) or `"global"` (the
                global mosaic, where the product offers one).
            generation: Product generation — `"R2021"` (default, the
                classic 2000–2020 line) or a Global-2 line (`"R2025A"`, …).
            year: A single year to fetch (overrides the date window).
            years: An explicit list of years (overrides the date window
                and `year`).
            crs: Output CRS as an EPSG string / code (default
                `"EPSG:4326"` — WorldPop's native CRS, so no reproject).
            api: Access path — `"rest"` (default; direct REST + pyramids,
                no optional SDK) or `"worldpoppy"` (the optional SDK via its
                file cache).
            catalog: Optional pre-built `Catalog` (tests inject a faked
                one); defaults to the bundled catalog.

        Raises:
            ValueError: When `variables` is empty, a product / alias is
                unknown, the selector tuple matches no sub-alias, or
                `resolution` / `scope` / `generation` / `api` is malformed.
            ImportError: When `api="worldpoppy"` but the `[worldpop]` extra
                is not installed.
        """
        if not variables:
            raise ValueError(
                "WorldPop requires a non-empty `variables` list of product keys, "
                'e.g. ["pop"] or ["population"].'
            )
        if api not in _API_MODES:
            raise ValueError(f"api must be one of {sorted(_API_MODES)}; got {api!r}.")
        if resolution not in _RESOLUTIONS:
            raise ValueError(
                f"resolution must be one of {sorted(_RESOLUTIONS)}; got {resolution!r}."
            )
        if scope not in _SCOPES:
            raise ValueError(f"scope must be one of {sorted(_SCOPES)}; got {scope!r}.")
        if generation not in GENERATIONS:
            raise ValueError(
                f"generation must be one of {list(GENERATIONS)}; got {generation!r}."
            )
        if api == "worldpoppy":
            _require_worldpoppy()

        self._catalog = catalog if catalog is not None else Catalog()
        self._constrained = constrained
        self._unadjusted = unadjusted
        self._resolution = resolution
        self._scope = scope
        self._generation = generation
        self._year_arg = year
        self._years_arg = years
        self._crs = crs
        self._output_epsg = epsg_int(crs)
        self._api_mode = api
        self._auth = WorldPopAuth()
        self._aggregate_cfg = None
        self._show_progress = True

        # Resolve + statically validate (product + selector → sub-alias) up
        # front; year validation happens once the year list is derived.
        self._products: list[str] = [self._catalog.resolve(v) for v in variables]
        self._subalias_ids: dict[str, str] = {
            product: self._catalog.pick_subalias(
                product,
                constrained=constrained,
                unadjusted=unadjusted,
                resolution=resolution,
                scope=scope,
                generation=generation,
            )
            for product in self._products
        }
        self._iso3s: list[str] = self._resolve_aoi(aoi, lat_lim, lon_lim)

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

    def _resolve_aoi(
        self,
        aoi: str | list[str] | list[float] | object | None,
        lat_lim: list[float],
        lon_lim: list[float],
    ) -> list[str]:
        """Resolve the AOI to a sorted list of ISO3 country codes.

        Args:
            aoi: An ISO3 string, a list of ISO3 strings, a `[w, s, e, n]`
                bbox, a `GeoDataFrame`, or `None` (derive from the bbox).
            lat_lim: `[lat_min, lat_max]` used when `aoi` is `None`.
            lon_lim: `[lon_min, lon_max]` used when `aoi` is `None`.

        Returns:
            list[str]: The intersecting / requested ISO3 codes, sorted.
        """
        if isinstance(aoi, str):
            return [normalise_iso3(aoi)]
        if isinstance(aoi, list) and aoi and isinstance(aoi[0], str):
            return sorted({normalise_iso3(code) for code in aoi})
        if isinstance(aoi, list) and len(aoi) == 4 and isinstance(aoi[0], (int, float)):
            bbox = [float(x) for x in aoi]
        elif aoi is not None and hasattr(aoi, "total_bounds"):
            bbox = [float(x) for x in aoi.total_bounds]  # a GeoDataFrame
        else:
            bbox = [lon_lim[0], lat_lim[0], lon_lim[1], lat_lim[1]]
        return iso3_for_bbox(bbox, load_iso3_bbox())

    def _initialize(self):
        """Configure the (no-op) auth; no network client is created.

        Returns:
            None: WorldPop is open + anonymous, so the parent binds no
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

        The window's years select the WorldPop years in range; the
        per-year expansion happens in `_years`.

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

    def _years(self) -> list[int]:
        """Return the explicit years to fetch (from `years` / `year` / window).

        Returns:
            list[int]: Sorted, de-duplicated years. `years=` wins, then
                `year=`, else every year spanned by `start`/`end`.
        """
        if self._years_arg is not None:
            return sorted({int(y) for y in self._years_arg})
        if self._year_arg is not None:
            return [int(self._year_arg)]
        return sorted({d.year for d in self.time.dates})

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical search/fetch shape."""
        return self._api_via_search_fetch()

    def download(self, progress_bar: bool = True, aggregate=None) -> list[Path]:
        """Fetch the requested products as AOI-cropped GeoTIFFs (+ tables).

        Args:
            progress_bar: Whether per-download progress is shown.
            aggregate: Optional `earthlens.aggregate.AggregationConfig`;
                reduces the per-year raster stack across years (`C6`).

        Returns:
            list[Path]: The written GeoTIFF / table paths.
        """
        self._show_progress = progress_bar
        self._aggregate_cfg = aggregate
        return self._api_via_search_fetch()


def _require_worldpoppy() -> None:
    """Import-check the optional `worldpoppy` SDK, with a friendly error.

    Raises:
        ImportError: If `worldpoppy` is not installed, naming the
            `earthlens[worldpop]` extra.
    """
    try:
        import worldpoppy  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "api='worldpoppy' needs the optional WorldPopPy SDK. "
            "Install it with: pip install earthlens[worldpop]"
        ) from exc
