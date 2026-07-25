from __future__ import annotations

import difflib
import functools
import inspect
import os
import warnings
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

OutputKind = Literal["raster", "vector", "tabular", "mixed"]
"""The four output shapes an `AbstractDataSource` subclass can emit.

`EarthLens` reads `datasource.OUTPUT_KIND` at `download()` time to
decide whether a non-`None` `aggregate=` argument is meaningful for
the bound backend. The semantics per value:

* `"raster"` — gridded raster output. Covers both 2-D rasters
  (GeoTIFF, COG, BIL) and per-variable NetCDF / Zarr — the
  classifier is "is the on-disk artefact a gridded array?". The
  aggregator is forwarded; the time-window reducer (in
  `earthlens.aggregate`, backed by `pyramids.netcdf.NetCDF`) reads
  the file directly. This is what every backend shipped before C1
  declares, and every gridded backend added after C1 should
  declare.
* `"vector"` — `GeoDataFrame` / vector features (events, footprints,
  admin boundaries). The aggregator is rejected with
  `NotImplementedError` — no meaningful gridded reduction.
* `"tabular"` — `DataFrame` per-row records (station observations,
  climate indices, biodiversity occurrences). Also rejects
  `aggregate=`.
* `"mixed"` — escape hatch for backends like HDX whose per-resource
  format is only known at download time. The facade forwards the
  aggregator unchanged and trusts the backend to honour it.

Examples:
    - Inspect the literal arguments without importing anything else:
        ```python
        >>> from typing import get_args
        >>> from earthlens.base import OutputKind
        >>> get_args(OutputKind)
        ('raster', 'vector', 'tabular', 'mixed')

        ```

See Also:
    AbstractDataSource.OUTPUT_KIND: The per-class declaration each
        backend uses to opt into one of these shapes.
"""


class PolygonAoiWarning(UserWarning):
    """A polygon `aoi=` was reduced to its bounding box by the chosen backend.

    Raised (as a warning) when a request passes a real polygon area of
    interest to a backend whose `SUPPORTS_POLYGON_AOI` is `False`. The
    download still succeeds, but it covers the polygon's **bounding box**, so
    cells outside the polygon are included. That is the most dangerous kind of
    wrong result — a valid raster of the right variable over roughly the right
    area — so it is surfaced rather than left silent.

    A dedicated class (rather than a bare `UserWarning`) so callers can filter
    or escalate exactly this case:

    Examples:
        - Turn the silent degradation into an error for a strict pipeline:
            ```python
            >>> import warnings
            >>> from earthlens.base import PolygonAoiWarning
            >>> with warnings.catch_warnings(record=True) as caught:
            ...     warnings.simplefilter("always")
            ...     warnings.warn("bbox only", PolygonAoiWarning)
            >>> caught[0].category.__name__
            'PolygonAoiWarning'

            ```
        - It is a `UserWarning`, so existing broad filters still catch it:
            ```python
            >>> from earthlens.base import PolygonAoiWarning
            >>> issubclass(PolygonAoiWarning, UserWarning)
            True

            ```
    """


@dataclass(frozen=True)
class RemoteProduct:
    """A single discoverable item returned by `AbstractDataSource._search`.

    Carries enough metadata for `AbstractDataSource._fetch` to pull
    the underlying bytes without re-querying the catalog, plus a
    free-form `metadata` dict for backend-specific payloads (CMR
    `umm`, STAC item JSON, EUMETSAT `eumdac.product.id`, CDS
    request-shape dict). Designed so a dry-run "what would I
    download?" query is cheap — call `_search` and inspect the
    returned list without consuming any network bandwidth for the
    actual data.

    The dataclass is frozen and value-equal: two `RemoteProduct`s
    with the same `id` / `href` / `metadata` compare equal and can
    be used as dedupe keys (after dict-ification of `metadata`).

    Attributes:
        id: Stable provider-side identifier of the product. Format
            depends on the backend: a CMR concept-id
            (`G1234-PROVIDER`), a CDS request hash, an Earth Engine
            asset id, a STAC item id, etc. Used for logging,
            caching keys, and dedupe.
        href: Optional URL the bytes live at. `None` is valid for
            backends whose `_fetch` needs more than a URL — CDS
            jobs queue a request and discover the URL at completion
            time; CHIRPS FTP composes the path from the catalog row
            + date.
        metadata: Backend-specific extra fields the fetch step
            needs. Kept untyped (a plain dict) on purpose: every
            backend's metadata shape is different, and pinning a
            schema here would force every backend to inherit a
            wrapper class for its CMR JSON or its STAC asset map.

    Examples:
        - Minimal product — only the id is required:
            ```python
            >>> from earthlens.base import RemoteProduct
            >>> rp = RemoteProduct(id="G1234-EARTHDATA")
            >>> rp.id
            'G1234-EARTHDATA'
            >>> rp.href is None
            True
            >>> rp.metadata
            {}

            ```
        - Carry an asset URL plus arbitrary upstream metadata:
            ```python
            >>> from earthlens.base import RemoteProduct
            >>> rp = RemoteProduct(
            ...     id="S2A_MSIL2A_20240115T112109_N0510_R037_T29SNB_20240115T143018",
            ...     href="s3://sentinel-s2-l2a-cogs/29/S/NB/2024/1/15/0/B04.tif",
            ...     metadata={"cloud_cover": 12.5, "platform": "Sentinel-2A"},
            ... )
            >>> rp.metadata["platform"]
            'Sentinel-2A'
            >>> rp.href.endswith(".tif")
            True

            ```
        - Two products with the same fields compare equal:
            ```python
            >>> from earthlens.base import RemoteProduct
            >>> a = RemoteProduct(id="x", href="h", metadata={"k": 1})
            >>> b = RemoteProduct(id="x", href="h", metadata={"k": 1})
            >>> a == b
            True

            ```
    """

    id: str
    href: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TemporalExtent(BaseModel):
    """Per-instance temporal context produced by :meth:`check_input_dates`.

    Replaces the `self.time` dict that earlier versions of
    :class:`AbstractDataSource` accepted from subclass overrides. The
    frozen pydantic model enforces presence of every consumer-visible
    field at construction time, so a subclass that returns a malformed
    container fails fast instead of surfacing as `KeyError` deep
    inside the download loop.

    Attributes:
        start_date: Inclusive start of the requested window. Typed
            :data:`~typing.Any` because pandas / numpy timestamp types
            are not native pydantic primitives; the cross-field
            validator below enforces `start_date <= end_date` for
            anything that supports comparison.
        end_date: Inclusive end of the requested window.
        resolution: Spacing between consecutive entries in
            :attr:`dates`, expressed as a pandas frequency alias —
            `"D"` for daily, `"MS"` for month-start. Same
            shorthand pandas uses for `date_range(freq=...)`.
        dates: The :class:`pandas.DatetimeIndex` the download loop
            iterates. Typed :data:`~typing.Any` to avoid a hard
            pandas import in the abstract module.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    start_date: Any
    end_date: Any
    resolution: str
    dates: Any

    @model_validator(mode="after")
    def _check_start_le_end(self) -> TemporalExtent:
        """Validate that `start_date <= end_date`.

        Raises:
            ValueError: If the window is inverted.
        """
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError(
                    f"TemporalExtent has inverted bounds: start_date "
                    f"{self.start_date} > end_date {self.end_date}"
                )
        return self


class SpatialExtent(BaseModel):
    """Geographic bounding box (WGS84) for a download request.

    Backend-agnostic. Coordinates are in **degrees**:

    * latitude in `[-90, 90]` (south negative, north positive)
    * longitude in `[-180, 180]` (west negative, east positive)

    Each concrete data source converts this to whatever format its
    protocol expects (CDS: `[north, west, south, east]`; CHIRPS:
    per-row clipping; S3: prefix filter; GEE:
    `ee.Geometry.Rectangle(west, south, east, north)`). For
    projected coordinates, define a separate `ProjectedExtent`
    type — do not reuse this one with metric values.

    Attributes:
        latitude_min: Inclusive south edge of the bbox, in degrees.
        latitude_max: Inclusive north edge of the bbox, in degrees.
        longitude_min: Inclusive west edge of the bbox, in degrees.
        longitude_max: Inclusive east edge of the bbox, in degrees.
        resolution: Grid cell size in degrees, applied to both
            latitude and longitude. `None` for backends that work
            on irregular grids or do not need a cell size for their
            request shape (e.g. CHIRPS FTP file lookup, S3 prefix
            listing). Mirrors :attr:`TemporalExtent.resolution` —
            the spatial counterpart of the temporal cadence.
    """

    model_config = ConfigDict(frozen=True)

    latitude_min: float = Field(ge=-90.0, le=90.0, description="South edge in degrees")
    latitude_max: float = Field(ge=-90.0, le=90.0, description="North edge in degrees")
    longitude_min: float = Field(
        ge=-180.0, le=180.0, description="West edge in degrees"
    )
    longitude_max: float = Field(
        ge=-180.0, le=180.0, description="East edge in degrees"
    )
    resolution: float | None = Field(
        default=None, gt=0.0, description="Grid cell size in degrees"
    )
    geometry: Any = Field(
        default=None,
        exclude=True,
        repr=False,
        description=(
            "Optional WGS84 polygon mask (a geopandas `GeoDataFrame`) "
            "captured when the request's area of interest was a polygon "
            "rather than a plain bbox. Raster backends that clip via "
            "`pyramids.Dataset.crop` use it to mask the fetched bbox to "
            "the exact polygon; `None` means clip to the rectangular "
            "bbox only. Excluded from serialisation."
        ),
    )

    @model_validator(mode="after")
    def _check_min_le_max(self) -> SpatialExtent:
        """Validate that `min <= max` on both axes.

        Per-field range constraints (`Field(ge=..., le=...)`) cannot
        express the cross-field invariant.

        Raises:
            ValueError: If either `latitude_min > latitude_max` or
                `longitude_min > longitude_max`.
        """
        if self.latitude_min > self.latitude_max:
            raise ValueError(
                f"latitude_min ({self.latitude_min}) > "
                f"latitude_max ({self.latitude_max})"
            )
        if self.longitude_min > self.longitude_max:
            # A west > east box is how GeoJSON/STAC spell an antimeridian
            # crossing, so say so and name the remedy: only the stac backend
            # splits such a box today (via
            # `pyramids.feature.bbox.split_antimeridian`), and a bare
            # "min > max" reads like a typo rather than an unsupported case.
            raise ValueError(
                f"longitude_min ({self.longitude_min}) > longitude_max "
                f"({self.longitude_max}). A west-of-east box denotes an "
                f"antimeridian crossing, which this backend does not support; "
                f"split it at ±180 and issue the two halves as separate "
                f"requests (e.g. [{self.longitude_min}, 180] and "
                f"[-180, {self.longitude_max}])."
            )
        return self

    #: The scalar fields that define spatial identity. `geometry` is a
    #: heavy, unhashable `GeoDataFrame` whose pandas `==` is non-boolean,
    #: so it is deliberately excluded from equality / hashing (as it is
    #: from serialisation) — two extents over the same bbox are equal and
    #: hashable whether or not one carries a polygon mask.
    _IDENTITY_FIELDS = (
        "latitude_min",
        "latitude_max",
        "longitude_min",
        "longitude_max",
        "resolution",
    )

    def _identity(self) -> tuple[float | None, ...]:
        """Return the bbox-identity tuple used for equality / hashing."""
        return tuple(getattr(self, name) for name in self._IDENTITY_FIELDS)

    def __eq__(self, other: object) -> bool:
        """Compare two extents by bbox + resolution, ignoring `geometry`.

        Overrides pydantic's field-wise equality, which would otherwise
        evaluate `GeoDataFrame == GeoDataFrame` (a non-boolean pandas
        result) and raise for a polygon-`aoi=` extent.

        Args:
            other: The object to compare against.

        Returns:
            `True` when `other` is a `SpatialExtent` with the same bbox
            and resolution; `NotImplemented` for any other type.
        """
        if not isinstance(other, SpatialExtent):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        """Hash by bbox + resolution, ignoring the unhashable `geometry`."""
        return hash(self._identity())

    @classmethod
    def from_pairs(
        cls,
        lat_lim: list[float],
        lon_lim: list[float],
        resolution: float | None = None,
    ) -> SpatialExtent:
        """Build from the legacy `[min, max]` pair shape.

        :class:`AbstractDataSource.__init__` accepts `lat_lim` /
        `lon_lim` as constructor kwargs in the public API; this
        classmethod adapts that shape to the four named fields.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            resolution: Grid cell size in degrees. Defaults to
                `None` (unspecified — typical for backends that
                work off file listings rather than gridded request
                shapes).

        Returns:
            SpatialExtent: A validated, frozen instance.
        """
        return cls(
            latitude_min=lat_lim[0],
            latitude_max=lat_lim[1],
            longitude_min=lon_lim[0],
            longitude_max=lon_lim[1],
            resolution=resolution,
        )

    @property
    def north(self) -> float:
        """Northern edge of the bbox (== `latitude_max`)."""
        return self.latitude_max

    @property
    def south(self) -> float:
        """Southern edge of the bbox (== `latitude_min`)."""
        return self.latitude_min

    @property
    def east(self) -> float:
        """Eastern edge of the bbox (== `longitude_max`)."""
        return self.longitude_max

    @property
    def west(self) -> float:
        """Western edge of the bbox (== `longitude_min`)."""
        return self.longitude_min

    def estimate_pixel_dims(self, scale_m: float) -> tuple[int, int]:
        """Return `(width_px, height_px)` of this bbox sampled at `scale_m` metres.

        Thin wrapper over :func:`earthlens.base.spatial.estimate_pixel_dims`
        so every backend can pre-flight a request size without reaching
        into another subpackage. Useful e.g. for GEE's 32768-px
        synchronous-export cap or for any "will this download blow up?"
        check before queuing a job.

        Args:
            scale_m: Output pixel size in metres.

        Returns:
            `(width_px, height_px)` — both rounded up, each at least 1.

        Raises:
            ValueError: If `scale_m` is not positive.
        """
        # Local import to keep the existing import order untouched.
        from earthlens.base.spatial import estimate_pixel_dims

        return estimate_pixel_dims(
            self.longitude_min,
            self.latitude_min,
            self.longitude_max,
            self.latitude_max,
            scale_m,
        )


class LazyClientMixin:
    """Defer a backend's network client until first `client` access.

    A backend that authenticates or opens a connection mixes this in
    (before :class:`AbstractDataSource` in its bases) and implements
    :meth:`_open_client` — the network half. Its :meth:`_initialize` then
    keeps only eager, offline work (input validation, catalog resolution)
    and returns `None`, so the parent never binds an eager `client`. The
    live client is built lazily on first access to :attr:`client` and
    cached, so constructing the backend — or a bare `EarthLens(...)` —
    never touches the network. Authentication errors therefore surface on
    the first `download()` / `search()` / `client` use rather than at
    construction.

    Examples:
        - The client is built once, on first access, then cached:
            ```python
            >>> from earthlens.base import LazyClientMixin
            >>> class Demo(LazyClientMixin):
            ...     calls = 0
            ...     def _open_client(self):
            ...         Demo.calls += 1
            ...         return "connection"
            >>> demo = Demo()
            >>> Demo.calls
            0
            >>> demo.client
            'connection'
            >>> demo.client
            'connection'
            >>> Demo.calls
            1

            ```
    """

    def _open_client(self) -> Any:
        """Open and return the backend's live network client.

        Raises:
            NotImplementedError: Until a backend overrides it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} mixes in LazyClientMixin but does not "
            f"implement _open_client."
        )

    @property
    def client(self) -> Any:
        """The backend's network client — opened lazily and cached.

        Returns:
            The object :meth:`_open_client` returns, built on first access
            and reused thereafter.
        """
        if "_client_obj" not in self.__dict__:
            self.__dict__["_client_obj"] = self._open_client()
        return self.__dict__["_client_obj"]

    @client.setter
    def client(self, value: Any) -> None:
        """Inject a client, seeding the cache so `_open_client` is skipped.

        Lets callers (and tests) bind a ready-made / fake client; the
        getter then returns it without ever opening a connection.

        Args:
            value: The client object to use.
        """
        self.__dict__["_client_obj"] = value


class AbstractDataSource(ABC):
    """Blueprint for every concrete data-source backend.

    Subclasses encapsulate the request shape, authentication, and
    download orchestration for a single provider (CHIRPS, ERA5 on AWS
    S3, ECMWF CDS, Google Earth Engine). The base class wires the
    abstract hooks (:meth:`_initialize`, :meth:`_create_grid`,
    :meth:`_check_input_dates`) into a uniform `__init__` shape and
    exposes a single :meth:`download` entry point.

    Attributes:
        OUTPUT_KIND: Class-level declaration of the natural output
            shape this backend emits. Read by
            :class:`earthlens.earthlens.EarthLens` at facade
            `download()` time to gate the `aggregate=` argument:
            `"raster"` accepts it (the existing pyramids-backed
            `aggregate_netcdf` flow); `"vector"` and `"tabular"`
            reject it with :class:`NotImplementedError`; `"mixed"`
            forwards it unchanged. Subclasses override the class
            attribute; the default is `"raster"`.

            Most backends fix `OUTPUT_KIND` as a class attribute. A few
            backends whose output shape is only known once the requested
            dataset(s) are resolved set it **per instance** in
            `__init__` instead — a sanctioned override: earthdata and
            eumetsat copy the resolved dataset's `output_kind` onto
            `self.OUTPUT_KIND`, and tropycal sets `"tabular"` for its
            `ships` product (else `"vector"`). The facade reads the
            instance attribute, so both forms work.
        REQUIRES_TIME_WINDOW: Whether this backend needs both `start` and
            `end`. `True` (the default) makes :meth:`__init__` reject a
            missing bound up front with an actionable message, instead of
            letting the `None` reach the subclass's date parsing and surface
            as a bare `TypeError: strptime() argument 1 must be str, not
            NoneType`. Snapshot backends with no per-step time axis — admin,
            osm, overture, glaciers, risk_indicators, bathymetry, dem,
            soilgrids, solar_wind_atlas — set it to `False` and treat a
            `None` bound as "the whole record".
        SUPPORTS_POLYGON_AOI: Whether this backend clips to a polygon
            `aoi=`, rather than only to its bounding box. A polygon `aoi=`
            is reduced to `lat_lim` / `lon_lim` *and* carried as a mask on
            `self.space.geometry`; a backend honours that mask by cropping
            through `earthlens.base.spatial.crop_to_aoi` /
            `mask_to_geometry` (or reading `space.geometry` itself) and sets
            this to `True`. When it is `False`,
            :meth:`_attach_clip_geometry` emits a
            :class:`PolygonAoiWarning`, because the request silently returns
            the polygon's bounding box — a plausible-looking raster over
            roughly the right area, which is the hardest kind of wrong
            output to notice.
    """

    OUTPUT_KIND: OutputKind = "raster"

    REQUIRES_TIME_WINDOW: bool = True

    SUPPORTS_POLYGON_AOI: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Give every backend the facade's ergonomic constructor sugar.

        Wraps each concrete backend's `__init__` so that — whether reached
        through the `EarthLens` facade or by constructing the backend
        class directly — it also accepts:

        * `aoi` (+ `buffer`): any shape :func:`earthlens.base.spatial.normalize_aoi`
          understands, reduced to `lat_lim` / `lon_lim`; a backend that
          declares its own `aoi` (WorldPop) keeps it;
        * `cadence`: a clearer alias for `temporal_resolution`;
        * `dataset`: split out of a single-key `variables` dict (or passed
          through to a backend with a native `dataset`, e.g. S3).

        The original `__init__` is preserved as the wrapper's `__wrapped__`,
        so signature introspection (e.g. `EarthLens.options_for`) and the
        facade's kwarg validation still see the backend's real parameters.

        Note:
            No backend currently subclasses another backend. If one ever
            does, its `__init__` must not forward the ergonomic kwargs
            (`aoi` / `buffer` / `cadence` / `dataset`) up to
            `super().__init__()`: the parent's wrapper would resolve them a
            second time (e.g. re-running `resolve_aoi`). Forward only the
            already-resolved native parameters (`lat_lim` / `lon_lim` /
            `temporal_resolution` / `variables`) instead.
        """
        super().__init_subclass__(**kwargs)
        orig = cls.__dict__.get("__init__")
        if orig is None or getattr(orig, "_ergonomic", False):
            return
        params = inspect.signature(orig).parameters
        native_aoi = "aoi" in params
        native_dataset = "dataset" in params

        @functools.wraps(orig)
        def __init__(
            self, *args, aoi=None, buffer=None, cadence=None, dataset=None, **kw
        ):
            clip_geometry = None
            if cadence is not None:
                kw["temporal_resolution"] = cadence
            if dataset is not None:
                if native_dataset:
                    kw["dataset"] = dataset
                elif isinstance(kw.get("variables"), dict):
                    raise ValueError(
                        "pass variables= as a list when using dataset=, or omit "
                        "dataset= and key the variables dict yourself"
                    )
                else:
                    v = kw.get("variables")
                    kw["variables"] = {dataset: list(v) if v is not None else []}
            if aoi is not None:
                if native_aoi:
                    if buffer is not None:
                        raise ValueError(
                            f"buffer= is not supported by {cls.__name__}, which "
                            "interprets aoi= itself"
                        )
                    kw["aoi"] = aoi
                else:
                    if kw.get("lat_lim") is not None or kw.get("lon_lim") is not None:
                        raise ValueError(
                            "pass either aoi= or lat_lim=/lon_lim=, not both"
                        )
                    from earthlens.base.spatial import resolve_aoi

                    kw["lat_lim"], kw["lon_lim"], clip_geometry = resolve_aoi(
                        aoi, buffer=buffer
                    )
            elif buffer is not None:
                raise ValueError(
                    "buffer= only applies to a point aoi=(lon, lat); pass aoi= too"
                )
            orig(self, *args, **kw)
            if clip_geometry is not None:
                self._attach_clip_geometry(clip_geometry)

        __init__._ergonomic = True  # type: ignore[attr-defined]
        cls.__init__ = __init__  # type: ignore[method-assign]

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]] | list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        fmt: str = "%Y-%m-%d",
        path: Path | str = "",
    ):
        """Initialize a data source instance.

        Captures the return values of the abstract hooks so subclasses
        do not have to wire them onto `self` themselves:

        * `self.client` — whatever :meth:`_initialize` returns (a CDS
          client, an S3 client, `None` for FTP). Subclasses that
          assign `self.client` inside :meth:`_initialize` (e.g.
          :class:`S3`) keep their own assignment; the parent only sets
          the attribute when :meth:`_initialize` returns a non-`None`
          value.
        * `self.space` — the dict returned by :meth:`_create_grid`,
          containing `lat_lim` and `lon_lim`. Subclasses that
          override :meth:`_create_grid` to set attributes directly (e.g.
          :class:`CHIRPS`) and return `None` are unaffected.
        * `self.time` — the dict returned by :meth:`_check_input_dates`,
          containing `start_date`, `end_date`, `time_freq` and
          `dates`. Same opt-in semantics as `self.space`.
        * `self.root_dir` — the absolute :class:`pathlib.Path` of the
          output directory. `self.path` is kept as a legacy alias so
          older backends (CHIRPS, S3) continue to work.

        Args:
            start: Inclusive start date as a string. Format controlled
                by `fmt`. Defaults to `None`.
            end: Inclusive end date as a string. Defaults to `None`.
            variables: List of variable short codes to download.
            temporal_resolution: `"daily"` or `"monthly"`. Defaults
                to `"daily"`.
            lat_lim: `[lat_min, lat_max]`.
            lon_lim: `[lon_min, lon_max]`.
            fmt: `strptime` format for `start` / `end`. Defaults
                to `"%Y-%m-%d"`.
            path: Output directory. Created if it does not exist.
                Defaults to the current working directory.

        Raises:
            ValueError: If :attr:`REQUIRES_TIME_WINDOW` is `True` and either
                `start` or `end` is `None`.
        """
        self._check_time_window(start, end)

        client = self._initialize()
        if client is not None:
            self.client = client

        self.temporal_resolution = temporal_resolution
        self.vars = variables

        space = self._create_grid(lat_lim, lon_lim)
        if isinstance(space, SpatialExtent):
            self.space = space
        elif isinstance(space, dict):
            self.space = SpatialExtent.from_pairs(
                lat_lim=space["lat_lim"], lon_lim=space["lon_lim"]
            )

        time = self._check_input_dates(start, end, temporal_resolution, fmt)
        if isinstance(time, TemporalExtent):
            self.time = time
        elif isinstance(time, dict):
            self.time = TemporalExtent(
                start_date=time["start_date"],
                end_date=time["end_date"],
                resolution=cast(str, time.get("resolution", time.get("time_freq"))),
                dates=time["dates"],
            )

        self.root_dir = Path(path).absolute()
        self.path = self.root_dir
        if not os.path.exists(self.root_dir):
            os.makedirs(self.root_dir)

    @classmethod
    def _check_time_window(cls, start: Any, end: Any) -> None:
        """Reject a missing `start` / `end` when the backend needs both.

        Runs before :meth:`_check_input_dates` so a backend that declares
        :attr:`REQUIRES_TIME_WINDOW` never has to defend against `None`, and
        the user gets the name of the missing bound rather than a bare
        `strptime` `TypeError` from deep inside the subclass.

        Args:
            start: The requested start bound, possibly `None`.
            end: The requested end bound, possibly `None`.

        Raises:
            ValueError: If the backend requires a window and either bound is
                `None`. The message names which bound(s) are missing.
        """
        if not cls.REQUIRES_TIME_WINDOW:
            return
        missing = [
            name for name, value in (("start", start), ("end", end)) if value is None
        ]
        if not missing:
            return
        raise ValueError(
            f"the {cls.__name__} backend requires a time window, but "
            f"{' and '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} missing. Pass "
            f"start=/end= (e.g. start='2024-01-01', end='2024-01-31') or the "
            f"single time='2024-01-01/2024-01-31' range."
        )

    def _attach_clip_geometry(self, geometry: Any) -> None:
        """Record a polygon mask on `self.space` for precise clipping.

        Called by the ergonomic `__init__` wrapper when the request's
        `aoi=` was a polygon rather than a plain bbox. The geometry is
        stored on the (frozen) :class:`SpatialExtent` via a copy so that
        raster backends clipping through `pyramids.Dataset.crop` can mask
        the fetched bbox down to the exact shape. A no-op when `self.space`
        is not a :class:`SpatialExtent`.

        Backends that do not clip to the polygon (`SUPPORTS_POLYGON_AOI` is
        `False`) still get the mask recorded — a later migration then needs no
        facade change — but the caller is warned, because such a request
        silently returns the polygon's bounding box instead.

        Args:
            geometry: A WGS84 `GeoDataFrame` polygon mask.

        Warns:
            PolygonAoiWarning: When the backend does not honour a polygon
                `aoi=`, so the result is the polygon's bounding box.
        """
        space = getattr(self, "space", None)
        if not isinstance(space, SpatialExtent):
            return
        if geometry is not None and not self.SUPPORTS_POLYGON_AOI:
            warnings.warn(
                f"the {type(self).__name__} backend clips to a bounding box only, "
                f"so this polygon aoi= is applied as its bounding box — cells "
                f"outside the polygon are still included. Post-clip the result "
                f"with `pyramids.Dataset.crop(mask=...)`, or pass a bbox aoi= to "
                f"make the request's extent explicit.",
                PolygonAoiWarning,
                stacklevel=3,
            )
        self.space = space.model_copy(update={"geometry": geometry})

    def authenticate(self) -> AbstractDataSource:
        """Eagerly establish the backend's authenticated connection.

        The explicit, fail-fast counterpart to the lazy authentication
        that otherwise happens on the first :meth:`download` / `search`:
        it opens the network client for backends that have one (those
        mixing in :class:`LazyClientMixin` — e.g. GEE, ECMWF, STAC) or
        runs the credential `configure()` step for backends that hold an
        auth object (CMEMS, Earthdata, EUMETSAT, …), raising
        :class:`~earthlens.base.AuthenticationError` on failure. It is a
        no-op for credential-free backends (CHIRPS, GDACS, Overture, …),
        and is idempotent.

        Returns:
            The backend instance, so callers can chain
            `EarthLens(...).authenticate().download()`.

        Raises:
            AuthenticationError: If the backend cannot authenticate.
        """
        # Independent checks, not an if/elif chain: a backend may legitimately
        # have both a lazily-opened client *and* a credential object, and an
        # `elif` would silently skip `configure()` for it.
        if isinstance(self, LazyClientMixin):
            # Accessing `client` runs the cached `_open_client` (auth).
            _ = self.client
        if (auth := getattr(self, "_auth", None)) is not None:
            auth.configure()
        return self

    @abstractmethod
    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ):
        """Check validity of input dates. Called by `__init__`.

        Still abstract, because the *shape* of a backend's time axis is a real
        design decision rather than boilerplate. Most implementations are one
        call to one of the three factories below —
        :meth:`_whole_window_extent`, :meth:`_cadence_extent`, or
        :meth:`_static_extent` — which cover the three archetypes the 48
        backends fall into; only a genuinely bespoke axis (a provider release
        cadence to snap to, a forecast `(cycle, step)` grid) needs its own body.
        """
        pass

    # ------------------------------------------------------------------
    # TemporalExtent factories.
    #
    # Every backend's `_check_input_dates` used to re-derive one of three
    # shapes by hand, which is how the cadence bug (a `.get(..., "D")` that
    # silently substituted daily) reached seven backends. Building the extent
    # through these keeps the parsing, the cadence validation, and the
    # `dates` axis consistent.
    # ------------------------------------------------------------------

    def _whole_window_extent(
        self,
        start: Any,
        end: Any,
        fmt: str,
        resolution: str = "all",
    ) -> TemporalExtent:
        """Build the extent for a backend that queries the window in one request.

        The archetype for a provider whose API takes a date *range* rather
        than one date per file (an event feed, an occurrence search, a station
        query): there is no per-step download loop, so `dates` carries just the
        two bounds and `resolution` is a label rather than a pandas frequency.

        Args:
            start: The requested start bound, in any form
                :func:`~earthlens.base.to_datetime` accepts.
            end: The requested end bound.
            fmt: `strptime` format tried first for a string bound.
            resolution: The label to record — conventionally `"all"` (one
                query spans the window), or the backend's own cadence word
                where that is more informative.

        Returns:
            TemporalExtent: The window, with `dates` holding `[start, end]`.
        """
        import pandas as pd

        from earthlens.base._dates import to_datetime

        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=resolution,
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _cadence_extent(
        self,
        start: Any,
        end: Any,
        fmt: str,
        cadence: str,
        accepted: Mapping[str, str],
    ) -> TemporalExtent:
        """Build the extent for a backend that loops over one step per cadence.

        The archetype for a provider addressed one file / request per period.
        The cadence is resolved through
        :func:`~earthlens.base.resolve_cadence`, so an unsupported or mistyped
        spelling raises instead of silently substituting a different cadence,
        and `dates` is the expanded period axis the download loop iterates.

        Args:
            start: The requested start bound.
            end: The requested end bound.
            fmt: `strptime` format tried first for a string bound.
            cadence: The user-facing cadence (`temporal_resolution`).
            accepted: This backend's `{cadence: pandas offset alias}` map.

        Returns:
            TemporalExtent: The window, with `dates` holding one entry per
                period start.

        Raises:
            ValueError: If `cadence` is not a key of `accepted`.
        """
        from earthlens.base._dates import date_windows, resolve_cadence, to_datetime

        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        resolution = resolve_cadence(cadence, accepted, backend=type(self).__name__)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=resolution,
            dates=date_windows(start_dt, end_dt, resolution),
        )

    def _static_extent(self, resolution: str = "static") -> TemporalExtent:
        """Build the extent for a backend whose product has no time axis.

        The archetype for a time-invariant product (elevation, soil
        properties, a long-term resource climatology): both bounds are `None`
        and `dates` is empty, so nothing downstream tries to iterate a time
        axis that does not exist.

        Args:
            resolution: The label to record. Defaults to `"static"`.

        Returns:
            TemporalExtent: An empty, boundless extent.
        """
        import pandas as pd

        return TemporalExtent(
            start_date=None,
            end_date=None,
            resolution=resolution,
            dates=pd.DatetimeIndex([]),
        )

    def _initialize(self, *args: Any, **kwargs: Any) -> Any:
        """Prepare the backend before the extents are built; return its client.

        Called once by :meth:`__init__`, before :meth:`_create_grid` and
        :meth:`_check_input_dates`. A non-`None` return is captured onto
        `self.client`.

        The default does nothing and returns `None` — the right behaviour for a
        backend that needs no eager setup, which is half of them (an anonymous
        HTTP/FTP endpoint, or a lazily-imported stateless SDK). Backends that
        must resolve a catalog row, build an auth object, or open a client
        override it. A backend whose client is a *network* connection should
        prefer :class:`LazyClientMixin` and keep `_initialize` offline, so
        construction never touches the network.

        Returns:
            `None` by default; an override returns the client to bind onto
            `self.client`.
        """
        return None

    def _create_grid(
        self, lat_lim: list[float], lon_lim: list[float]
    ) -> SpatialExtent | dict | None:
        """Turn the requested lat/lon bounds into this backend's spatial extent.

        Called once by :meth:`__init__`; the result is captured onto
        `self.space`.

        The default wraps the bounds verbatim in a validated
        :class:`SpatialExtent`, which is what all but a handful of backends
        need — most providers accept an arbitrary WGS84 bbox and do any
        snapping server-side. Override only to do real work on the bounds:
        snap them to the provider's grid (ecmwf), attach a native cell size
        (chc), split an antimeridian-crossing box (stac), or ignore them for a
        global-only product (climate_indices, risk_indicators).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: The validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    @abstractmethod
    def download(self):
        """Download every requested variable and return the produced artifacts.

        The return shape tracks :attr:`OUTPUT_KIND`: `"raster"` /
        `"mixed"` file-writing backends return the list of written
        paths (`list[Path]`); `"vector"` backends return an in-memory
        `FeatureCollection` (radar returns a `GeoDataFrame`);
        `"tabular"` backends return a `pandas.DataFrame`. Every backend
        now returns its produced artifacts (the legacy CHIRPS / ECMWF
        backends return their written `list[Path]` and also leave the
        files on disk under `self.root_dir`).

        Partial-failure policy is per backend and currently varies:
        most multi-item backends are **skip-and-continue** — a single
        failed `(dataset, variable)` / chunk / sensor is logged and the
        batch proceeds, with a success/failure summary at the end
        (CHIRPS, CMEMS, FDSN, FIRMS, …) — while single-shot backends
        propagate the error. NWP exposes this as an explicit
        `errors="warn" | "raise" | "ignore"` argument; new backends
        with a per-item loop should follow that `errors=` convention so
        the policy becomes uniformly caller-controllable.
        """
        # loop over dates if the downloaded rasters/netcdf are for a specific date out of the required
        # list of dates
        pass

    def _download_dataset(self):
        """Download a single variable/dataset (called by :meth:`download`)."""
        pass

    def _api(self, *args: Any, **kwargs: Any) -> Any:
        """Send / receive the request(s) this download needs.

        Called by :meth:`download` (or :meth:`_download_dataset`). The default
        is the search→fetch composition, :meth:`_api_via_search_fetch`, which
        is what a backend built on the :meth:`_search` / :meth:`_fetch` split
        wants — the great majority. Override it only for a backend that talks
        to its provider in one indivisible step and has no listable product
        set (chc composes an FTP path per date; ecmwf queues a CDS job; gee
        builds an ee chain), or one whose `_fetch` takes no product list.

        Returns:
            Whatever :meth:`_fetch` returned — see :meth:`_fetch` for the
            element type, which tracks :attr:`OUTPUT_KIND`.

        Raises:
            NotImplementedError: If the backend overrides neither this method
                nor the :meth:`_search` / :meth:`_fetch` pair.
        """
        return self._api_via_search_fetch()

    # ------------------------------------------------------------------
    # C3 — optional search/fetch decomposition.
    #
    # The existing four backends (CHIRPS, S3, ECMWF, GEE) keep their
    # `_api` overrides unchanged: nothing below is abstract, so they do
    # not have to implement `_search` / `_fetch` to stay green.
    #
    # New backends (earthlens.stac, earthlens.earthdata, earthlens.fdsn,
    # earthlens.openaq, …) should override `_search` and `_fetch`
    # instead — `_search` returns a list of `RemoteProduct`s and
    # `_fetch` consumes them. The :meth:`_api_via_search_fetch` helper
    # is the canonical composition; backends can opt into it by
    # overriding `_api` as `return self._api_via_search_fetch()`.
    # ------------------------------------------------------------------

    def _search(self) -> list[RemoteProduct]:
        """List the remote products that satisfy this download request.

        Default raises `NotImplementedError` so backends that do not
        opt into the search/fetch split (the four shipped before C3)
        keep their `_api`-only flow unchanged. Backends that opt in
        override this to return one `RemoteProduct` per item the
        server's catalog says they should download.

        The split exists to make dry-run inspection cheap (`_search`
        does not hit the bulk-download endpoint) and to make
        per-product parallelism explicit (`_fetch` is the
        parallelisable half).

        Returns:
            list[RemoteProduct]: One item per product to download.
                The empty list is a legal result (the catalog matched
                nothing) and short-circuits `_api_via_search_fetch`
                without ever calling `_fetch`.

        Raises:
            NotImplementedError: When the subclass keeps the legacy
                `_api`-only flow. The message names the subclass
                class so the user can find the offending backend.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement _search; "
            f"either override _api directly (legacy) or override both "
            f"_search and _fetch (post-C3)."
        )

    def _count(self) -> int:
        """Return how many products :meth:`_search` would yield, without fetching.

        Default implementation runs :meth:`_search` and counts the
        result. Backends with a cheap server-side total (e.g. a STAC
        `numberMatched` read with `limit=1`) should override this to
        avoid materialising the whole product list.

        Returns:
            int: The number of products the current request matches.

        Raises:
            NotImplementedError: When the backend keeps the legacy
                `_api`-only flow and implements no :meth:`_search`.
        """
        return len(self._search())

    def _fetch(self, products: list[RemoteProduct]) -> list[Any]:
        """Download the bytes of every product `_search` returned.

        Default raises `NotImplementedError` (see `_search`).
        Backends that opt into the search/fetch split override this
        to iterate over `products` — either sequentially or via
        `joblib.Parallel` / `concurrent.futures` — and write each
        one to disk (or build it in memory).

        Args:
            products: The list returned by `_search` (or a
                user-filtered subset). The empty list is allowed and
                returns an empty list.

        Returns:
            list[Any]: One element per product, in `products` order.
                The element type tracks :attr:`OUTPUT_KIND`: written
                `Path`s for `"raster"` / `"mixed"`, `FeatureCollection`
                fragments for `"vector"`, and `DataFrame` fragments for
                `"tabular"` (these are concatenated by the backend's
                `download`). Empty list when `products` is empty (no-op
                fetch is legal).

        Raises:
            NotImplementedError: When the subclass keeps the legacy
                `_api`-only flow.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement _fetch; "
            f"either override _api directly (legacy) or override both "
            f"_search and _fetch (post-C3)."
        )

    def _api_via_search_fetch(self) -> list[Any]:
        """Canonical `_api` body for backends using the C3 split.

        Backends that override `_search` and `_fetch` usually want
        `_api` to just compose them; this helper is that
        composition, factored once so each new backend's `_api`
        body becomes a single line:

        ```python
        def _api(self):
            return self._api_via_search_fetch()
        ```

        The helper short-circuits on an empty search result so
        `_fetch` is only called when there is something to fetch —
        a tiny but meaningful win when many backends are queried in
        parallel and most return nothing.

        Returns:
            list[Any]: Whatever `_fetch` returned (element type tracks
                :attr:`OUTPUT_KIND` — see :meth:`_fetch`). An empty list
                when `_search` returned no products.
        """
        products = self._search()
        if not products:
            return []
        return self._fetch(products)

    def _fetch_one(self, product: RemoteProduct) -> Any:
        """Fetch a single product — the per-product hook for `_search_fetch_each`.

        Default raises `NotImplementedError`. Backends that want a
        per-item progress bar override this (instead of, or alongside,
        the whole-list `_fetch`) so `_search_fetch_each` can map it over
        the `_search` results under a `tqdm` bar.

        Raises:
            NotImplementedError: When the backend does not opt into the
                per-product fetch hook.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement _fetch_one."
        )

    def _search_fetch_each(
        self,
        *,
        progress_bar: bool = False,
        desc: str | None = None,
        unit: str = "item",
    ) -> list[Any]:
        """C3 composition with an optional per-product `tqdm` progress bar.

        Like :meth:`_api_via_search_fetch`, but maps the per-product
        :meth:`_fetch_one` hook over the `_search` results so a `tqdm`
        bar can show per-item progress — the shared form of the
        progress-aware composition several backends (FIRMS, OpenAQ)
        previously duplicated. Backends that fetch the whole product
        list at once, or need bespoke progress / partial-failure
        handling (e.g. CMEMS), keep their own composition.

        Args:
            progress_bar: Show the per-product `tqdm` bar when `True`.
            desc: `tqdm` description; defaults to the class name.
            unit: `tqdm` unit label.

        Returns:
            list[Any]: One :meth:`_fetch_one` result per product
                (element type tracks :attr:`OUTPUT_KIND`), or `[]` when
                `_search` matched nothing.
        """
        products = self._search()
        if not products:
            return []
        from tqdm import tqdm

        iterator = tqdm(
            products,
            disable=not progress_bar,
            desc=desc or type(self).__name__,
            unit=unit,
        )
        return [self._fetch_one(product) for product in iterator]


class AbstractCatalog(BaseModel):
    """Abstract base class for per-data-source variable catalogs.

    Subclasses load a backend-specific catalog (a YAML file, an
    in-code dict, or a remote query) in :meth:`get_catalog` and
    expose individual entries via :meth:`get_variable`. The
    :func:`model_post_init` hook eagerly populates :attr:`catalog`
    after pydantic validation runs, so subclasses can treat the
    catalog as a dict thereafter without writing their own
    `__init__`.

    Subclasses pass through pydantic's normal `BaseModel.__init__`
    — declare any backend-specific construction parameters as
    pydantic fields rather than `__init__` arguments. Override
    :meth:`get_catalog` (and optionally :meth:`get_variable`); the
    base implementations raise :class:`NotImplementedError` to flag
    a missing override at first use rather than silently returning
    an empty mapping.

    Attributes:
        catalog: The full catalog mapping returned by
            :meth:`get_catalog`. Populated post-init; defaults to an
            empty dict so the field is always present. Type and
            shape are backend-specific (a concrete subclass typically
            stores typed value objects, e.g. `dict[str, Variable]`
            for the ECMWF backend).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: Short label used by :meth:`get_dataset`'s did-you-mean error
    #: message — concrete subclasses override (e.g. `"GEE catalog"`,
    #: `"CDS catalog"`, `"CHC catalog"`) so the user sees which
    #: catalog they failed against.
    _catalog_kind: str = "catalog"

    #: Plural noun for the catalog entries, used in :meth:`get_dataset`'s
    #: did-you-mean message (`"Known {noun}: [...]"`). Defaults to
    #: `"datasets"`; subclasses whose entries are not "datasets" override
    #: it (e.g. `"parameters"` for the openaq / usgs_water catalogs).
    _entry_noun: str = "datasets"

    catalog: dict[str, Any] = Field(default_factory=dict)
    available_datasets: list[str] = Field(default_factory=list)
    datasets: dict[str, Any] = Field(default_factory=dict)
    providers: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Populate :attr:`catalog` after pydantic validation runs.

        Pydantic calls this hook automatically; subclasses that need
        their own post-init wiring should override it and call
        `super().model_post_init(__context)` first to keep the
        catalog-loading behaviour.
        """
        self.catalog = self.get_catalog()

    def get_catalog(self) -> Any:
        """Read the catalog of the datasource from disk or retrieve it from server.

        Abstract; concrete subclasses must override and return their
        backend-specific catalog object (e.g. a pydantic `Catalog`
        instance, a `dict`, or whatever shape the backend uses).

        Raises:
            NotImplementedError: Always, until overridden by a subclass.
        """
        raise NotImplementedError

    def get_variable(self, dataset_key: str, variable_name: str) -> Any:
        """Return one leaf (variable / band / asset) of a dataset.

        Shared two-argument contract for the two-level catalogs: a leaf
        is addressed by its `(dataset_key, variable_name)` pair, because
        the same leaf code can appear under more than one dataset (e.g.
        `"2m-temperature"` lives under several CDS datasets). Concrete
        overrides return their typed leaf row and raise `ValueError`
        (with a did-you-mean hint) on an unknown key:

        * chc / ecmwf / cmems — return a `Variable`.
        * gee — return a `Band` (also exposed as `get_band`).
        * firms — return a `SensorColumn` (also exposed as `get_column`).
        * tropycal — return a `TrackField` (also exposed as `get_field`).

        Single-level catalogs (where one row *is* the leaf — fdsn, gdacs,
        radar, openaq, overture, usgs_water) do not implement this; their
        rows are addressed directly with :meth:`get_dataset` / `[key]`.

        Note:
            This supersedes the former single-argument
            `get_variable(var_name)`, which returned `self.catalog.get(var_name)`.
            External callers/subclassers that relied on the one-argument
            form must pass the parent `dataset_key` as well.

        Args:
            dataset_key: The parent dataset / collection key.
            variable_name: The leaf code within that dataset.

        Returns:
            The backend-specific leaf row.

        Raises:
            NotImplementedError: If the backend has no per-dataset leaf
                level.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no per-dataset variable level; "
            "address its rows with get_dataset() / [key]."
        )

    # -- shared dict-like surface over `datasets` (M1 from catalog-cross-backend-comparison)

    def get_dataset(self, name: str) -> Any:
        """Return the dataset record for `name`, with a did-you-mean hint on miss.

        Backend-generic: looks up `name` in :attr:`datasets` and raises
        `ValueError` (not `KeyError`) with the closest known name when
        absent. Concrete subclasses can override to narrow the return
        type or customise the error message.

        Args:
            name: Catalog key (e.g. CDS dataset short name, EE asset id,
                CHC dataset key).

        Returns:
            The matching dataset record (type depends on the subclass).

        Raises:
            ValueError: If `name` is not a key of :attr:`datasets`.
        """
        try:
            return self.datasets[name]
        except KeyError:
            close = difflib.get_close_matches(name, self.datasets, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{name!r} is not in the {self._catalog_kind}. "
                f"Known {self._entry_noun}: {sorted(self.datasets)}.{hint}"
            ) from None

    def __getitem__(self, name: str) -> Any:
        """`cat[name]` — dict-style lookup; raises `KeyError` on miss."""
        try:
            return self.get_dataset(name)
        except ValueError as exc:
            raise KeyError(name) from exc

    def __contains__(self, name: object) -> bool:
        """`name in cat` — True when `name` is a curated dataset."""
        return name in self.datasets

    def __iter__(self):
        """Iterate over the curated dataset keys."""
        return iter(self.datasets)

    def __len__(self) -> int:
        """Number of curated datasets in the catalog."""
        return len(self.datasets)

    def __repr__(self) -> str:
        """Compact developer repr — counts, not contents."""
        return (
            f"{type(self).__name__}(datasets={len(self.datasets)}, "
            f"available_datasets={len(self.available_datasets)})"
        )

    def get_provider(self, slug: str) -> Any:
        """Return the provider record for `slug` (with a did-you-mean hint on miss).

        The value type depends on the backend's :attr:`providers` field:
        most backends store an :class:`earthlens.base.Provider`, but some
        mirror a domain-specific record (earthdata mirrors its
        `EarthdataDAAC` from `daacs`, stac its `Endpoint` from
        `endpoints`).

        Args:
            slug: A registered provider slug (e.g. `"nasa-lp-daac"`,
                `"ucsb-chc"`, `"copernicus"`).

        Returns:
            The matching provider record (a `Provider`, or the backend's
            domain-specific provider model).

        Raises:
            ValueError: If `slug` is not a registered provider.
        """
        try:
            return self.providers[slug]
        except KeyError:
            close = difflib.get_close_matches(slug, self.providers, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{slug!r} is not a registered provider. "
                f"Known providers: {sorted(self.providers)}.{hint}"
            ) from None

    def resolve(self, key: str, *args: Any, **kwargs: Any) -> Any:
        """Map a user-facing key to the concrete thing a request needs.

        Shared convention for every backend that implements a resolve
        step: take a *logical* catalog key (a friendly name, collection
        key, or model key) and return the backend-specific value the
        download path consumes. The return type and any extra
        positional / keyword arguments are backend-specific by
        necessity — the catalogs resolve to different things — so this
        base method only fixes the *verb*, not the signature. The
        concrete overrides:

        * `nwp.resolve(model_key)` / `usgs_water.resolve(code_or_name)`
          — return a model key / 5-digit parameter code (`str`).
        * `stac.resolve(endpoint, collection_key)` — return the upstream
          collection id for that endpoint (`str`).
        * `openeo.resolve(key)` / `sentinel_hub.resolve(key)` — return a
          normalised request object (a `ResolvedGraph` / `ResolvedRequest`)
          covering both plain collections and recipes.
        * `earthdata.resolve(key, daac=None)` /
          `eumetsat.resolve(key, group=None)` — return the dataset row,
          with an optional second argument to disambiguate a key shared
          across DAACs / mission groups.

        Backends without a resolve step address their catalog directly
        through :meth:`get_dataset` / `__getitem__`.

        Args:
            key: The logical catalog key to resolve.
            *args: Backend-specific positional arguments (e.g. the STAC
                endpoint).
            **kwargs: Backend-specific keyword arguments (e.g.
                `daac=` / `group=`).

        Returns:
            The backend-specific resolved value (see the override list).

        Raises:
            NotImplementedError: If the backend has no resolve step.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no resolve() step; address its "
            "catalog with get_dataset() / [key] instead."
        )

    def __str__(self) -> str:
        """Pretty-print the curated `datasets` map as YAML.

        `None`-valued fields are omitted so the output stays readable;
        the ordering of keys follows insertion. Concrete subclasses
        whose dataset values aren't pydantic `BaseModel`s (rare) must
        override.
        """
        import yaml

        body = {}
        for key, dataset in self.datasets.items():
            if isinstance(dataset, BaseModel):
                body[key] = dataset.model_dump(exclude_none=True)
            else:
                body[key] = dataset
        dumped = yaml.safe_dump(
            body, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
        return cast(str, dumped)
