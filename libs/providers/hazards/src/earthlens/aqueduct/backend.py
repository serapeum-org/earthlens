"""Backend that fetches WRI Aqueduct riverine flood-risk data by admin unit.

`Aqueduct(AbstractDataSource)` downloads one WRI Aqueduct Global Flood Analyzer
(2015) shapefile from WRI's public file host (`files.wri.org`, CC-BY-4.0, no
credentials), reads it with pyramids, selects one metric / year / scenario
across the requested flood return periods, filters by unit name and/or bbox, and
returns the admin polygons carrying the selected exposure columns as a
:class:`~pyramids.feature.collection.FeatureCollection` (or, with
`geometry=False`, a geometry-dropped :class:`pandas.DataFrame`).

This is a static, country/state/basin-indexed product with no time axis, so a
missing `start` / `end` is legal (`REQUIRES_TIME_WINDOW = False`). It is a
`vector` backend: the :class:`earthlens.earthlens.EarthLens` facade rejects an
`aggregate=` argument — there is no meaningful gridded reduction of an
admin-aggregated exposure table.

Only the free **riverine** 2015 product is served here; `hazard="coastal"` (part
of the 2020 Aqueduct Floods product, which is not freely downloadable) is
rejected with a clear message. The downloaded zip is cached under `cache_dir`
(default: `aqueduct/` under the shared earthlens cache directory), so repeated
requests for the same admin level reuse it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd
import requests  # noqa: F401  # runtime seam so tests can monkeypatch this module's `requests`
from loguru import logger

from earthlens.aqueduct import _helpers
from earthlens.aqueduct.catalog import Catalog
from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.base.http import HttpClient
from earthlens.config import cache_dir as _shared_cache_dir

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

#: The one flood type the free 2015 product covers.
_SUPPORTED_HAZARD = "riverine"

#: Zip local-file-header magic; a downloaded body must start with it.
_ZIP_MAGIC = b"PK\x03\x04"

#: Global sentinel bounds — the request is admin-indexed; a narrower bbox
#: filters the returned units, a global one keeps them all.
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


class Aqueduct(AbstractDataSource):
    """WRI Aqueduct riverine flood-risk backend (vector admin-polygon output).

    Resolves one `(admin_level, metric, year, scenario, return_period)`
    selection, downloads and caches the admin level's shapefile, reads it with
    pyramids, and returns the admin polygons carrying the per-return-period
    exposure columns (`rp_<n>`). The query is a search/fetch split:
    :meth:`_search` pins the one product (admin level + resolved column names),
    and :meth:`_fetch` downloads, extracts, reads, and filters it.

    Needs no credentials. `aggregate=` is rejected — the data is an
    admin-aggregated exposure table, not a gridded raster.

    Attributes:
        OUTPUT_KIND: `"vector"` (a `FeatureCollection`) by default, or
            `"tabular"` (a `DataFrame`) when constructed with `geometry=False`.
            The facade reads it to gate `aggregate=` and to know the return
            shape.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = (
        "Aqueduct returns flood exposure aggregated per admin unit, not a gridded "
        "raster, so there is no meaningful gridded reduction. Call download() "
        "without aggregate= and post-process the returned FeatureCollection (a "
        "GeoDataFrame) directly"
    )

    #: The exposure shapefiles are a 2015 snapshot with no time axis, so a
    #: missing `start` / `end` is legal.
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
        admin_level: str = "country",
        metric: str = "population_affected",
        year: int | str = 2010,
        scenario: str = "baseline",
        return_period: int | list[int] | None = None,
        hazard: str = "riverine",
        country: str | None = None,
        geometry: bool = True,
        cache_dir: Path | str | None = None,
        timeout: float = 120.0,
    ):
        """Initialise an Aqueduct backend instance.

        Args:
            start: Accepted for signature parity; the product has no time axis,
                so `None` (the default) is normal.
            end: Accepted for signature parity; `None` is normal.
            lat_lim: `[lat_min, lat_max]` bbox latitudes; `None` keeps every
                unit. A narrower box filters the returned units.
            lon_lim: `[lon_min, lon_max]` bbox longitudes; `None` keeps all.
            temporal_resolution: Recorded as the resolution label only.
            path: Output directory for the written vector file. When omitted it
                falls back to the configured earthlens output directory
                (`set_output_dir()` / `EARTHLENS_DATA_DIR`); see
                `earthlens.config`.
            fmt: `strptime` format for `start` / `end`.
            admin_level: `"country"` (default), `"state"`, or `"basin"`.
            metric: `"gdp_affected"`, `"population_affected"` (default), or
                `"urban_damage"`.
            year: `2010` (default) or `2030`; an `int` or `str`.
            scenario: `"baseline"` (the only 2010 scenario) or one of the seven
                2030 combinations (`"ssp2-rcp8p5"`, ...).
            return_period: The flood return period(s) in years — an `int`, a
                list, or `None` (the default) for all nine (2 → 1000). Duplicates
                in a list are collapsed (output columns are keyed by return
                period).
            hazard: Only `"riverine"` is supported; `"coastal"` is part of the
                2020 product, which is not freely downloadable, and is rejected.
            country: A unit name to keep (case-insensitive exact match on
                `unit_name`). At country level this is the country name; below it
                `country=` matches the unit's own name (the state layer has an
                unused `admin` country column, the basin layer none), so use
                `lat_lim` / `lon_lim` to select a sub-national region.
            geometry: `True` (default) returns a `FeatureCollection`; `False`
                returns a geometry-dropped `DataFrame` (`OUTPUT_KIND="tabular"`).
            cache_dir: Directory for the downloaded zip. Defaults to
                `aqueduct/` under the shared earthlens cache directory
                (`set_cache_dir()` / `EARTHLENS_CACHE`), not under `path`.
            timeout: Per-request timeout in seconds for the download.

        Raises:
            ValueError: If `hazard` is not `"riverine"`, `admin_level` is
                unknown, or the metric / year / scenario / return-period
                selection is invalid (`scenario` must match `year`).
        """
        if hazard != _SUPPORTED_HAZARD:
            raise ValueError(
                f"hazard={hazard!r} is not available. This backend serves only "
                f"the free riverine 2015 product (hazard='riverine'); coastal "
                "flooding is part of the 2020 Aqueduct Floods product, whose "
                "aggregated tables are not freely downloadable."
            )
        self._catalog = Catalog()
        # Resolve/validate the admin level and the column selection up front so a
        # bad request fails at construction, not mid-download.
        self._admin_level = admin_level
        self._admin_row = self._catalog.get(admin_level)
        self._metric = metric
        self._year = str(year)
        self._scenario = scenario
        self._return_periods = self._normalise_return_periods(return_period)
        self._columns = _helpers.resolve_columns(
            self._catalog, metric, self._year, scenario, self._return_periods
        )
        self._country = country
        self._geometry = geometry
        self._timeout = timeout
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None

        # Per-instance output shape (G2): drop-geometry is a tabular DataFrame.
        self.OUTPUT_KIND = "vector" if geometry else "tabular"

        # The metric is the request axis, addressed by `metric=` (a facet), so
        # `variables` is not a facade-visible parameter — the facade then neither
        # requires nor accepts `variables=`. The base still records one.
        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=[metric],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

    def _normalise_return_periods(
        self, return_period: int | list[int] | None
    ) -> list[int]:
        """Turn the `return_period` argument into an ordered list of years.

        Args:
            return_period: An `int`, a list of ints, or `None` for all nine.

        Returns:
            list[int]: The requested return periods (years), catalog order when
                `None`.
        """
        if return_period is None:
            return sorted(self._catalog.return_periods)
        if isinstance(return_period, int):
            return [return_period]
        return list(dict.fromkeys(return_period))

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Return the base static-snapshot extent (the product has no time axis).

        Delegates to :meth:`~earthlens.base.AbstractDataSource._static_extent`,
        the shared factory for no-time-axis backends, so `start` / `end` / `fmt`
        are ignored — there is no window to parse.

        Args:
            start: Ignored (no time axis).
            end: Ignored.
            temporal_resolution: Recorded as the resolution label on the extent.
            fmt: Ignored.

        Returns:
            TemporalExtent: The frozen static extent.
        """
        return self._static_extent(resolution=temporal_resolution)

    def _search(self) -> list[RemoteProduct]:
        """Pin the one product: the admin level, its URL, and the columns.

        Returns:
            list[RemoteProduct]: A single product whose `metadata` carries the
                resolved `columns` map.
        """
        return [
            RemoteProduct(
                id=self._admin_level,
                href=self._catalog.download_url(self._admin_level),
                metadata={"columns": self._columns},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Download + read the shapefile and build the filtered collection.

        Widens the inherited `-> list[Path]` contract: a vector backend returns
        an in-memory :class:`FeatureCollection`. The download is cached under
        :attr:`cache_dir`, so a repeat request for the same admin level reuses
        the zip.

        Args:
            products: The single-element list from :meth:`_search`.

        Returns:
            list[FeatureCollection]: One trimmed, filtered collection.

        Raises:
            requests.HTTPError: If the download returns a non-2xx status.
        """
        product = products[0]
        zip_path = self._cached_zip(product)
        extract_dir = self._cache_root / "_extract" / self._admin_level
        shp_path = _helpers.extract_shapefile(zip_path, self._admin_row, extract_dir)

        from pyramids.feature.collection import FeatureCollection

        source = FeatureCollection.read_file(str(shp_path))
        columns = cast("dict[int, str]", product.metadata["columns"])
        trimmed = _helpers.build_feature_collection(source, columns)
        filtered = _helpers.filter_units(trimmed, self._country, self.space)
        return [filtered]

    @property
    def _cache_root(self) -> Path:
        """The directory holding the downloaded zips and their extracted shapefiles.

        Defaults to `aqueduct/` under the shared earthlens cache directory
        (`set_cache_dir()` / `EARTHLENS_CACHE`); overridden
        by `cache_dir`. The download and extraction land here (in the cache
        subfolder), not directly under the output `root_dir`, so a `geometry=False`
        request — which skips the GeoPackage write — leaves the output directory
        free of result files, with only the reusable cache subfolder alongside.
        """
        return self._cache_dir or (_shared_cache_dir() / "aqueduct")

    def _cached_zip(self, product: RemoteProduct) -> Path:
        """Return the local zip path, downloading it on a cache miss.

        Args:
            product: The product from :meth:`_search` (carries the download URL
                in `href`).

        Returns:
            Path: The cached zip on disk.

        Raises:
            requests.HTTPError: If the download returns a non-2xx status.
        """
        cache_dir = self._cache_root
        cache_dir.mkdir(parents=True, exist_ok=True)
        zip_name = self._admin_row.container_zip or self._admin_row.zip
        zip_path = cache_dir / zip_name
        if zip_path.exists():
            with open(zip_path, "rb") as handle:
                head = handle.read(4)
            if head == _ZIP_MAGIC:
                logger.info(f"Aqueduct: using cached {zip_name}")
                return zip_path
            logger.warning(
                f"Aqueduct: cached {zip_name} is not a valid zip "
                "(empty / truncated / foreign); re-downloading."
            )
        url = cast("str", product.href)
        logger.info(
            f"Aqueduct: downloading {zip_name} for admin level {self._admin_level!r}"
        )
        HttpClient(timeout=self._timeout).download(
            url, zip_path, expect_magic=_ZIP_MAGIC, progress=False
        )
        return zip_path

    def download(
        self,
        progress_bar: bool = True,
    ) -> FeatureCollection | pd.DataFrame:
        """Fetch the selection and return the admin units carrying the exposure.

        Issues the (cached) download, reads and filters the shapefile, writes the
        result to one vector file under `path` (when `geometry=True`), and
        returns the in-memory collection — or a geometry-dropped `DataFrame`
        when the backend was built with `geometry=False`.

        Args:
            progress_bar: Accepted for signature parity; one file is fetched, so
                this is a no-op.

        Returns:
            A :class:`~pyramids.feature.collection.FeatureCollection` (also
            written to `root_dir`) or, when `geometry=False`, a
            :class:`pandas.DataFrame` of the units and their `rp_<n>` columns.

        Raises:
            requests.HTTPError: If the download returns a non-2xx status.
        """
        collections = self._api()
        collection = collections[0]
        logger.info(
            f"Aqueduct {self._admin_level}/{self._metric}/{self._year}/"
            f"{self._scenario}: {len(collection)} unit(s)."
        )
        logger.info(
            f"Aqueduct source: {self._catalog.attribution} "
            f"(licence {self._catalog.license})."
        )
        if not self._geometry:
            frame = pd.DataFrame(collection.drop(columns=collection.geometry.name))
            if frame.empty:
                logger.warning("Aqueduct: no unit matched the request (empty table).")
            return frame
        if not len(collection):
            logger.warning("Aqueduct: no unit matched the request; nothing written.")
            return collection
        out_path = self._write(collection)
        logger.info(f"Aqueduct: wrote {len(collection)} unit(s) to {out_path}")
        return collection

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the collection to one GeoPackage under `root_dir`.

        The filename embeds the selection so distinct requests do not overwrite
        one another.

        Args:
            collection: The collection to write.

        Returns:
            Path: The written file path.
        """
        return_periods = "-".join(str(rp) for rp in self._return_periods)
        country_tag = ""
        if self._country:
            # Slugify to keep the filename path-safe (a unit name may carry
            # spaces, punctuation, or a slash, e.g. "Gulf of Mexico, North ...").
            slug = "".join(
                ch if ch.isalnum() else "-" for ch in self._country.strip()
            ).strip("-")
            country_tag = f"_{slug}"
        bbox_tag = ""
        if not _helpers._is_global(self.space):
            box = self.space
            bbox_tag = (
                f"_bbox{box.latitude_min:g}_{box.longitude_min:g}_"
                f"{box.latitude_max:g}_{box.longitude_max:g}"
            )
        stem = (
            f"aqueduct_{self._admin_level}_{self._metric}_{self._year}_"
            f"{self._scenario}_rp{return_periods}{country_tag}{bbox_tag}"
        )
        out_path = self.root_dir / f"{stem}.gpkg"
        collection.to_file(str(out_path), driver="GPKG")
        return out_path
