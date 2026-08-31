"""Backend that fetches FLOPROS flood-protection standards by subnational unit.

`FLOPROS(AbstractDataSource)` downloads the FLOPROS global shapefile (Scussolini
et al., 2016) from the NHESS-2016 paper's public supplement zip
(`nhess.copernicus.org`, CC-BY-3.0, no credentials), reads it with pyramids,
selects the requested protection layer(s), filters by unit name and/or bbox,
and returns the ~4650 subnational polygons carrying the selected
protection-standard columns as a
:class:`~pyramids.feature.collection.FeatureCollection` (or, with
`geometry=False`, a geometry-dropped :class:`pandas.DataFrame`).

Each polygon's protection standard is a return period in years across the
FLOPROS layers — Modelled (riverine), Merged (the recommended combined riverine
standard), and the Design / Policy layers (min & max, riverine + coastal). This
is the defended-vs-undefended correction: clip a flood-hazard map by the local
protection standard to separate protected from exposed areas.

This is a static product with no time axis, so a missing `start` / `end` is
legal (`REQUIRES_TIME_WINDOW = False`). It is a `vector` backend: the
:class:`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument —
there is no meaningful gridded reduction of an admin-aggregated protection
table. The downloaded zip is cached under `cache_dir`, so repeated requests
reuse it.
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
)
from earthlens.base.http import HttpClient
from earthlens.config import cache_dir as _shared_cache_dir
from earthlens.flopros import _helpers
from earthlens.flopros.catalog import Catalog

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

#: Zip local-file-header magic; a downloaded body must start with it.
_ZIP_MAGIC = b"PK\x03\x04"

#: Global sentinel bounds — the request is admin-indexed; a narrower bbox
#: filters the returned units, a global one keeps them all.
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


class FLOPROS(AbstractDataSource):
    """FLOPROS flood-protection-standard backend (vector admin-polygon output).

    Downloads and caches the single FLOPROS shapefile, reads it with pyramids,
    selects the requested protection layer(s), filters by unit name and/or bbox,
    and returns the subnational polygons carrying the selected protection
    standards (return periods, years). Needs no credentials. `aggregate=` is
    rejected — the data is an admin-aggregated table, not a gridded raster.

    Attributes:
        OUTPUT_KIND: `"vector"` (a `FeatureCollection`) by default, or
            `"tabular"` (a `DataFrame`) when constructed with `geometry=False`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = (
        "FLOPROS returns flood-protection standards aggregated per admin unit, "
        "not a gridded raster, so there is no meaningful gridded reduction. Call "
        "download() without aggregate= and post-process the returned "
        "FeatureCollection (a GeoDataFrame) directly"
    )

    #: The protection shapefile is a static snapshot with no time axis, so a
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
        layer: str | list[str] | None = None,
        country: str | None = None,
        geometry: bool = True,
        cache_dir: Path | str | None = None,
        timeout: float = 120.0,
    ):
        """Initialise a FLOPROS backend instance.

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
            layer: The FLOPROS protection layer(s) to keep — a public layer name
                (`"merged_riverine"`), a list of them, or `None` (the default)
                for every layer.
            country: A unit name to keep (case-insensitive exact match on `name`
                or `geonunit`).
            geometry: `True` (default) returns a `FeatureCollection`; `False`
                returns a geometry-dropped `DataFrame` (`OUTPUT_KIND="tabular"`).
            cache_dir: Directory for the downloaded zip. Defaults to
                `flopros/` under the shared earthlens cache directory
                (`set_cache_dir()` / `EARTHLENS_CACHE`), not under `path`.
            timeout: Per-request timeout in seconds for the download.

        Raises:
            ValueError: If a requested `layer` is not a FLOPROS layer.
        """
        self._catalog = Catalog()
        self._dataset = self._catalog.get("flopros")
        # Resolve/validate the layer selection up front so a bad request fails at
        # construction, not mid-download.
        self._layers = _helpers.resolve_layers(self._dataset, layer)
        self._country = country
        self._geometry = geometry
        self._timeout = timeout
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None

        # Per-instance output shape: drop-geometry is a tabular DataFrame.
        self.OUTPUT_KIND = "vector" if geometry else "tabular"

        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=list(self._layers),
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
        """Return the base static-snapshot extent (the product has no time axis).

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
        """Pin the one product: the shapefile URL and the resolved layers.

        Returns:
            list[RemoteProduct]: A single product whose `metadata` carries the
                resolved `layers` map.
        """
        return [
            RemoteProduct(
                id="flopros",
                href=self._dataset.url,
                metadata={"layers": self._layers},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Download + read the shapefile and build the filtered collection.

        Widens the inherited `-> list[Path]` contract: a vector backend returns
        an in-memory :class:`FeatureCollection`. The download is cached under
        :attr:`_cache_root`, so a repeat request reuses the zip.

        Args:
            products: The single-element list from :meth:`_search`.

        Returns:
            list[FeatureCollection]: One trimmed, filtered collection.

        Raises:
            requests.HTTPError: If the download returns a non-2xx status.
        """
        product = products[0]
        zip_path = self._cached_zip(product)
        extract_dir = self._cache_root / "_extract"
        shp_path = _helpers.extract_shapefile(
            zip_path, self._dataset.shapefile_stem, extract_dir
        )

        from pyramids.feature.collection import FeatureCollection

        source = FeatureCollection.read_file(str(shp_path))
        layers = cast("dict[str, str]", product.metadata["layers"])
        trimmed = _helpers.build_feature_collection(
            source, self._dataset.identity_columns, layers
        )
        filtered = _helpers.filter_units(trimmed, self._country, self.space)
        return [filtered]

    @property
    def _cache_root(self) -> Path:
        """The directory holding the downloaded zip and its extracted shapefile.

        Defaults to `flopros/` under the shared earthlens cache directory
        (`set_cache_dir()` / `EARTHLENS_CACHE`); overridden
        by `cache_dir`. The download and extraction land here, not directly under
        the output `root_dir`, so a `geometry=False` request — which skips the
        GeoPackage write — leaves the output directory free of result files.
        """
        return self._cache_dir or (_shared_cache_dir() / "flopros")

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
        zip_path = cache_dir / "flopros_supplement.zip"
        if zip_path.exists():
            with open(zip_path, "rb") as handle:
                head = handle.read(4)
            if head == _ZIP_MAGIC:
                logger.info("FLOPROS: using cached supplement zip")
                return zip_path
            logger.warning(
                "FLOPROS: cached zip is not a valid zip "
                "(empty / truncated / foreign); re-downloading."
            )
        url = cast("str", product.href)
        logger.info("FLOPROS: downloading the NHESS supplement zip")
        HttpClient(timeout=self._timeout).download(
            url, zip_path, expect_magic=_ZIP_MAGIC, progress=False
        )
        return zip_path

    def download(
        self,
        progress_bar: bool = True,
    ) -> FeatureCollection | pd.DataFrame:
        """Fetch the selection and return the units carrying their protection standards.

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
            :class:`pandas.DataFrame` of the units and their protection-standard
            columns.

        Raises:
            requests.HTTPError: If the download returns a non-2xx status.
        """
        collections = self._api()
        collection = collections[0]
        logger.info(f"FLOPROS {sorted(self._layers)}: {len(collection)} unit(s).")
        logger.info(
            f"FLOPROS source: {self._catalog.attribution} "
            f"(licence {self._catalog.license})."
        )
        if not self._geometry:
            frame = pd.DataFrame(collection.drop(columns=collection.geometry.name))
            if frame.empty:
                logger.warning("FLOPROS: no unit matched the request (empty table).")
            return frame
        if not len(collection):
            logger.warning("FLOPROS: no unit matched the request; nothing written.")
            return collection
        out_path = self._write(collection)
        logger.info(f"FLOPROS: wrote {len(collection)} unit(s) to {out_path}")
        return collection

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the collection to one GeoPackage under `root_dir`.

        The filename embeds the selected layers (and a `country` / bbox tag)
        so distinct requests do not overwrite one another.

        Args:
            collection: The collection to write.

        Returns:
            Path: The written file path.
        """
        layer_tag = "-".join(sorted(self._layers)) or "all"
        country_tag = ""
        if self._country:
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
        stem = f"flopros_{layer_tag}{country_tag}{bbox_tag}"
        out_path = self.root_dir / f"{stem}.gpkg"
        collection.to_file(str(out_path), driver="GPKG")
        return out_path
