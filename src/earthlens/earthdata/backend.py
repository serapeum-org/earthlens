"""Backend that fetches NASA Earthdata granules via earthaccess + CMR.

`EarthData(AbstractDataSource)` accepts the same constructor surface
as the other earthlens backends — `start`, `end`, `variables`,
`lat_lim`, `lon_lim`, `temporal_resolution`, `path` — plus a few
backend-specific kwargs for authentication, DAAC disambiguation, and
the in-region S3 streaming choice. Each `(dataset_key, [band, ...])`
pair in the `variables` mapping names one curated collection to search
on CMR and fetch from its DAAC.

**This backend's `OUTPUT_KIND` is per-instance, not fixed (`G1`).**
Earthdata spans gridded raster (GPM IMERG, MUR SST), point/profile
vector (GEDI L4A, ICESat-2), and tabular (some ORNL CSV). Every other
earthlens backend fixes one `OUTPUT_KIND` as a class attribute; this
one resolves the requested dataset row(s) in `__init__` and copies the
row's `output_kind` onto `self.OUTPUT_KIND`. The
:class:`earthlens.earthlens.EarthLens` facade reads that per-instance
value at `download()` time to gate `aggregate=` (`G6`): forwarded for
`"raster"`, rejected for `"vector"` / `"tabular"`.

A single request may name several datasets, but they must all share
one `output_kind` — a mixed raster+vector request is rejected at
construction, since one instance carries exactly one `OUTPUT_KIND`.

The MVP fetches **whole native granules** (`G5`): HTTPS via
`earthaccess.download` (the safe default off-cloud) or in-region S3
streaming via `earthaccess.open` when the collection is cloud-hosted
and the caller runs in `us-west-2` (`G4`). Server-side bbox / band
subsetting (Harmony) and ASF's richer search are deferred; the bands
in `variables` are informational for a whole-granule fetch.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.earthdata.auth import EarthdataAuth, EarthdataCredentials
from earthlens.earthdata.catalog import Catalog, EarthdataDataset

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig


class EarthData(AbstractDataSource):
    """NASA Earthdata backend (per-dataset output kind).

    Wraps `earthaccess` + CMR so a user can search a curated NASA
    EOSDIS collection by bbox + window and fetch its native granules
    through the same `download()` shape every other earthlens backend
    uses. One Earthdata Login authenticates across every DAAC.

    Attributes:
        OUTPUT_KIND: Class default `"raster"`, **overridden per
            instance** in :meth:`__init__` from the resolved dataset
            row's `output_kind` (`G1`). The facade reads this instance
            value to gate `aggregate=`.
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        daac: str | None = None,
        region: str | None = None,
        direct_s3: str = "auto",
        username: str | None = None,
        password: str | None = None,
        netrc_path: Path | str | None = None,
    ):
        """Initialise an Earthdata backend instance.

        Resolves every requested dataset key against the catalog
        **before** calling the parent constructor, so the per-instance
        :attr:`OUTPUT_KIND` is set from the resolved row(s). The parent
        `__init__` runs :meth:`_initialize` first (EDL login), so the
        resolution cannot live there — `self.vars` is not yet set at
        that point.

        Args:
            start: Inclusive start date as a string (parsed with
                `fmt`).
            end: Inclusive end date as a string.
            variables: Mapping from curated dataset key to a list of
                band names, e.g. `{"GPM_3IMERGHHL_07":
                ["precipitation"]}`. Bands are informational for the
                whole-granule fetch the MVP performs.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory cadence label. Defaults to
                `"daily"`.
            path: Output directory. Created by the parent class if it
                does not exist.
            fmt: `strptime` format for `start` / `end`. Defaults to
                `"%Y-%m-%d"`.
            daac: Optional DAAC id to disambiguate a dataset key whose
                `short_name` is served by more than one provider.
            region: AWS region the caller runs in (e.g.
                `"us-west-2"`). Falls back to `AWS_REGION` /
                `AWS_DEFAULT_REGION`. Used by the in-region S3 path
                (`G4`).
            direct_s3: `"auto"` (default) streams from S3 only when the
                collection is cloud-hosted and the caller is in
                `us-west-2`; `"never"` always uses HTTPS download;
                `"always"` forces the S3 path.
            username: EDL username. Falls back to `EARTHDATA_USERNAME`,
                then `~/.netrc`, then an interactive prompt.
            password: EDL password. Falls back to
                `EARTHDATA_PASSWORD`, then `~/.netrc`.
            netrc_path: Optional path to a `.netrc` file holding a
                `urs.earthdata.nasa.gov` entry.

        Raises:
            ValueError: When `variables` is empty, a dataset key is
                unknown, or the requested datasets do not all share one
                `output_kind`.
        """
        self._daac = daac
        self._region = region
        self._direct_s3 = direct_s3
        self._username = username
        self._password = password
        self._netrc_path = Path(netrc_path) if netrc_path is not None else None
        self._auth: EarthdataAuth | None = None

        self._catalog = Catalog()
        self._datasets: list[EarthdataDataset] = self._resolve_datasets(variables)
        self.OUTPUT_KIND = self._unify_output_kind(self._datasets)

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

    def _resolve_datasets(
        self, variables: dict[str, list[str]]
    ) -> list[EarthdataDataset]:
        """Resolve every requested dataset key to a catalog row.

        Args:
            variables: The `{dataset_key: [band, ...]}` request.

        Returns:
            list[EarthdataDataset]: One row per key, in request order.

        Raises:
            ValueError: When `variables` is empty or a key is unknown
                (the catalog's did-you-mean is surfaced in the message).
        """
        if not variables:
            raise ValueError(
                "EarthData requires a non-empty `variables` mapping of "
                "{dataset_key: [band, ...]}."
            )
        resolved: list[EarthdataDataset] = []
        for key in variables:
            try:
                resolved.append(self._catalog.resolve(key, daac=self._daac))
            except KeyError as exc:
                raise ValueError(str(exc)) from exc
        return resolved

    @staticmethod
    def _unify_output_kind(datasets: list[EarthdataDataset]) -> OutputKind:
        """Return the single `output_kind` shared by every requested row.

        A backend instance carries exactly one :attr:`OUTPUT_KIND`, so
        a request mixing (say) a raster and a vector dataset is
        ambiguous and rejected here rather than silently honouring the
        first row's kind.

        Args:
            datasets: The resolved dataset rows.

        Returns:
            OutputKind: The shared `output_kind`.

        Raises:
            ValueError: When the rows do not all share one
                `output_kind`.
        """
        kinds = {ds.output_kind for ds in datasets}
        if len(kinds) > 1:
            detail = ", ".join(
                f"{ds.short_name}={ds.output_kind}" for ds in datasets
            )
            raise ValueError(
                "all datasets in one EarthData request must share one "
                f"output_kind; got mixed kinds ({detail}). Split the "
                "request into one call per output kind."
            )
        return kinds.pop()

    def _initialize(self):
        """Build the :class:`EarthdataAuth` and run `configure()` (EDL login).

        Returns `None` — `earthaccess` keeps the authenticated handle
        on the :class:`EarthdataAuth` instance (and a persisted token),
        so the parent class binds no opaque `self.client`.

        Raises:
            AuthenticationError: When EDL login fails.
        """
        creds = EarthdataCredentials(
            username=self._username,
            password=self._password,
            netrc_path=self._netrc_path,
        )
        self._auth = EarthdataAuth(creds)
        self._auth.configure()
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Validate and wrap the user bbox into a :class:`SpatialExtent`.

        CMR snaps nothing server-side for a whole-granule search, so
        this is a thin wrapper over `SpatialExtent.from_pairs` — the
        same path CMEMS uses. No antimeridian splitting is applied (the
        shipped backends do not, and the CMR search accepts a plain
        bounding box).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the date range into a :class:`TemporalExtent`.

        Args:
            start: Inclusive start date as a string.
            end: Inclusive end date as a string.
            temporal_resolution: Advisory cadence label; mapped to a
                pandas frequency for the `dates` index when known.
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        freq_map = {"daily": "D", "monthly": "MS", "hourly": "h"}
        resolution = freq_map.get(temporal_resolution, "D")
        dates = pd.date_range(start_dt, end_dt, freq=resolution)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=resolution,
            dates=dates,
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def _search(self) -> list[RemoteProduct]:
        """Query CMR for the granules of every requested dataset.

        One `earthaccess.search_data` call per resolved dataset row,
        scoped to the request bbox and time window. The bounding box is
        passed as the `(west, south, east, north)` tuple CMR expects
        (lon/lat order). Each returned granule becomes one
        :class:`RemoteProduct` whose `metadata` carries the raw granule
        handle and its dataset row, so :meth:`_fetch` can group and
        fetch without re-querying.

        Returns:
            list[RemoteProduct]: One product per matching CMR granule,
                across every requested dataset. An empty list (no
                granules in the window) short-circuits the fetch.
        """
        try:
            import earthaccess
        except ImportError as exc:
            raise ImportError(
                "the NASA Earthdata backend needs `earthaccess`; install "
                "`pip install earthlens[earthdata]` (Python >=3.12)."
            ) from exc

        if self._auth is not None:
            self._auth.configure()

        bbox = (
            self.space.west,
            self.space.south,
            self.space.east,
            self.space.north,
        )
        temporal = (
            self.time.start_date.isoformat(),
            self.time.end_date.isoformat(),
        )
        products: list[RemoteProduct] = []
        for ds in self._datasets:
            granules = earthaccess.search_data(
                short_name=ds.short_name,
                version=ds.version or None,
                provider=ds.provider or None,
                bounding_box=bbox,
                temporal=temporal,
                count=-1,
            )
            for granule in granules:
                products.append(
                    RemoteProduct(
                        id=str(granule["meta"]["concept-id"]),
                        metadata={"granule": granule, "dataset": ds},
                    )
                )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch every granule `_search` returned to local paths.

        Groups the products by dataset (each dataset's granules share
        one fetch strategy) and, per group, either streams in-region
        from S3 (`earthaccess.open`) or downloads over HTTPS
        (`earthaccess.download`). The choice follows `direct_s3`
        (`G4`): `"always"` forces S3, `"never"` forces HTTPS, and
        `"auto"` (default) uses S3 only when the dataset is
        cloud-hosted and the caller runs in the DAAC's region.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: Local paths of every fetched granule, in
                dataset/group order.
        """
        import earthaccess

        out_paths: list[Path] = []
        for ds, granules in self._group_by_dataset(products):
            if self._use_s3(ds):
                files = earthaccess.open(granules)
                out_paths.extend(Path(getattr(f, "path", str(f))) for f in files)
            else:
                downloaded = earthaccess.download(granules, str(self.root_dir))
                out_paths.extend(Path(p) for p in downloaded)
        return out_paths

    @staticmethod
    def _group_by_dataset(
        products: list[RemoteProduct],
    ) -> list[tuple[EarthdataDataset, list[Any]]]:
        """Group granule handles by their dataset row, preserving order.

        Returns `(dataset, granules)` pairs rather than a dict because
        :class:`EarthdataDataset` is not hashable (it carries a `bands`
        mapping), and grouping is by object identity.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[tuple[EarthdataDataset, list]]: One pair per dataset,
                in first-seen order, each holding that dataset's raw
                granule handles.
        """
        groups: list[tuple[EarthdataDataset, list[Any]]] = []
        for product in products:
            ds = product.metadata["dataset"]
            for existing_ds, granules in groups:
                if existing_ds is ds:
                    granules.append(product.metadata["granule"])
                    break
            else:
                groups.append((ds, [product.metadata["granule"]]))
        return groups

    def _use_s3(self, dataset: EarthdataDataset) -> bool:
        """Decide whether to stream a dataset from S3 instead of HTTPS (`G4`).

        Args:
            dataset: The dataset row being fetched.

        Returns:
            bool: `True` to use `earthaccess.open` (in-region S3),
                `False` to use `earthaccess.download` (HTTPS).
        """
        if self._direct_s3 == "always":
            return True
        if self._direct_s3 == "never":
            return False
        region = "us-west-2"
        try:
            region = self._catalog.get_daac(dataset.provider).cloud_region
        except KeyError:
            pass
        return dataset.cloud_hosted and self._in_region(region)

    def _in_region(self, region: str) -> bool:
        """Return whether the caller appears to run in `region`.

        Resolution order: explicit `region=` kwarg → `AWS_REGION` →
        `AWS_DEFAULT_REGION`. EC2 instance-metadata probing is
        intentionally **not** used here — it would hang for off-cloud
        callers; the explicit kwarg / env var is the supported signal
        (`G4`).

        Args:
            region: The region to test against (e.g. `"us-west-2"`).

        Returns:
            bool: `True` when the resolved caller region equals
                `region`.
        """
        caller = (
            self._region
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
        )
        return caller == region

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Search CMR, fetch the granules, return their local paths.

        Composes :meth:`_search` and :meth:`_fetch` to pull every
        granule matching the request bbox + window to `self.root_dir`
        (or in-region S3 handles). When `aggregate` is given, the
        fetched granules are reduced per-window via the existing
        pyramids reducer (`G6`) — the facade only forwards `aggregate=`
        for a `"raster"` instance, having rejected it for `"vector"` /
        `"tabular"`.

        Args:
            progress_bar: Reserved for parity with the other backends'
                `download(progress_bar=...)` signature; the
                `earthaccess` fetch shows its own progress.
            aggregate: Optional
                :class:`earthlens.aggregate.AggregationConfig`. Only
                reached for a `"raster"` instance.

        Returns:
            list[Path]: The fetched granule paths, or — when
                `aggregate` is set — the per-window reduced raster
                paths.

        Raises:
            NotImplementedError: When `aggregate` is set but the
                installed pyramids exposes no `NetCDF.reduce` /
                `DatasetCollection.groupby` for the fetched format.
        """
        paths = self._api_via_search_fetch()
        if aggregate is not None:
            return self._aggregate(paths, aggregate)
        return paths

    def _aggregate(
        self, paths: list[Path], config: AggregationConfig
    ) -> list[Path]:
        """Reduce fetched raster granules per-window via pyramids (`G6`).

        Routes by on-disk format using the existing pyramids reducers —
        no new pyramids feature: NetCDF / HDF granules go through
        :meth:`pyramids.netcdf.NetCDF.reduce` (the CMEMS path) and a
        COG stack through
        :meth:`pyramids.dataset.DatasetCollection.groupby` (the STAC
        path).

        Args:
            paths: The fetched granule paths.
            config: The aggregation request.

        Returns:
            list[Path]: The reduced raster paths.

        Raises:
            NotImplementedError: When the installed pyramids lacks the
                reducer the fetched format needs.
        """
        netcdf_like = {".nc", ".nc4", ".h5", ".hdf", ".hdf5", ".he5"}
        cog_like = {".tif", ".tiff", ".cog"}
        nc_paths = [p for p in paths if p.suffix.lower() in netcdf_like]
        tif_paths = [p for p in paths if p.suffix.lower() in cog_like]

        out: list[Path] = []
        if nc_paths:
            out.extend(self._reduce_netcdf(nc_paths, config))
        if tif_paths:
            out.extend(self._reduce_cog_stack(tif_paths, config))
        return out

    def _reduce_netcdf(
        self, paths: list[Path], config: AggregationConfig
    ) -> list[Path]:
        """Reduce NetCDF / HDF granules over time via `NetCDF.reduce`.

        Args:
            paths: NetCDF / HDF granule paths.
            config: The aggregation request (provides `freq` / `op`).

        Returns:
            list[Path]: One reduced NetCDF per input, written beside it.

        Raises:
            NotImplementedError: When the installed pyramids has no
                `NetCDF.reduce`.
        """
        from pyramids.netcdf import NetCDF

        if not hasattr(NetCDF, "reduce"):
            raise NotImplementedError(
                "EarthData.download(aggregate=...) needs pyramids' "
                "NetCDF.reduce, which the installed pyramids build does "
                "not provide. Upgrade pyramids, or call download() without "
                "aggregate= and post-process the granules directly."
            )
        how = "mean" if config.op == "auto" else config.op
        out: list[Path] = []
        for path in paths:
            reduced = NetCDF.read_file(str(path)).reduce("time", how=how)
            target = path.with_name(f"{path.stem}_{config.freq}_agg.nc")
            reduced.to_file(str(target))
            out.append(target)
        return out

    def _reduce_cog_stack(
        self, paths: list[Path], config: AggregationConfig
    ) -> list[Path]:
        """Reduce a COG stack per-window via `DatasetCollection.groupby`.

        Args:
            paths: COG / GeoTIFF granule paths.
            config: The aggregation request (provides `freq`).

        Returns:
            list[Path]: The per-window reduced raster paths.

        Raises:
            NotImplementedError: When the installed pyramids has no
                `DatasetCollection.groupby`.
        """
        from pyramids.dataset import DatasetCollection

        if not hasattr(DatasetCollection, "groupby"):
            raise NotImplementedError(
                "EarthData.download(aggregate=...) for a COG stack needs "
                "pyramids' DatasetCollection.groupby, which the installed "
                "pyramids build does not provide."
            )
        collection = DatasetCollection.read_file([str(p) for p in paths])
        grouped = collection.groupby(config.freq)
        return [Path(p) for p in grouped.to_file(str(self.root_dir))]
