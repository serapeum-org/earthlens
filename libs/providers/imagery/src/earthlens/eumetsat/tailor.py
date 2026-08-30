"""Typed request shape for the EUMETSAT Data Tailor (server-side customisation).

Hosts `TailorConfig`, the frozen pydantic value object that turns a
`download(tailor=...)` call into a EUMETSAT Data Tailor `Chain`. Data
Tailor is EUMETSAT's server-side subset / reproject / reformat service
(the analogue of NASA Harmony) — a **spatial** operation, distinct from
the temporal `earthlens.aggregate.AggregationConfig`. The two knobs
compose: `tailor=` reshapes each product server-side, then an optional
`aggregate=` reduces the result client-side.

`TailorConfig` is deliberately SDK-free — it holds only plain values and
knows how to derive the Data Tailor region-of-interest (NSWE) from its
`bbox`. The backend builds the actual `eumdac.tailor_models.Chain` (a
lazy `eumdac` import) from a `TailorConfig`, the resolved catalog row's
`tailor_product_type`, and the request's spatial extent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

#: Default Data Tailor output format (pinned from the live service in `A1`).
DEFAULT_FORMAT = "geotiff"
#: Default Data Tailor projection / CRS (pinned from the live service in `A1`).
DEFAULT_CRS = "geographic"
#: Data Tailor output formats that carry their own fixed grid and reject any
#: projection (from the `/epcs/formats` list, `A1`). `crs` must be `None` for these.
NATIVE_FORMATS = frozenset({"msgnative", "epsnative", "hrit", "hrit_compressed"})


class TailorConfig(BaseModel):
    """Server-side customisation request for the EUMETSAT Data Tailor.

    A frozen value object passed to `EUMETSAT.download(tailor=...)` (or the
    `EarthLens(..., tailor=...)` facade kwarg) to route a request through
    Data Tailor instead of the native whole-product fetch. It carries the
    output `format`, the target `crs` / projection, an optional `bbox`
    crop, an optional band `filter`, and a `quicklook` flag. The Data
    Tailor **product-type** is not set here — it comes from the resolved
    catalog row's `tailor_product_type` (`G4` / `G5`).

    Attributes:
        format: Data Tailor output format, e.g. `"geotiff"`, `"netcdf4"`.
            Maps to `Chain.format`. Defaults to `"geotiff"`.
        crs: Target projection / CRS, e.g. `"geographic"`. Maps to
            `Chain.projection`. Defaults to `"geographic"`. Pass `None`
            to reproject nothing, which omits `projection` from the
            chain entirely — required by the native output formats
            (`"msgnative"`, `"epsnative"`, `"hrit"`,
            `"hrit_compressed"`), since re-gridding the pixels would
            stop the result being native. An empty string is still
            rejected: `None` is explicit, `""` is a mistake.
        bbox: Optional crop as `(west, south, east, north)` in degrees
            (the GeoJSON / OGC bbox order). When `None`, the backend falls
            back to the request's own spatial extent (`lat_lim` /
            `lon_lim`). Maps to the Data Tailor ROI as an `NSWE` list.
        filter: Optional list of band / layer names to keep. Maps to
            `Chain.filter` (a `Filter(bands=...)`). `None` keeps every
            band.
        quicklook: When `True`, request a quicklook rendering alongside
            the customised data. Defaults to `False`.

    Examples:
        - A reproject + crop + reformat request:
            ```python
            >>> from earthlens.eumetsat import TailorConfig
            >>> cfg = TailorConfig(format="geotiff", crs="geographic", bbox=(4, 48, 8, 52))
            >>> cfg.nswe
            [52.0, 48.0, 4.0, 8.0]

            ```
        - No bbox falls back to the request extent (ROI derived later):
            ```python
            >>> from earthlens.eumetsat import TailorConfig
            >>> TailorConfig().nswe is None
            True

            ```
        - A native-format subset, which must not be reprojected — `crs=None`
          keeps `projection` off the chain, while the default still reprojects:
            ```python
            >>> from earthlens.eumetsat import TailorConfig
            >>> cfg = TailorConfig(format="msgnative", crs=None)
            >>> cfg.format
            'msgnative'
            >>> print(cfg.crs)
            None
            >>> TailorConfig().crs
            'geographic'

            ```
        - A blank `crs` is a mistake rather than a request for no
          reprojection, so it is rejected:
            ```python
            >>> from pydantic import ValidationError
            >>> from earthlens.eumetsat import TailorConfig
            >>> try:
            ...     TailorConfig(crs="  ")
            ... except ValidationError as err:
            ...     print(err.errors()[0]["msg"])
            Value error, must be a non-empty string

            ```

    See Also:
        earthlens.eumetsat.backend.EUMETSAT.download: Consumes this via its
            `tailor=` argument and builds the Data Tailor chain from it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: str = DEFAULT_FORMAT
    crs: str | None = DEFAULT_CRS
    bbox: tuple[float, float, float, float] | None = None
    filter: list[str] | None = None
    quicklook: bool = False

    @field_validator("format", "crs")
    @classmethod
    def _non_empty(cls, value: str | None) -> str | None:
        """Reject an empty `format` / `crs` string.

        A `None` `crs` passes through untouched — it is the explicit way
        to ask for no reprojection. Only `format` is typed `str`, so
        `None` can reach here for `crs` alone.

        Args:
            value: The candidate `format` or `crs` value.

        Returns:
            The stripped value, or `None` when `crs` is unset.

        Raises:
            ValueError: When the value is blank.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @model_validator(mode="after")
    def _native_format_forbids_projection(self) -> TailorConfig:
        """Reject a native output format paired with a non-`None` `crs`.

        A native format (`NATIVE_FORMATS`) carries its own fixed grid and the
        Data Tailor service rejects any projection on it. Catching the
        mismatch here, instead of leaving it to the service, avoids
        submitting a customisation that can only fail after a poll round
        trip that has been measured to take upward of 30 minutes.

        Returns:
            TailorConfig: `self`, unchanged, when the combination is valid.

        Raises:
            ValueError: When `format` is native and `crs` is not `None`.
        """
        if self.format in NATIVE_FORMATS and self.crs is not None:
            raise ValueError(
                f"format={self.format!r} is a native output format and cannot be "
                f"reprojected; pass crs=None instead of crs={self.crs!r}"
            )
        return self

    @field_validator("bbox")
    @classmethod
    def _valid_bbox(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        """Validate a `bbox` is a well-ordered, in-range `(w, s, e, n)`.

        Args:
            value: The candidate `(west, south, east, north)` box, or
                `None`.

        Returns:
            The validated box, or `None`.

        Raises:
            ValueError: When the box is inverted (`west > east` or
                `south > north`) or out of the WGS84 range.
        """
        if value is None:
            return None
        west, south, east, north = value
        if west > east:
            raise ValueError(f"bbox west ({west}) must be <= east ({east})")
        if south > north:
            raise ValueError(f"bbox south ({south}) must be <= north ({north})")
        if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
            raise ValueError(f"bbox longitudes must be in [-180, 180]: {value}")
        if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
            raise ValueError(f"bbox latitudes must be in [-90, 90]: {value}")
        return value

    @property
    def nswe(self) -> list[float] | None:
        """Return the Data Tailor ROI as an `[N, S, W, E]` list, or `None`.

        Data Tailor's `RegionOfInterest` takes an `NSWE` list of four
        numbers `[north, south, west, east]` (`A1`). This converts the
        stored `bbox` (`(west, south, east, north)`) into that order.
        Returns `None` when no `bbox` was set, so the backend can fall
        back to the request's spatial extent.

        Returns:
            list[float] | None: `[north, south, west, east]`, or `None`
                when `bbox` is unset.

        Examples:
            - A bbox is reordered from `(w, s, e, n)` into `[N, S, W, E]`:
                ```python
                >>> from earthlens.eumetsat import TailorConfig
                >>> roi = TailorConfig(bbox=(4, 48, 8, 52)).nswe
                >>> roi
                [52.0, 48.0, 4.0, 8.0]
                >>> roi[0], roi[3]
                (52.0, 8.0)

                ```
            - Without a bbox there is no ROI, and the backend falls back to
              the request's own extent:
                ```python
                >>> from earthlens.eumetsat import TailorConfig
                >>> print(TailorConfig().nswe)
                None

                ```
            - A degenerate bbox — a single point — still round-trips:
                ```python
                >>> from earthlens.eumetsat import TailorConfig
                >>> TailorConfig(bbox=(5.0, 50.0, 5.0, 50.0)).nswe
                [50.0, 50.0, 5.0, 5.0]

                ```

        See Also:
            TailorConfig.nswe_from_extent: Builds the same list from the
                request's spatial-extent bounds when no `bbox` is set.
        """
        if self.bbox is None:
            return None
        west, south, east, north = self.bbox
        return [north, south, west, east]

    @staticmethod
    def nswe_from_extent(
        north: float, south: float, west: float, east: float
    ) -> list[float]:
        """Build a Data Tailor `NSWE` list from spatial-extent bounds.

        The backend calls this with `self.space` bounds when a
        `TailorConfig` carries no explicit `bbox`, so the request's
        `lat_lim` / `lon_lim` become the ROI.

        Args:
            north: Northern latitude bound in degrees.
            south: Southern latitude bound in degrees.
            west: Western longitude bound in degrees.
            east: Eastern longitude bound in degrees.

        Returns:
            list[float]: `[north, south, west, east]`.

        Examples:
            - Bounds are passed through in `[N, S, W, E]` order:
                ```python
                >>> from earthlens.eumetsat import TailorConfig
                >>> TailorConfig.nswe_from_extent(52, 48, 4, 8)
                [52, 48, 4, 8]

                ```
            - It is callable on the class, so the backend does not need a
              config instance to build an ROI:
                ```python
                >>> from earthlens.eumetsat import TailorConfig
                >>> roi = TailorConfig.nswe_from_extent(
                ...     north=79.0, south=-79.0, west=-79.0, east=79.0
                ... )
                >>> roi[0] - roi[1]
                158.0

                ```

        See Also:
            TailorConfig.nswe: The same list derived from an explicit `bbox`.
        """
        return [north, south, west, east]
