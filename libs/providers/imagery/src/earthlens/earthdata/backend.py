"""Backend that fetches NASA Earthdata granules via earthaccess + CMR.

`Earthdata(AbstractDataSource)` accepts the same constructor surface
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

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from earthlens.base import (
    CADENCE_ALIASES,
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    end_is_date_only,
    expand_bare_date_end,
    region_affinity,
)
from earthlens.earthdata.auth import EarthdataAuth, EarthdataCredentials
from earthlens.earthdata.catalog import Catalog, EarthdataDataset

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig


class Earthdata(AbstractDataSource):
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

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    AGGREGATE_REFUSAL_REASON = "this collection resolves to a non-gridded output, so there is no grid to reduce"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        daac: str | None = None,
        region: str | None = None,
        direct_s3: str = "auto",
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
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
            token: EDL bearer token (a JSON Web Token generated from the
                EDL profile) — authenticate without a password, like
                GEE's service key. Takes precedence over username /
                password; falls back to the `EARTHDATA_TOKEN` env var.
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
        self._token = token
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
            ValueError: When `variables` is empty, a key is unknown
                (the catalog's did-you-mean is surfaced in the message),
                or `daac=` is combined with a multi-dataset request.
        """
        if not variables:
            raise ValueError(
                "Earthdata requires a non-empty `variables` mapping of "
                "{dataset_key: [band, ...]}."
            )
        # `daac=` disambiguates a single short_name served by more than one
        # provider, so it only makes sense for a single-dataset request. With
        # several datasets it would be applied to every key and wrongly reject
        # any whose DAAC differs — reject that combination up front.
        if self._daac is not None and len(variables) > 1:
            raise ValueError(
                "daac= only applies to a single-dataset request; it cannot be "
                f"combined with {len(variables)} datasets (it would be applied "
                "to every key). Drop daac= and rely on the dataset keys, or "
                "issue one request per dataset."
            )
        # Catalog.resolve raises ValueError (with a did-you-mean hint) for an
        # unknown key or a daac mismatch, so it propagates as-is.
        return [self._catalog.resolve(key, daac=self._daac) for key in variables]

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
            detail = ", ".join(f"{ds.short_name}={ds.output_kind}" for ds in datasets)
            raise ValueError(
                "all datasets in one Earthdata request must share one "
                f"output_kind; got mixed kinds ({detail}). Split the "
                "request into one call per output kind."
            )
        return kinds.pop()

    def _initialize(self):
        """Build the :class:`EarthdataAuth`; defer the EDL login.

        Returns `None` — `earthaccess` keeps the authenticated handle on
        the :class:`EarthdataAuth` instance (and a persisted token), so
        the parent class binds no opaque `self.client`. The EDL login
        (`EarthdataAuth.configure`, which contacts the auth server) is
        deferred out of construction: it runs on the first :meth:`_search`
        (CMR granule search authenticates via the idempotent `configure()`),
        so constructing the backend never authenticates — but note that a
        dry-run `search()` does, since CMR access goes through the same
        `earthaccess` session.
        """
        creds = EarthdataCredentials(
            username=self._username,
            password=SecretStr(self._password) if self._password is not None else None,
            token=SecretStr(self._token) if self._token is not None else None,
            netrc_path=self._netrc_path,
        )
        self._auth = EarthdataAuth(creds)
        return None

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
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Raises:
            ValueError: If `temporal_resolution` is not one of the cadences
                `earthlens.base.CADENCE_ALIASES` accepts.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        self._end_is_date_only = end_is_date_only(end)
        return self._cadence_extent(
            start,
            end,
            fmt=fmt,
            cadence=temporal_resolution,
            accepted=CADENCE_ALIASES,
        )

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
        # A date-only end is inclusive of its whole calendar day: it parses to
        # midnight, so passing it verbatim would make a same-day request a
        # zero-width instant and match few or no granules. An end naming a time
        # means that instant and is left alone.
        end_of_day = expand_bare_date_end(
            self.time.end_date, date_only=self._end_is_date_only
        )
        temporal = (
            self.time.start_date.isoformat(),
            end_of_day.isoformat(),
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

        show_progress = getattr(self, "_show_progress", True)
        out_paths: list[Path] = []
        for ds, granules in self._group_by_dataset(products):
            if self._use_s3(ds):
                files = earthaccess.open(granules, show_progress=show_progress)
                out_paths.extend(Path(getattr(f, "path", str(f))) for f in files)
            else:
                downloaded = earthaccess.download(
                    granules, str(self.root_dir), show_progress=show_progress
                )
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

        Delegates to the shared `earthlens.base.region.region_affinity`
        helper: the caller region is resolved from the explicit `region=`
        kwarg, then `AWS_REGION` / `AWS_DEFAULT_REGION`. EC2
        instance-metadata probing is intentionally **not** used here
        (`probe=False`) — it would hang for off-cloud callers; the
        explicit kwarg / env var is the supported signal (`G4`).

        Args:
            region: The region to test against (e.g. `"us-west-2"`).

        Returns:
            bool: `True` when the resolved caller region equals
                `region`.
        """
        return (
            region_affinity(region, caller_region=self._region, probe=False)
            == "in-region"
        )

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
            progress_bar: Forwarded to `earthaccess` as `show_progress`
                on the download / open call, so `False` suppresses the
                per-granule progress bar.
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
        self._show_progress = progress_bar
        paths = self._api_via_search_fetch()
        if aggregate is not None:
            return self._aggregate(paths, aggregate)
        return paths

    def _aggregate(self, paths: list[Path], config: AggregationConfig) -> list[Path]:
        """Reduce fetched raster granules per-window via pyramids (`G6`).

        The routing is **axis-driven, not format-driven** — it depends
        on where the time axis lives, not on the file extension:

        * The common Earthdata case is a **stack of single-timestep
          granules** (one file per overpass / half-hour / day, NetCDF
          *or* COG). That whole stack is windowed by
          :meth:`pyramids.dataset.DatasetCollection.groupby` (the STAC
          path) — one reduced raster per `config.freq` window.
        * The rarer case is a **single granule that already carries a
          multi-timestep time axis inside it** (one NetCDF cube). Its
          internal `time` axis is collapsed by
          :meth:`pyramids.netcdf.NetCDF.reduce` (the CMEMS path).

        A single COG (one timestep, no internal axis) falls through to
        the stack path as a one-element stack (a single window).

        Args:
            paths: The fetched granule paths.
            config: The aggregation request.

        Returns:
            list[Path]: The reduced raster paths.

        Raises:
            NotImplementedError: When the installed pyramids lacks the
                reducer the chosen path needs.
        """
        if not paths:
            return []
        if len(paths) == 1 and self._is_netcdf_like(paths[0]):
            return self._reduce_internal_axis(paths[0], config)
        return self._reduce_stack(paths, config)

    @staticmethod
    def _is_netcdf_like(path: Path) -> bool:
        """Return whether a path is a NetCDF / HDF granule (has an internal axis).

        Args:
            path: A fetched granule path.

        Returns:
            bool: `True` for NetCDF / HDF suffixes (which can carry an
                internal multi-timestep time axis), `False` otherwise
                (e.g. a single-timestep COG).
        """
        return path.suffix.lower() in {".nc", ".nc4", ".h5", ".hdf", ".hdf5", ".he5"}

    def _reduce_internal_axis(
        self, path: Path, config: AggregationConfig
    ) -> list[Path]:
        """Collapse one NetCDF cube's internal time axis via `NetCDF.reduce`.

        If the granule has no `time` dimension (a single-timestep file, or
        an HDF-EOS file whose time is not a decodable CF dimension), there
        is nothing to collapse — it is treated as a one-element stack and
        windowed via :meth:`_reduce_stack` instead of raising.

        Args:
            path: A single NetCDF / HDF granule, ideally carrying a
                multi-timestep `time` axis.
            config: The aggregation request (provides `freq` / `op`).

        Returns:
            list[Path]: One reduced NetCDF (written beside the source) when
                a `time` axis is present, otherwise the per-window raster
                paths from the stack fallback.

        Raises:
            NotImplementedError: When the installed pyramids has no
                `NetCDF.reduce` (or, on the fallback, no
                `DatasetCollection.groupby`).
        """
        from pyramids.netcdf import NetCDF

        if not hasattr(NetCDF, "reduce"):
            raise NotImplementedError(
                "Earthdata.download(aggregate=...) needs pyramids' "
                "NetCDF.reduce, which the installed pyramids build does "
                "not provide. Upgrade pyramids, or call download() without "
                "aggregate= and post-process the granules directly."
            )
        nc = NetCDF.read_file(str(path))
        if "time" not in tuple(nc.dimension_names or ()):
            # No internal time axis to collapse — e.g. a single-timestep
            # granule, or an HDF-EOS file whose time is not a decodable CF
            # dimension. Treat it as a one-element stack so `groupby` windows
            # it instead of `reduce` raising on a missing dimension.
            return self._reduce_stack([path], config)
        how = "mean" if config.op == "auto" else config.op
        reduced = nc.reduce("time", how=how)
        target = path.with_name(f"{path.stem}_{config.freq}_agg.nc")
        reduced.to_file(str(target))
        return [target]

    def _reduce_stack(self, paths: list[Path], config: AggregationConfig) -> list[Path]:
        """Window a stack of single-timestep granules via `groupby`.

        The common Earthdata aggregation case: many granules, each one
        timestep, windowed by `config.freq` into one reduced raster per
        window. Handles a NetCDF stack and a COG stack alike (the choice
        is the granule layout, not the format).

        Args:
            paths: The fetched granule paths (a temporal stack).
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
                "Earthdata.download(aggregate=...) for a granule stack needs "
                "pyramids' DatasetCollection.groupby, which the installed "
                "pyramids build does not provide."
            )
        collection = DatasetCollection.from_files([str(p) for p in paths])
        grouped = collection.groupby(config.freq)
        return [Path(p) for p in grouped.to_file(str(self.root_dir))]


#: Back-compat alias for the original camel-cased class name. NASA brands the
#: service "Earthdata" (one word), so `Earthdata` is the canonical class name;
#: `EarthData` remains importable for existing callers.
EarthData = Earthdata
