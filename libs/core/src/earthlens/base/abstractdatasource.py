from __future__ import annotations

import difflib
import functools
import inspect
import stat
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, cast

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from earthlens.config import resolve_output_path

if TYPE_CHECKING:
    from earthlens.base.http import HttpClient

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

    Issued when a request passes a real polygon area of
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


def _head_rows(chunk: Any, count: int) -> Any:
    """Return the first `count` rows of a fragment.

    The default trimmer for :meth:`AbstractDataSource._take_limited` and
    :meth:`AbstractDataSource.iter_download`. Prefers `.iloc` when the fragment
    has it, because slicing a pandas object is label-based for some index types
    and would not reliably keep the first n rows; falls back to `[:n]`, which
    covers lists and `FeatureCollection`-style sequences.

    Args:
        chunk: The fragment to trim.
        count: How many leading rows to keep.

    Returns:
        Any: The trimmed fragment, of the same type.
    """
    positional = getattr(chunk, "iloc", None)
    if positional is not None:
        return positional[:count]
    return chunk[:count]


#: "Nothing was produced" marker for the per-item loops. A plain `None` cannot
#: serve: an `on_failure` hook is free to return `None` as a real placeholder,
#: and that has to stay distinguishable from having no placeholder at all.
_MISSING = object()


def _missing_ancestors(directory: Path) -> list[Path]:
    """Return `directory` and each missing parent, leaf first.

    Args:
        directory: The output directory about to be created.

    Returns:
        list[Path]: The paths that do not exist yet, nearest first, so a
            failure can remove exactly what the call went on to create.
    """
    missing: list[Path] = []
    probe = directory
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    return missing


def _unwind_created(created: list[Path]) -> None:
    """Remove directories this call created, stopping at the first non-empty one.

    A request the backend rejects (an unsupported `aggregate=`, a bad dataset
    key) must not leave an output directory behind. Only the directories the
    call created are removed, and only while each is still empty, so a
    pre-existing tree and anything a partially-successful download wrote are
    both left alone.

    Args:
        created: The paths from :func:`_missing_ancestors`, leaf first.
    """
    for directory in created:
        try:
            directory.rmdir()
        except OSError:
            break


@functools.cache
def _parameters(function: Any) -> frozenset[str]:
    """Return the parameter names `function` accepts.

    Cached: this runs on every `download` call, and a function's signature does
    not change once it is defined.

    Args:
        function: The unwrapped `download` to inspect.

    Returns:
        frozenset[str]: Its parameter names.
    """
    return frozenset(inspect.signature(function).parameters)


def _passed_aggregate(function: Any, args: tuple[Any, ...], kw: dict[str, Any]) -> bool:
    """Whether this call supplied a non-`None` `aggregate`, positionally or not.

    Args:
        function: The unwrapped `download` whose signature names the parameters.
        args: Positional arguments the caller passed, `self` excluded.
        kw: Keyword arguments the caller passed.

    Returns:
        bool: `True` when an `aggregate` argument arrived with a value.
    """
    if kw.get("aggregate") is not None:
        return True
    if not args:
        return False
    try:
        bound = inspect.signature(function).bind_partial(None, *args, **kw)
    except TypeError:
        # A call that does not match the signature will raise on its own once
        # the wrapper forwards it; refusing here would report the wrong error.
        return False
    return bound.arguments.get("aggregate") is not None


#: Annotations for the parameters `__init_subclass__` appends to a backend's
#: signature. Declared once so `__signature__` and `__annotations__` cannot
#: drift apart, and typed as loosely as the wrapper actually accepts: `aoi`
#: takes a bbox, a point, a shapely geometry, GeoJSON, WKT or a GeoDataFrame.
#:
#: Real objects, not strings. `functools.wraps` copies `__module__` onto the
#: wrapper, so a stringified annotation would be resolved in the *backend's*
#: namespace — where `Any` is usually not imported — and
#: `typing.get_type_hints` / `pydantic.validate_call` would raise `NameError`.
#: That is the exact tooling this table exists to keep working.
_ERGONOMIC_ANNOTATIONS: dict[str, Any] = {
    "aoi": Any,
    "buffer": float | None,
    "cadence": str | None,
    "dataset": str | None,
}


def native_parameters(backend_cls: type) -> frozenset[str]:
    """Parameter names a backend's own `__init__` declares.

    The `__init_subclass__` wrapper advertises `aoi`, `buffer`, `cadence` and
    `dataset` on every backend's signature so they are discoverable, which means
    a plain `"aoi" in inspect.signature(cls.__init__).parameters` can no longer
    distinguish a backend that interprets `aoi=` *itself* (WorldPop's ISO3 /
    GeoDataFrame form) from one the wrapper resolves for. Callers that need that
    distinction ask here instead of introspecting directly.

    Args:
        backend_cls: An `AbstractDataSource` subclass.

    Deliberately uncached, unlike the sibling `_parameters` helper. Keying a
    cache on the class would retain every class ever passed — including the
    throwaway ones tests build — and the premise that a backend's parameters
    cannot change is not quite true here: `__init_subclass__` replaces
    `__init__`, and a caller may patch it again, after which a cached answer
    would be wrong. `inspect.signature` is cheap enough that neither risk is
    worth taking.

    Returns:
        frozenset[str]: The declared names, minus the ergonomic ones the
        wrapper synthesised. Empty when the signature cannot be read.

    Examples:
        - CHIRPS takes `aoi=` only through the wrapper, so it is not native
          even though the signature advertises it:
            ```python
            >>> import inspect
            >>> from earthlens.base.abstractdatasource import native_parameters
            >>> from earthlens.chc import CHIRPS
            >>> "aoi" in inspect.signature(CHIRPS.__init__).parameters
            True
            >>> "aoi" in native_parameters(CHIRPS)
            False
            >>> sorted(native_parameters(CHIRPS))[:3]
            ['end', 'fmt', 'lat_lim']
            >>> "self" in native_parameters(CHIRPS)
            False

            ```
        - WorldPop declares its own richer `aoi=`, so it reports as native
          and the facade forwards the value untouched:
            ```python
            >>> from earthlens.base.abstractdatasource import native_parameters
            >>> from earthlens.worldpop import WorldPop
            >>> "aoi" in native_parameters(WorldPop)
            True

            ```
    """
    # `object.__init__` means every real class answers this, so the `None`
    # branch guards only a class that shadows `__init__` with a non-callable —
    # which the tests construct deliberately. It is kept because this is a
    # public helper answering a question about arbitrary input.
    init = getattr(backend_cls, "__init__", None)
    if init is None:
        return frozenset()
    synthetic: frozenset[str] = getattr(init, "_ergonomic_params", frozenset())
    try:
        declared = frozenset(inspect.signature(init).parameters)
    except (TypeError, ValueError):
        return frozenset()
    # `self` is not something a caller can pass. The `inspect.signature(cls)`
    # call this function replaced dropped it implicitly; reading `__init__`
    # directly does not, and the question here is "what does this backend
    # accept?".
    return declared - synthetic - {"self"}


def _describe_remote_product(product: Any) -> str:
    """Render a product for the :meth:`AbstractDataSource._run_items` log lines.

    Args:
        product: The product whose fetch failed. Only its `id` is read, so a
            backend passing anything id-shaped works.

    Returns:
        str: The product id, quoted.
    """
    return repr(getattr(product, "id", product))


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
            `self.OUTPUT_KIND`, emdat copies it from the resolved EM-DAT /
            GDIS row, and tropycal sets `"tabular"` for its
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
        SUPPORTS_AGGREGATE: Whether this backend implements the `aggregate=`
            temporal reduction. `False` (the default) means the parameter is
            refused centrally, so the backend neither declares it nor writes
            its own refusal — `OUTPUT_KIND` alone cannot decide this, because
            plenty of `"raster"` backends (goes, dem, jaxa, radar, …) emit
            grids the reducer has no time axis for. Only the backends that
            actually wire the aggregator set it to `True`.

    Note:
        **Threads.** A backend instance is not safe to share across threads.
        It caches per-request state (`self.space` / `self.time`, an
        `HttpClient`, a lazily-built SDK client), none of it guarded. Give each
        thread its own instance. Where earthlens itself fans out — ghsl's tile
        downloads run through `joblib.Parallel(prefer="threads")` — the shared
        helpers take a session from
        :func:`earthlens.base.http.thread_local_session`, one per thread,
        because `requests.Session` is not guaranteed thread-safe either.

        A consequence worth knowing: `min_interval` throttling is per
        `HttpClient`, so N threads holding N clients each wait
        `min_interval` *independently* — the effective request rate is N times
        what a single-threaded run would produce.

    Note:
        **Processes.** A freshly constructed backend usually pickles, because
        the SDK clients are lazy. Once one materialises — after
        :meth:`authenticate` or the first download — it generally does not, and
        a backend that caches an :class:`~earthlens.base.http.HttpClient` on
        `self._http` (erddap, bathymetry, gee) never does: the client holds a
        `threading.Lock` for the throttle, and locks do not pickle. (A bare
        `requests.Session`, perhaps surprisingly, does.)

        So distribute at the **request** level, not the object level: send the
        request parameters to the worker and construct the backend there. That
        is also the only shape that works with a rate limit, since a throttle
        cannot span processes.
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: Whether the raw `end` bound named a whole calendar day rather than an
    #: instant. Recorded in `_check_input_dates` by the backends that widen an
    #: inclusive `end`; see `earthlens.base.end_is_date_only`. The `False`
    #: default is the conservative one: a backend that never records it does
    #: not widen.
    _end_is_date_only: bool = False

    REQUIRES_TIME_WINDOW: bool = True

    SUPPORTS_POLYGON_AOI: bool = False

    SUPPORTS_AGGREGATE: bool = False

    #: Optional sentence explaining why this backend refuses `aggregate=`,
    #: appended to the central message by
    #: :meth:`_refuse_unsupported_aggregate`. Worth setting: the specific reason
    #: ("a single static prediction with no temporal axis") is more use to a
    #: caller than the generic one.
    AGGREGATE_REFUSAL_REASON: str = ""

    #: Minimum seconds between consecutive requests to this provider — a
    #: client-side politeness limit, passed to
    #: :class:`~earthlens.base.http.HttpClient`'s `min_interval`. `0.0` (the
    #: default) means the provider publishes no etiquette we are bound by.
    #:
    #: Declared on the backend rather than read from the `providers.yaml`
    #: registry, which cannot answer it: only ecmwf, earthdata and gee ship one,
    #: and all three authenticate through an SDK, while the backends that
    #: actually get rate-limited (osm's Overpass / ohsome) have no provider
    #: record at all.
    #:
    #: The throttle is per :class:`HttpClient`, so a backend fanning out over N
    #: threads with a client each waits `min_interval` N times in parallel — see
    #: the concurrency note on this class.
    MIN_REQUEST_INTERVAL: float = 0.0

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Give every backend its `download` wrapper and constructor sugar.

        Two independent pieces of wiring run for each concrete backend:

        1. :meth:`_wrap_download` wraps whichever `download` the class
           defines so :attr:`root_dir` is created when a download starts
           rather than at construction. This runs for *every* subclass,
           including one that declares no `__init__` of its own.
        2. The backend's `__init__` is wrapped so that — whether reached
           through the `EarthLens` facade or by constructing the backend
           class directly — it also accepts the ergonomic kwargs below.

        The constructor sugar adds:

        * `aoi` (+ `buffer`): any shape :func:`earthlens.base.spatial.normalize_aoi`
          understands, reduced to `lat_lim` / `lon_lim`; a backend that
          declares its own `aoi` (WorldPop) keeps it;
        * `cadence`: a clearer alias for `temporal_resolution`;
        * `dataset`: split out of a single-key `variables` dict (or passed
          through to a backend with a native `dataset`, e.g. S3).

        The original `__init__` is preserved as the wrapper's `__wrapped__`,
        so signature introspection (e.g. `EarthLens.options_for`) and the
        facade's kwarg validation still see the backend's real parameters.

        Raises:
            TypeError: When a backend subclasses another backend without
                passing `ergonomics_resolved=True`. Both classes get an
                `__init__` wrapper, so if the child forwards an ergonomic kwarg
                up to `super().__init__()` the parent's wrapper resolves it a
                second time — `resolve_aoi` runs twice and the second call sees
                an already-reduced bbox. All 48 backends inherit
                `AbstractDataSource` directly, so this has never fired; it is
                checked rather than left as a comment because the failure is
                silent, and a plausible-looking bbox over roughly the right area
                is the hardest kind of wrong output to notice.

                A subclass that genuinely wants this declares
                `class Child(Parent, ergonomics_resolved=True)`, which says "my
                `__init__` forwards only the already-resolved native parameters
                (`lat_lim` / `lon_lim` / `temporal_resolution` / `variables`)"
                and skips the second wrap.
        """
        resolved = kwargs.pop("ergonomics_resolved", False)
        super().__init_subclass__(**kwargs)
        backend_bases = [
            base
            for base in cls.__bases__
            if base is not AbstractDataSource
            and isinstance(base, type)
            and issubclass(base, AbstractDataSource)
        ]
        # Only a child that declares its *own* `__init__` is at risk: that is
        # what earns a second wrapper. A child without one inherits the parent's
        # already-wrapped constructor and cannot double-resolve anything, which
        # is why the test helpers and any mixin-style subclass stay legal.
        if backend_bases and not resolved and "__init__" in cls.__dict__:
            names = ", ".join(base.__name__ for base in backend_bases)
            raise TypeError(
                f"{cls.__name__} declares its own __init__ and subclasses the "
                f"backend(s) {names}, so both classes carry an __init__ wrapper "
                f"and an ergonomic kwarg forwarded to super().__init__() would "
                f"be resolved twice. Forward only the resolved native "
                f"parameters (lat_lim, lon_lim, temporal_resolution, variables) "
                f"and declare `class {cls.__name__}({names}, "
                f"ergonomics_resolved=True)`."
            )
        if resolved and not backend_bases:
            # The promise is about a *parent's* wrapper, and there is no backend
            # parent here. Honouring it would silently drop the ergonomic
            # kwargs (aoi / buffer / cadence / dataset) from a backend that has
            # no second wrapper to avoid — the opposite of what the flag means.
            raise TypeError(
                f"{cls.__name__} passes ergonomics_resolved=True but inherits "
                f"AbstractDataSource directly, so there is no parent wrapper to "
                f"avoid. The flag would only disable this class's own ergonomic "
                f"kwargs (aoi=, buffer=, cadence=, dataset=). Drop it."
            )
        # Every subclass needs this, independent of the constructor sugar
        # below: a backend that inherits `__init__` unchanged still needs its
        # `download` to create `root_dir`.
        cls._wrap_download()
        if resolved:
            # The child promises it forwards only resolved parameters, so it
            # keeps the parent's `__init__` wrapper and gains no second one.
            return
        orig = cls.__dict__.get("__init__")
        if orig is None or getattr(orig, "_ergonomic", False):
            return
        native_signature = inspect.signature(orig)
        params = native_signature.parameters
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
        # `functools.wraps` copies `__wrapped__`, and `inspect.signature`
        # follows it by default — so without this the four parameters the
        # wrapper adds are invisible to `help()`, to IDE autocomplete and to
        # every signature-driven tool, on a backend that accepts them happily
        # at runtime. Re-advertise them as keyword-only, appended to whatever
        # the backend already declares, and drop any the backend names itself
        # so a native `aoi=` / `dataset=` is not listed twice.
        existing = set(params)
        # A backend that declares neither `variables` nor `**kwargs` addresses
        # its data by facet keywords (cmip6's `variable_id=` / `source_id=`), so
        # `dataset=` can never reach anything there; and `buffer=` only shapes a
        # point `aoi=` that the wrapper itself resolves, so it is meaningless on
        # a backend that interprets `aoi=` natively. Advertising either would
        # promise a parameter whose only possible outcome is an error.
        facet_only = "variables" not in existing and not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        unsupported = set()
        if facet_only:
            unsupported.add("dataset")
        if native_aoi:
            unsupported.add("buffer")
        extra = [
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=_ERGONOMIC_ANNOTATIONS[name],
            )
            for name in ("aoi", "buffer", "cadence", "dataset")
            if name not in existing and name not in unsupported
        ]
        __init__._ergonomic_params = frozenset(  # type: ignore[attr-defined]
            p.name for p in extra
        )
        if extra:
            declared = list(params.values())
            # Keyword-only parameters must precede any **kwargs.
            var_kw = [p for p in declared if p.kind is inspect.Parameter.VAR_KEYWORD]
            head = [p for p in declared if p.kind is not inspect.Parameter.VAR_KEYWORD]
            __init__.__signature__ = native_signature.replace(  # type: ignore[attr-defined]
                parameters=head + extra + var_kw
            )
            # `functools.wraps` gave the wrapper the native's annotations —
            # under PEP 649 (3.14) by copying `__annotate__`, so the two share
            # one source rather than a dict. Without this assignment the
            # signature and the annotations disagree and anything that pairs
            # them (pydantic `validate_call`, signature-driven CLI builders)
            # sees an untyped parameter. Rebinding rather than mutating is what
            # makes it safe: reading `__annotations__` materialises a dict from
            # the shared `__annotate__`, and assigning a new one detaches the
            # wrapper (3.14 sets its `__annotate__` to `None`), so the native
            # function keeps the annotations it declared.
            __init__.__annotations__ = {
                **__init__.__annotations__,
                **{p.name: _ERGONOMIC_ANNOTATIONS[p.name] for p in extra},
            }
        cls.__init__ = __init__  # type: ignore[method-assign]

    @classmethod
    def _wrap_download(cls) -> None:
        """Make the backend's own `download` create `root_dir` before it runs.

        `root_dir` is resolved at construction but deliberately not created
        there (see :meth:`_ensure_root_dir`). Rather than make every backend
        remember to call it, :meth:`__init_subclass__` calls this so the
        directory exists the moment a real download starts.

        The wrap is applied only to a `download` the class defines itself, and
        only once: a subclass that inherits `download` unchanged already
        inherits a wrapped one, and re-running this on an
        already-wrapped method is a no-op. `functools.wraps` keeps the
        backend's own name, docstring and signature introspectable, so the
        docs build and anything reflecting over `download` still sees the real
        method. (`EarthLens.options_for` reads `__init__`, not `download`, so
        it is unaffected either way.)
        """
        original = cls.__dict__.get("download")
        if original is None or getattr(original, "_ensures_root_dir", False):
            return

        @functools.wraps(original)
        def download(self, *args, **kw):
            # One place refuses `aggregate=`. Previously 40 backends each
            # declared the parameter and wrote their own `NotImplementedError`,
            # so the policy was stated 40 times and the argument sat in the
            # signature of backends it meant nothing to.
            #
            # Both conditions have to permit it, and they answer different
            # questions. `SUPPORTS_AGGREGATE` is per class — does this backend
            # wire the reducer at all. `OUTPUT_KIND` is per *instance* for a few
            # backends (earthdata, eumetsat, tropycal, cmems, emdat) whose shape is
            # only known once the dataset resolves: cmems supports aggregation
            # for its gridded datasets and must still refuse it for a vector
            # one.
            #
            # Bound against the real signature rather than read out of `kw`:
            # `aggregate` is the second positional parameter on the backends
            # that declare it, so `download(False, cfg)` would slip past a
            # keyword-only lookup and be silently ignored — the refusal these
            # backends used to raise themselves.
            if _passed_aggregate(original, args, kw):
                self._refuse_unsupported_aggregate()
            # `aggregate=None` means "not asking for one", and it worked on the
            # ~40 backends that each declared the parameter before the refusal
            # was centralised. Removing it from their signatures turned that
            # call into a `TypeError`, which is a break for any caller that
            # forwards the argument unconditionally. Absorb it here for the
            # backends that no longer name it; the non-`None` case was already
            # refused above.
            if "aggregate" in kw and "aggregate" not in _parameters(original):
                kw.pop("aggregate")
            # Recorded *before* creating them, so the failure path can unwind
            # exactly what this call added.
            created = _missing_ancestors(self.root_dir)
            self._ensure_root_dir()
            try:
                return original(self, *args, **kw)
            except BaseException:
                _unwind_created(created)
                raise

        download._ensures_root_dir = True  # type: ignore[attr-defined]
        cls.download = download  # type: ignore[method-assign]

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
        * `self.space` — the :class:`SpatialExtent` returned by
          :meth:`_create_grid`.
        * `self.time` — the :class:`TemporalExtent` returned by
          :meth:`_check_input_dates`.
        * `self.root_dir` — the absolute :class:`pathlib.Path` of the
          output directory. `self.path` is kept as a legacy alias so
          older backends (CHIRPS, S3) continue to work. The directory is
          *resolved* here but only *created* when a download actually
          runs (see :meth:`_ensure_root_dir`), so merely constructing a
          backend — to read its catalog, inspect its options, or validate
          a request — never litters the filesystem.

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
            path: Output directory. Resolved here and created on the first
                download, not at construction. A relative value is anchored to
                the current working directory. When omitted (`None`) it falls
                back to the configured earthlens output directory
                (`set_output_dir()` / `EARTHLENS_DATA_DIR`, else
                `~/.earthlens/data`); see `earthlens.config`. Pass `path=""` to
                ask for the working directory explicitly. The fallback is
                resolved once, here, so a later `set_output_dir()` does not move
                an already-constructed backend.

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

        # Both hooks return their validated model. They used to be allowed to
        # return a plain dict or `None` instead, and this branched on
        # `isinstance` to cope — three valid answers to one question, which a
        # backend author could not infer without reading this. Every one of the
        # 53 overrides in the tree returns the model, so the other two branches
        # were dead; a hook returning something else now fails here rather than
        # silently leaving `space` / `time` unset.
        self.space = self._create_grid(lat_lim, lon_lim)
        self.time = self._check_input_dates(start, end, temporal_resolution, fmt)

        # An explicit `path=` wins; omitting it entirely falls back to the
        # configured output dir (set_output_dir() / $EARTHLENS_DATA_DIR) so a
        # project can be pointed at one location without threading `path=`.
        # `path=""` stays the documented way to ask for the working directory.
        self.root_dir = resolve_output_path(path)
        self.path = self.root_dir

    def _refuse_unsupported_aggregate(self) -> None:
        """Raise unless this instance can honour a non-`None` `aggregate=`.

        The single implementation of a policy 40 backends used to each write for
        themselves. Two independent questions have to pass:

        * :attr:`SUPPORTS_AGGREGATE` — does the class wire the reducer at all;
        * :attr:`OUTPUT_KIND` — is *this instance's* output gridded. A handful of
          backends resolve their kind per request, so a backend that aggregates
          its raster datasets must still refuse for a vector one.

        A backend may set :attr:`AGGREGATE_REFUSAL_REASON` to a sentence saying
        why, which is appended to the message. That is worth doing: "SoilGrids is
        a single static prediction with no temporal axis" tells a caller
        something the generic sentence cannot.

        Raises:
            NotImplementedError: When either check fails.
        """
        output_kind = getattr(self, "OUTPUT_KIND", "raster")
        griddable = output_kind in {"raster", "mixed"}
        if self.SUPPORTS_AGGREGATE and griddable:
            return
        reason = getattr(self, "AGGREGATE_REFUSAL_REASON", "") or (
            "the temporal reducer needs a gridded output with a time axis to "
            "reduce over, and this request has none"
        )
        raise NotImplementedError(
            f"aggregate= is not supported by {type(self).__name__} "
            f"(OUTPUT_KIND={output_kind!r}): {reason}. Reduce the downloaded "
            f"output yourself, or use a backend that supports it."
        )

    def _is_complete(
        self,
        dest: Path | str,
        expected_size: int | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """Report whether `dest` already holds a usable, complete download.

        The shared form of the skip-if-exists check eight backends each
        hand-rolled as `dest.exists() and dest.stat().st_size > 0`. Routing
        them through one helper means a re-run skips what it already has — so
        a multi-granule job that died halfway carries on from the granule it
        reached, rather than re-fetching the ones already written — and, where
        the caller knows the size, a *truncated* file is no longer mistaken
        for a finished one. This is resumption at the level of whole files:
        `HttpClient.download` never resumes the bytes *within* one.

        "Non-empty" is a weak completeness signal on its own: it is only
        trustworthy because the shared downloader writes to a sibling
        `<dest>.part` and renames on success, so a file present at `dest`
        was never a partial write. Pass `expected_size` whenever the
        provider advertises one (a `Content-Length`, a catalog field) to get
        a real check rather than a proxy.

        Args:
            dest: The output path to test.
            expected_size: Exact size in bytes the finished file must have.
                `None` (the default) falls back to the non-empty check.
            force: When `True`, always report `False` so the caller re-fetches.
                Wire a backend's `force=` download kwarg through here.

        Returns:
            bool: `True` when `dest` can be reused as-is.

        Examples:
            - The check is a pure function of the path, so it can be exercised
              on any backend instance. `libs/core/tests/base/test_hook_defaults.py`
              covers the full matrix: missing, empty, written, wrong size,
              exact size, a directory, and `force=True`.
        """
        if force:
            return False
        dest = Path(dest)
        try:
            info = dest.stat()
        except OSError:
            return False
        # A directory reports a size too, and on Windows that size is 0 — so
        # `expected_size=0` would accept one as a finished download.
        if not stat.S_ISREG(info.st_mode):
            return False
        if expected_size is not None:
            return info.st_size == expected_size
        return info.st_size > 0

    def _ensure_root_dir(self) -> Path:
        """Create :attr:`root_dir` if it does not exist yet, and return it.

        Called by the `download` wrapper installed in
        :meth:`__init_subclass__`, so every backend's output directory exists
        by the time its own `download` body runs — without construction
        itself creating one. Constructing a backend to read its catalog,
        inspect `options_for`, or validate a request is a read-only act and
        must not leave an empty directory behind (it also used to create the
        directory before the request had been validated, so a rejected
        request still made one).

        Creating an existing directory is a no-op, and any missing parent is
        created too, so a backend pointed at a deep path needs no
        preparation from the caller.

        Returns:
            Path: The (now existing) :attr:`root_dir`.

        Note:
            Construction resolves `root_dir` without touching the filesystem;
            this is what creates it. `TestLazyRootDir` in
            `libs/core/tests/base/test_hook_defaults.py` pins both halves.
        """
        self.root_dir.mkdir(parents=True, exist_ok=True)
        return self.root_dir

    def _check_time_window(self, start: Any, end: Any) -> None:
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
        if not self.REQUIRES_TIME_WINDOW:
            return
        missing = [
            name for name, value in (("start", start), ("end", end)) if value is None
        ]
        if not missing:
            return
        raise ValueError(
            f"the {type(self).__name__} backend requires a time window, but "
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
            # The remedy differs by output shape: a raster is post-clipped with
            # pyramids, whereas vector / tabular rows are filtered with a
            # spatial predicate. Advising `Dataset.crop` to a FeatureCollection
            # backend would be useless advice.
            if getattr(self, "OUTPUT_KIND", "raster") in {"raster", "mixed"}:
                remedy = "Post-clip the result with `pyramids.Dataset.crop(mask=...)`"
            else:
                remedy = (
                    "Filter the returned rows to the polygon (e.g. "
                    "`gdf[gdf.within(polygon)]`)"
                )
            warnings.warn(
                f"the {type(self).__name__} backend selects by bounding box only, "
                f"so this polygon aoi= is applied as its bounding box — results "
                f"outside the polygon but inside its bbox are still included. "
                f"{remedy}, or pass a bbox aoi= to make the request's extent "
                f"explicit.",
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
    ) -> TemporalExtent:
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
        *,
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
        *,
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
        from earthlens.base._dates import (
            WHOLE_WINDOW,
            date_windows,
            resolve_cadence,
            to_datetime,
        )

        resolution = resolve_cadence(cadence, accepted, backend=type(self).__name__)
        if resolution == WHOLE_WINDOW:
            # A cadence naming a release *character* rather than a period
            # ("irregular", "climatology", "subdaily", "raw", ...) has no period
            # axis to expand. The caller's own word is kept as the label rather
            # than collapsed to the sentinel, so `self.time.resolution` still
            # reports what was asked for — a backend that logs or serialises the
            # extent would otherwise see every such request as plain "all".
            return self._whole_window_extent(start, end, fmt=fmt, resolution=cadence)
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        dates = date_windows(start_dt, end_dt, resolution)
        if len(dates) == 0:
            # A coarse cadence expands to nothing when the window contains no
            # period *anchor* — `"YS"` over 2024-02-01..2024-03-19 has no
            # January 1st, so `date_range` is empty even though the request is
            # perfectly valid. Returning that empty axis would make a
            # download loop over `self.time.dates` silently do nothing, so the
            # window start stands in for the single period that covers it.
            import pandas as pd

            dates = pd.DatetimeIndex([start_dt])
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=resolution,
            dates=dates,
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

    def _create_grid(self, lat_lim: list[float], lon_lim: list[float]) -> SpatialExtent:
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
    def download(self, progress_bar: bool = True) -> Any:
        """Download every requested variable and return the produced artifacts.

        Declares exactly the parameter every backend shares. A subclass may add
        further **optional** arguments — that stays substitutable, so mypy checks
        the overrides rather than waving them through. Deliberately *not*
        `**kwargs`: putting that here would oblige all 48 overrides to accept
        arbitrary keywords, which is the opposite of a contract.

        Capability-gated arguments appear only where they are honoured
        (`aggregate=` on the backends declaring :attr:`SUPPORTS_AGGREGATE`,
        `errors=` where the batch is a loop over independent items, `force=`
        where a re-run can skip completed artefacts), alongside genuinely
        backend-specific ones (`cores=` on chc, `tailor=` on eumetsat). Read the
        backend's own signature for those.

        Args:
            progress_bar: Whether to show this backend's progress bar. The one
                universal parameter, which is why it is declared here: this
                method used to be `download(self)` while all 48 overrides took
                two to five arguments, so nothing — not mypy, not a test — could
                catch a signature drifting.

        Returns:
            Any: The produced artifacts, shaped by :attr:`OUTPUT_KIND` —
                `"raster"` / `"mixed"` file-writing backends return the written
                paths (`list[Path]`); `"vector"` backends return an in-memory
                `FeatureCollection` (radar returns a `GeoDataFrame`);
                `"tabular"` backends return a `pandas.DataFrame`. Every backend
                returns its artifacts; the file-writing ones also leave them on
                disk under :attr:`root_dir`.

        Partial-failure policy across a multi-item batch defaults to
        **skip-and-continue** — a failed `(dataset, variable)` / chunk /
        sensor is logged and the batch proceeds, with a summary at the end
        — while single-shot backends propagate the error.

        The backends whose batch is a genuine loop over independent items
        (`chc`, `cmems`, `ecmwf`, `fdsn`, `nwp`, `radar`, `soilgrids`) make
        that policy caller-controllable with an explicit
        `errors="warn" | "raise" | "ignore"` argument, routed through
        :meth:`check_errors_policy` and :meth:`_run_items`. A backend whose
        `download` does not take `errors=` has nothing to apply it to — it
        issues one request, or its loop needs per-failure recovery the
        shared helper cannot express (chc re-opens its FTP session between
        failed dates). So **check the backend's own `download` signature**
        rather than assuming; :meth:`_search_fetch_each` also takes
        `errors=` for backends composed from it.
        """

    def _api(self, *args: Any, **kwargs: Any) -> Any:
        """Send / receive the request(s) this download needs.

        Called by :meth:`download`. The default
        is the search→fetch composition, :meth:`_api_via_search_fetch`, which
        is what a backend built on the :meth:`_search` / :meth:`_fetch` split
        wants — the great majority. Override it only for a backend that talks
        to its provider in one indivisible step and has no listable product
        set (chc composes an FTP path per date; ecmwf queues a CDS job; gee
        builds an ee chain), or one whose `_fetch` takes no product list.

        Returns:
            Whatever :meth:`_fetch` returned — see :meth:`_fetch` for the
            element type, which tracks :attr:`OUTPUT_KIND`.

            Typed `Any` rather than `list[Any]` deliberately: the overrides do
            not all return lists. chc returns a per-date mapping and gee a
            `Path | str | TaskInfo` depending on `export_via`, so narrowing the
            base annotation makes those overrides incompatible. The cost is that
            a `download()` forwarding `_api()` out of a `-> list[Path]` signature
            needs a `cast`.

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

    #: The partial-failure policies :meth:`_run_items` accepts. `"skip"` is a
    #: deprecated alias for `"ignore"`, kept because the nwp backend shipped it
    #: before the convention settled on the three names documented on
    #: :meth:`download`.
    ERROR_POLICIES: frozenset[str] = frozenset({"raise", "warn", "ignore", "skip"})

    #: The policy :meth:`_run_items` applies when a backend's own `download`
    #: was not given one. Declared here rather than per backend so a loop can
    #: read `self._errors` unconditionally; a `download(errors=...)` overrides
    #: it by assigning the :meth:`check_errors_policy` result.
    _errors: str = "warn"

    #: The total-row cap a backend's `download(limit=...)` recorded, read by
    #: whichever method assembles the result (often `_fetch_all`, not `download`
    #: itself). `None` means no cap. Declared here so an adopting backend does
    #: not have to initialise it in `__init__`.
    _limit: int | None = None

    #: Slot for a backend's lazily-built `HttpClient`. Declared here so the
    #: backends that hold one (rather than rebuilding it per item, which would
    #: discard the pooled connection) can check `if self._http is None` without
    #: each re-declaring the attribute. `None` until first use.
    _http: HttpClient | None = None

    @staticmethod
    def check_limit(limit: int | None) -> int | None:
        """Validate a total-row cap.

        Args:
            limit: The maximum number of rows / features the caller wants in
                total, or `None` for no cap.

        Returns:
            int | None: `limit` unchanged, once known to be usable.

        Raises:
            TypeError: If `limit` is neither `None` nor an `int` (a `bool` is
                rejected too — `limit=True` is a mistake, not a cap of 1).
            ValueError: If `limit` is zero or negative. A request for no rows
                is a caller bug, not a cheap no-op to serve.

        Examples:
            - A positive cap and `None` both pass through:
                ```python
                >>> from earthlens.base import AbstractDataSource
                >>> AbstractDataSource.check_limit(500)
                500
                >>> AbstractDataSource.check_limit(None) is None
                True

                ```
            - Zero is refused rather than silently returning nothing:
                ```python
                >>> from earthlens.base import AbstractDataSource
                >>> try:
                ...     AbstractDataSource.check_limit(0)
                ... except ValueError as exc:
                ...     print("rejected")
                rejected

                ```
        """
        if limit is None:
            return None
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(
                f"limit must be an int or None, got {type(limit).__name__}: {limit!r}."
            )
        if limit < 1:
            raise ValueError(
                f"limit must be at least 1, got {limit}. Pass None for no cap."
            )
        return limit

    def _take_limited(
        self,
        chunks: Iterable[Any],
        *,
        limit: int | None,
        size: Callable[[Any], int] | None = None,
        head: Callable[[Any, int], Any] | None = None,
    ) -> list[Any]:
        """Consume `chunks` until `limit` rows have been collected.

        The bounded counterpart to "append every fragment, concatenate at the
        end". `chunks` is consumed lazily, so a backend whose per-item fetch is
        a generator stops issuing requests once the cap is met instead of
        pulling the whole result set and truncating afterwards — which is what
        makes this a cap on *memory*, not just on the returned value.

        The last fragment is trimmed so the total is exactly `limit`, which is
        why a page-size argument is not a substitute: pages land in
        page-size multiples, this does not.

        Args:
            chunks: The per-item fragments — `DataFrame`s,
                `FeatureCollection`s, lists of paths. Consumed lazily.
            limit: Total rows to keep, or `None` to collect everything.
            size: Row count of one fragment. Defaults to `len`.
            head: `(fragment, n) -> fragment` keeping the first `n` rows.
                Defaults to slicing (`fragment[:n]`), which covers lists and
                anything else sliceable; pass one for a type that is not.

        Returns:
            list[Any]: The collected fragments, the last one trimmed when it
                straddled the cap.

        Examples:
            - The cap is exact even when it falls inside a fragment, and the
              fragments past it are never consumed:
                ```python
                >>> from earthlens.base import AbstractDataSource
                >>> pulled = []
                >>> def pages():
                ...     for page in ([1, 2, 3], [4, 5, 6], [7, 8, 9]):
                ...         pulled.append(page[0])
                ...         yield page
                >>> class Demo(AbstractDataSource):
                ...     def _initialize(self): pass
                ...     def _create_grid(self): pass
                ...     def _check_input_dates(self): pass
                ...     def download(self): pass
                >>> Demo._take_limited(Demo, pages(), limit=4)
                [[1, 2, 3], [4]]
                >>> pulled
                [1, 4]

                ```
        """
        if limit is None:
            return list(chunks)
        measure = size or len
        take = head or _head_rows
        collected: list[Any] = []
        remaining = limit
        iterator = iter(chunks)
        try:
            for chunk in iterator:
                length = measure(chunk)
                # `>=`, not `>`: a chunk that exactly fills the cap must also end
                # the loop here. Deciding on the *next* iteration would pull one
                # more fragment first — the very work the cap exists to avoid.
                if length >= remaining:
                    collected.append(
                        take(chunk, remaining) if length > remaining else chunk
                    )
                    return collected
                collected.append(chunk)
                remaining -= length
        finally:
            # Stopping early abandons the generator mid-`for`, which leaves any
            # `with` block it is suspended inside — a temp directory holding a
            # bulk download, an open session — unwound only whenever the object
            # is collected. Closing it here makes that deterministic, which is
            # the difference between a temp dir removed now and one removed at
            # interpreter exit.
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        return collected

    def iter_download(self, *, limit: int | None = None) -> Iterator[Any]:
        """Yield the download's artifacts one item at a time.

        The streaming counterpart to :meth:`download`, for callers who want to
        consume a large vector / tabular result without the whole thing being
        resident: each `_search` product's fragment is yielded as it arrives
        and can be dropped before the next is fetched. `download()` remains the
        batch form and is unaffected.

        The default implementation composes the `_search` / :meth:`_fetch_one`
        split, so any backend with that split gets it for free. A backend whose
        fetch is inherently whole-batch (one server-side request for
        everything) does not override this and raises below, rather than
        pretending to stream.

        Args:
            limit: Total rows / features to yield across every product, or
                `None` for no cap. The fragment that straddles the cap is
                trimmed so the total is exact, and the products past it are
                never fetched.

        Yields:
            Any: One fragment per product — the same element type
                :meth:`_fetch` returns for this backend's
                :attr:`OUTPUT_KIND`.

        Raises:
            NotImplementedError: When the backend implements neither the
                `_search` / `_fetch_one` split nor its own `iter_download`.
            TypeError: If `limit` is neither `None` nor an `int`.
            ValueError: If `limit` is less than 1.
        """
        if type(self)._fetch_one is AbstractDataSource._fetch_one:
            raise NotImplementedError(
                f"{type(self).__name__} cannot stream: it has no per-product "
                "_fetch_one, so there is nothing to yield incrementally. Use "
                "download() instead."
            )
        remaining = self.check_limit(limit)
        for product in self._search():
            fragment = self._fetch_one(product)
            if remaining is None:
                yield fragment
                continue
            length = len(fragment)
            if length >= remaining:
                # Skip the slice when the fragment fills the cap exactly, as
                # `_take_limited` does: `_head_rows` would copy every row to
                # produce the fragment it was handed.
                yield (
                    fragment if length == remaining else _head_rows(fragment, remaining)
                )
                return
            yield fragment
            remaining -= length

    @staticmethod
    def check_errors_policy(errors: str) -> str:
        """Validate an `errors=` argument, normalising the `"skip"` alias.

        Args:
            errors: The requested policy.

        Returns:
            The canonical policy — `"raise"`, `"warn"` or `"ignore"`.

        Raises:
            ValueError: If `errors` is not a recognised policy.

        Examples:
            - The canonical names pass through, and `"skip"` normalises:
                ```python
                >>> from earthlens.base import AbstractDataSource
                >>> AbstractDataSource.check_errors_policy("warn")
                'warn'
                >>> AbstractDataSource.check_errors_policy("skip")
                'ignore'

                ```
            - Anything else is rejected with the accepted set:
                ```python
                >>> from earthlens.base import AbstractDataSource
                >>> AbstractDataSource.check_errors_policy("continue")
                Traceback (most recent call last):
                    ...
                ValueError: errors must be 'raise', 'warn' or 'ignore'; got 'continue'.

                ```
        """
        if errors not in AbstractDataSource.ERROR_POLICIES:
            raise ValueError(
                f"errors must be 'raise', 'warn' or 'ignore'; got {errors!r}."
            )
        return "ignore" if errors == "skip" else errors

    def _run_items(
        self,
        items: Sequence[Any],
        fn: Callable[[Any], Any],
        *,
        errors: str = "warn",
        label: str = "item",
        describe: Callable[[Any], str] | None = None,
        on_failure: Callable[[Any, BaseException], Any] | None = None,
        fatal: tuple[type[Exception], ...] = (),
    ) -> tuple[list[Any], list[tuple[str, BaseException]]]:
        """Map `fn` over `items`, applying the caller's partial-failure policy.

        The shared form of the skip-and-continue loop the multi-item backends
        each hand-rolled, and the reason `errors=` was previously advertised on
        :meth:`download` but honoured by exactly one backend: without somewhere
        to put the policy, every loop hard-coded "log it and carry on", so a
        caller could not ask for a batch to fail fast.

        Args:
            items: The work items — products, dates, `(dataset, variable)` pairs.
            fn: Called once per item; its return value is collected.
            errors: `"raise"` propagates the first failure, `"warn"` logs each
                one and continues, `"ignore"` continues silently. `"skip"` is
                accepted as a deprecated alias for `"ignore"`.
            label: Noun for the log lines (e.g. `"granule"`, `"variable"`).
            describe: Renders an item for the log; defaults to `str`.
            on_failure: Optional `(item, exception) -> placeholder`. When given,
                a failed item contributes its placeholder to `results`, so the
                results stay positionally aligned with `items` — the shape the
                vector backends need, where a failed provider still occupies a
                slot with an empty `FeatureCollection`. When omitted, failures
                are simply absent from `results`.
            fatal: Exception classes that always propagate, whatever `errors`
                says — for a failure of the *service* rather than of one item,
                where continuing would report an upstream outage as a set of
                empty results.

        Returns:
            `(results, failures)` — one result per succeeding item, in order,
            and `(description, exception)` for each failure. The caller decides
            what an all-failed batch means, since that differs by backend.

        Raises:
            ValueError: If `errors` is not a recognised policy.
            BaseException: The first item's exception when `errors="raise"`,
                or any exception matching `fatal` under **every** policy.
        """
        policy = self.check_errors_policy(errors)
        failures: list[tuple[str, BaseException]] = []
        results = list(
            self._iter_items(
                items,
                fn,
                errors=policy,
                label=label,
                describe=describe,
                on_failure=on_failure,
                failures=failures,
                fatal=fatal,
            )
        )
        if failures and policy == "warn":
            logger.warning(
                f"{type(self).__name__}: {len(failures)} of {len(items)} "
                f"{label}(s) failed; {len(items) - len(failures)} succeeded."
            )
        return results, failures

    def _fragment_rows(self, fragment: Any) -> int:
        """Row count of one fetched fragment, for the shared composition's cap.

        `len` is right for the row-bearing fragments (`DataFrame`,
        `FeatureCollection`) the tabular and vector backends yield. A raster
        backend's `_fetch_one` yields a single `Path`, which has no length —
        soilgrids is the one built on this composition. That combination only
        arises if such a backend gains a `limit=`, and the bare `len()` failure
        is a `TypeError: object of type 'WindowsPath' has no len()` naming
        neither the backend nor the cap.

        Args:
            fragment: One `_fetch_one` result.

        Returns:
            int: The fragment's row count.

        Raises:
            TypeError: When the fragment has no length, naming the backend and
                what to do about it.
        """
        try:
            return len(fragment)
        except TypeError as exc:
            raise TypeError(
                f"{type(self).__name__} cannot apply a row cap: its fetch "
                f"returns {type(fragment).__name__}, which has no length "
                f"(OUTPUT_KIND={self.OUTPUT_KIND!r}). A `limit=` counts rows, "
                f"so it does not describe a backend that writes files — narrow "
                f"the request instead, or pass `size=` if a per-item cap is "
                f"what you mean."
            ) from exc

    def _iter_items(
        self,
        items: Iterable[Any],
        fn: Callable[[Any], Any],
        *,
        errors: str | None,
        label: str,
        describe: Callable[[Any], str] | None,
        on_failure: Callable[[Any, BaseException], Any] | None,
        failures: list[tuple[str, BaseException]],
        fatal: tuple[type[Exception], ...] = (),
    ) -> Iterator[Any]:
        """Apply `fn` to each item under the failure policy, yielding as it goes.

        The lazy form of :meth:`_run_items`, and the single implementation of
        the policy: `_run_items` is `list()` of this plus a summary line. Being
        a generator is what lets a bounded caller stop early — under a policy a
        cap cannot be turned into a slice of `items`, because failures consume
        items without producing rows, so the decision to stop can only be made
        after each result arrives.

        Args:
            items: The items to process; consumed lazily.
            fn: Called once per item; its return value is yielded.
            errors: An already-validated policy (`"raise"` / `"warn"` /
                `"ignore"`), or `None` for `"raise"`.
            label: Noun for the log lines (e.g. `"granule"`, `"variable"`).
            describe: Renders an item for the log; defaults to `str`.
            on_failure: Optional `(item, exception) -> placeholder`, yielded in
                place of the failed item's result.
            failures: Accumulator the caller owns; each failure is appended as
                `(description, exception)` so a caller that stops early still
                sees what failed before it stopped.
            fatal: Exception classes that always propagate, whatever the policy
                — a service-level failure (the upstream refused to serve *any*
                request) is not the per-item data gap `errors="warn"` exists to
                absorb, and silently returning fewer items would report it as
                "this item has no data".

        Yields:
            Any: Each successful `fn(item)` result, plus any `on_failure`
                placeholders, in item order.

        Raises:
            BaseException: The first item's exception when the policy is
                `"raise"`, or any exception matching `fatal` under **every**
                policy.
        """
        name = describe or str
        for item in items:
            # `fn` is called outside the `yield` on purpose: yielding inside the
            # `try` would put the handler in the path of whatever the *consumer*
            # raises while the generator is suspended, so a caller's own error
            # would be logged as this item's failure and swallowed by an
            # `ignore` policy.
            try:
                value = fn(item)
            except Exception as exc:  # noqa: BLE001 - policy decides the fate
                if errors is None or errors == "raise" or isinstance(exc, fatal):
                    raise
                placeholder = self._record_failure(
                    item,
                    exc,
                    errors=errors,
                    label=label,
                    name=name,
                    on_failure=on_failure,
                    failures=failures,
                )
                if placeholder is not _MISSING:
                    yield placeholder
                continue
            yield value

    def _record_failure(
        self,
        item: Any,
        exc: BaseException,
        *,
        errors: str,
        label: str,
        name: Callable[[Any], str],
        on_failure: Callable[[Any, BaseException], Any] | None,
        failures: list[tuple[str, BaseException]],
    ) -> Any:
        """Log and record one item's failure under a non-raising policy.

        Split out of :meth:`_iter_items` so the generator stays a plain
        try/except around the item call.

        Args:
            item: The item whose `fn` call raised.
            exc: What it raised.
            errors: The already-validated policy (`"warn"` or `"ignore"`).
            label: Noun for the log line (e.g. `"granule"`).
            name: Renders `item` for the log.
            on_failure: Optional `(item, exception) -> placeholder`.
            failures: Accumulator the caller owns; appended to here.

        Returns:
            Any: The placeholder to yield in the failed item's place, or the
                `_MISSING` sentinel when there is none. A hook returning
                `None` is a real placeholder, which is why a sentinel and not
                `None` marks its absence.
        """
        described = name(item)
        placeholder = _MISSING if on_failure is None else on_failure(item, exc)
        if errors == "warn":
            logger.warning(
                f"{type(self).__name__}: {label} {described} failed: "
                f"{type(exc).__name__}: {exc}"
            )
        failures.append((described, exc))
        return placeholder

    def _fetch_limited(
        self, products: Sequence[RemoteProduct], limit: int | None = None
    ) -> list[Any]:
        """Fetch each product, stopping once `limit` rows have been collected.

        The bounded form of the `[self._fetch_one(p) for p in products]` that
        several backends write as their `_fetch`. The comprehension fetches
        everything and any cap applied afterwards only truncates the result; this
        consumes lazily, so a product past the cap is never requested.

        Args:
            products: The products from :meth:`_search`.
            limit: Total rows to collect, or `None` for all of them. Usually
                :attr:`_limit`, recorded by the backend's `download(limit=...)`.

        Returns:
            list[Any]: One fragment per fetched product, the last trimmed when it
                straddled the cap.
        """
        return self._take_limited(
            (self._fetch_one(product) for product in products), limit=limit
        )

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
        errors: str | None = None,
        label: str = "product",
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
            errors: The partial-failure policy to apply across the
                products, normally `self._errors` from a backend whose
                `download` accepts `errors=`. `None` — the default —
                propagates the first failure, which is what a caller that
                never opted into a policy already expects.
            label: Noun for the :meth:`_run_items` log lines when a policy
                is in force.

        Returns:
            list[Any]: One :meth:`_fetch_one` result per product
                (element type tracks :attr:`OUTPUT_KIND`), or `[]` when
                `_search` matched nothing. With a policy in force, failed
                products are absent rather than aborting the batch.

        Raises:
            ValueError: If `errors` is not a recognised policy.
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
        # Closed explicitly: a cap that stops mid-sweep leaves the bar
        # unfinished, and tqdm only restores the terminal (and stops redrawing)
        # when it is closed. `_take_limited` closes the *generator* it abandons,
        # which is `_iter_items` — the bar underneath it is a separate object.
        # Lazy in both branches so `self._limit` stops the fetching rather than
        # trimming the assembled list. Under a policy the cap cannot be turned
        # into a slice of `products` up front: a failed product consumes an item
        # without contributing rows, so only the results can be counted.
        policy = self.check_errors_policy(errors) if errors is not None else None
        failures: list[tuple[str, BaseException]] = []
        # `finally`, so the bar is closed on the failure path too: a
        # `_fetch_one` that raises under the `raise` policy propagates straight
        # out of here, and an unclosed tqdm keeps redrawing over whatever the
        # caller prints next.
        try:
            results = self._take_limited(
                self._iter_items(
                    iterator,
                    self._fetch_one,
                    errors=policy,
                    label=label,
                    describe=_describe_remote_product,
                    on_failure=None,
                    failures=failures,
                ),
                limit=self._limit,
                size=self._fragment_rows,
            )
        finally:
            iterator.close()
        if failures and policy == "warn":
            # Counted against the products actually attempted, not the whole
            # planned list: a cap can end the sweep early, and "3 of 400 failed"
            # reads as a 0.75% failure rate when in truth 3 of the 5 products
            # that ran failed.
            attempted = len(failures) + len(results)
            logger.warning(
                f"{type(self).__name__}: {len(failures)} of {attempted} "
                f"{label}(s) attempted failed; {len(results)} succeeded."
            )
        return results


#: The row type a concrete catalog holds. Providers parameterise
#: :class:`AbstractCatalog` with their own pydantic row model
#: (`AbstractCatalog[Dataset]`), so `datasets`, `get_catalog` and
#: `get_dataset` keep that type instead of degrading to `Any`. Left
#: unparameterised the catalog still works; it is simply untyped in the rows.
RowT = TypeVar("RowT")


class AbstractCatalog(BaseModel, Generic[RowT]):
    """Abstract base class for per-data-source variable catalogs.

    Subclasses load a backend-specific catalog (a YAML file, an
    in-code dict, or a remote query) in :meth:`get_catalog` and
    expose individual entries via :meth:`get_variable`. The
    :func:`model_post_init` hook eagerly populates :attr:`catalog`
    after pydantic validation runs, so subclasses can treat the
    catalog as a mapping thereafter without writing their own
    `__init__`.

    Subclasses pass through pydantic's normal `BaseModel.__init__`
    — declare any backend-specific construction parameters as
    pydantic fields rather than `__init__` arguments.

    :meth:`get_catalog` defaults to returning :attr:`datasets`, which is
    where every backend keeps its rows and what the dict surface below
    already reads, so a subclass storing them there needs no override.
    **A subclass whose rows live in another field must override it** —
    otherwise `get_catalog()` and the :attr:`catalog` property report an
    empty mapping rather than failing, and a catalog that silently has no
    entries is harder to notice than one that raises. :meth:`get_variable`
    still raises :class:`NotImplementedError`, since there is no sensible
    default for a per-dataset variable level.

    Attributes:
        catalog: Read-only view of the mapping returned by
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

    available_datasets: list[str] = Field(default_factory=list)
    datasets: dict[str, RowT] = Field(default_factory=dict)
    providers: dict[str, Any] = Field(default_factory=dict)

    @property
    def catalog(self) -> Mapping[str, Any]:
        """Read-only view of the catalog mapping (see :meth:`get_catalog`).

        For nearly every backend `get_catalog()` returns :attr:`datasets`
        itself, so `catalog` used to be a second name bound to the very same
        `dict` — assigning through one silently rewrote the other, and a
        caller who mutated `cat.catalog` corrupted the shared parse cache
        the loader hands out. Exposing a `MappingProxyType` keeps every read
        working (`cat.catalog["key"]`, `in`, `len`, iteration) while making
        that accidental write fail loudly.

        Returns:
            Mapping[str, Any]: A read-only view over `get_catalog()`.

        Examples:
            - Reads behave like the mapping; writes are refused rather than
              silently rewriting `datasets`:
                ```python
                >>> from types import MappingProxyType
                >>> view = MappingProxyType({"EQ": "Earthquake"})
                >>> view["EQ"]
                'Earthquake'
                >>> view["EQ"] = None
                Traceback (most recent call last):
                    ...
                TypeError: 'mappingproxy' object does not support item assignment

                ```
        """
        return MappingProxyType(self.get_catalog())

    @classmethod
    def _autoload(cls) -> Mapping[str, Any]:
        """Return the payload to fill an empty catalog from disk.

        The one part of post-init that genuinely differs per backend: *how* the
        rows are read. Everything around it — only read when no rows were
        supplied, never clobber what the caller passed — is the same everywhere
        and lives in :meth:`model_post_init`.

        Returns:
            Mapping[str, Any]: Field name to value, e.g.
                `{"datasets": ..., "available_datasets": ...}`. The default is
                empty, meaning this catalog does not auto-load.
        """
        return {}

    def model_post_init(self, __context: Any) -> None:
        """Fill an empty catalog from disk, then run the subclass's wiring.

        `Catalog()` with no arguments reads from disk; passing `datasets=...`
        skips the read, which is what lets a test build a catalog from literals.
        A field the caller already supplied is never overwritten, so a partial
        construction (`datasets=` but no `available_datasets=`) still gets the
        rest filled in.

        This used to be written out in all 48 provider catalogs. The bodies
        differed only in the loader call, and the surrounding rule had drifted —
        some defaulted `available_datasets`, some did not — which is the kind of
        difference nobody notices until two catalogs disagree.

        Args:
            __context: Opaque context handed in by the pydantic v2 lifecycle.
                Unused — this hook only fills empty fields — but named
                positionally because pydantic calls it that way.
        """
        if not self.datasets:
            for field, value in self._autoload().items():
                if not getattr(self, field, None):
                    setattr(self, field, value)

    def get_catalog(self) -> dict[str, RowT]:
        """Return the catalog's rows.

        Defaults to :attr:`datasets`, which is where every backend keeps
        them and what the inherited dict surface (`len`, `in`, `[]`,
        iteration, :meth:`get_dataset`) already reads. Raising
        `NotImplementedError` here instead made the method mandatory, and
        all 61 provider catalogs answered it with the same
        `return self.datasets`.

        A backend whose catalog is genuinely a different shape still
        overrides this.

        Returns:
            dict[str, RowT]: The `{key: row}` mapping backing this catalog,
            typed as the row model the subclass parameterised the base with.

        Examples:
            - Read the rows and inspect one:
                ```python
                >>> from earthlens.chc import Catalog
                >>> rows = Catalog().get_catalog()
                >>> "africa-daily" in rows
                True
                >>> sorted(rows["africa-daily"].variables)
                ['precipitation']

                ```
            - It is the same mapping the dict surface reads, so `len` and
              iteration agree with it:
                ```python
                >>> from earthlens.chc import Catalog
                >>> catalog = Catalog()
                >>> len(catalog.get_catalog()) == len(catalog)
                True
                >>> sorted(catalog.get_catalog())[:2]
                ['africa-2-monthly', 'africa-3-monthly']

                ```
        """
        if not self.datasets:
            # The default cannot tell "no rows" from "rows kept elsewhere", so
            # it answers an empty mapping for both. Every in-repo catalog is
            # covered by a contract test; an out-of-tree subclass gets this
            # instead of silence.
            logger.warning(
                f"{type(self).__name__}.get_catalog() is empty. If this "
                f"catalog keeps its rows somewhere other than `datasets`, "
                f"override get_catalog()."
            )
        return self.datasets

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

    def get_dataset(self, name: str) -> RowT:
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

    def __getitem__(self, name: str) -> RowT:
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

        # Annotated: the branches store a dumped dict and a raw row, so the
        # inferred type would come from whichever ran first.
        body: dict[str, Any] = {}
        for key, dataset in self.datasets.items():
            if isinstance(dataset, BaseModel):
                body[key] = dataset.model_dump(exclude_none=True)
            else:
                body[key] = dataset
        dumped = yaml.safe_dump(
            body, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
        return cast(str, dumped)
