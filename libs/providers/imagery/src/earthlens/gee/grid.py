"""Where a request reads, in what CRS, and on what pixel grid.

Nine members on `GEE` used to answer that one question, spread over 2,000 lines of
`backend.py` — `_bbox_to_output_crs`, `_eedai_output_grid`, `_eedai_grid`,
`_eedai_latlon_aoi`, `_eedai_window`, `_region_in_output_crs`, `_same_crs`,
`_eedai_native_fits` and a module-level `_same_epsg_code`. Every engine needs the
answer, and the two shipped engines size it **differently**, which is the real
reason this belongs in one place:

* Earth Engine reads `scale` in a geographic CRS as a uniform degree-equivalent.
* The EEDAI reader sizes for square metres on the ground, shortening longitude by
  `cos(latitude)`.

Away from the equator those disagree on the column count. Keeping both behind one
value object with two named constructors makes the disagreement explicit and
testable, rather than implicit in two method families that happen to differ.

`OutputGrid` is the value; the constructors are
`OutputGrid.for_metre_grid` (the EEDAI convention),
`OutputGrid.for_earth_engine` (the degree-equivalent one) and
`OutputGrid.from_crs_transform` (an explicitly pinned affine grid).

```python
>>> grid = OutputGrid.for_metre_grid((31.2, 29.9, 31.3, 30.0), crs="EPSG:4326", scale=90.0)
>>> grid.rows, grid.cols
(124, 108)
>>> grid.pixels
13392

```
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any, ClassVar

from pydantic import ConfigDict, Field

from earthlens.base.leaves import SummarisedLeaf

__all__ = [
    "OutputGrid",
    "NATIVE_CRS",
    "same_crs",
    "same_epsg_code",
    "bbox_to_crs",
    "bbox_to_latlon",
    "geographic_grid",
    "projected_grid",
    "validate_crs_transform",
]

#: The CRS scene discovery and every lat/lon AOI are expressed in.
NATIVE_CRS: str = "EPSG:4326"

#: Metres per degree of latitude, the constant the geographic grid converts with.
_METRES_PER_DEGREE: float = 111_320.0

#: Latitude beyond which `cos` is clamped, so a polar AOI cannot divide by zero.
_MAX_POLEWARD_DEGREES: float = 89.9

#: How close to a whole pixel counts as *on* the lattice. A window whose edge is
#: an exact multiple of the pixel size still divides to something like
#: `100.00000000000142` in binary floating point, and a bare `ceil` turns that
#: into a whole extra column - one that is 100 m wide at 0.001 degrees, and whose
#: appearance depends on how large the pinned origin happens to be.
_LATTICE_EPSILON: float = 1e-9


def _lattice_floor(offset: float) -> int:
    """Return the lattice index at or below `offset`, treating near-exact as exact.

    Args:
        offset: The window edge in pixels from the pinned origin.

    Returns:
        `floor(offset)`, or the nearest whole index when `offset` is within
        `_LATTICE_EPSILON` of one.
    """
    nearest = round(offset)
    if abs(offset - nearest) <= max(_LATTICE_EPSILON, abs(offset) * 1e-12):
        return int(nearest)
    return math.floor(offset)


def _lattice_ceil(offset: float) -> int:
    """Return the lattice index at or above `offset`, treating near-exact as exact.

    Args:
        offset: The window edge in pixels from the pinned origin.

    Returns:
        `ceil(offset)`, or the nearest whole index when `offset` is within
        `_LATTICE_EPSILON` of one.
    """
    nearest = round(offset)
    if abs(offset - nearest) <= max(_LATTICE_EPSILON, abs(offset) * 1e-12):
        return int(nearest)
    return math.ceil(offset)


def validate_crs_transform(crs_transform: object) -> tuple[float, ...] | None:
    """Normalise an explicit grid transform, or reject it.

    One definition, called both by `GEE(crs_transform=...)` - so a bad transform
    is refused at construction, before the catalog parse and any request - and by
    `OutputGrid.from_crs_transform`, which cannot snap a grid it has not
    checked.

    Args:
        crs_transform: The candidate transform
            `(x_scale, x_shear, x_origin, y_shear, y_scale, y_origin)`, in the
            units of the output CRS, or `None` for an unpinned grid.

    Returns:
        The six terms as floats, or `None` when none was given.

    Raises:
        TypeError: If it is not an iterable of numbers.
        ValueError: If it does not hold exactly six finite terms, either pixel
            size is zero (which would collapse the grid to no extent), or either
            shear term is non-zero - a sheared lattice is not axis-aligned, so
            there is no single column a window could be snapped to.

    Examples:
        - A north-up 10 m grid in a projected CRS passes through:
            ```python
            >>> validate_crs_transform([10, 0, 300000, 0, -10, 3400000])
            (10.0, 0.0, 300000.0, 0.0, -10.0, 3400000.0)
            >>> validate_crs_transform(None) is None
            True

            ```
        - A rotated lattice is refused, because no column of it could be
          snapped to:
            ```python
            >>> validate_crs_transform([10, 0.5, 300000, 0.5, -10, 3400000])
            Traceback (most recent call last):
                ...
            ValueError: crs_transform shear terms must be zero: earthlens pins axis-aligned grids, got x_shear=0.5, y_shear=0.5

            ```
        - So is a transform of the wrong length:
            ```python
            >>> validate_crs_transform((10, 0, 300000))
            Traceback (most recent call last):
                ...
            ValueError: crs_transform must have 6 elements, got 3

            ```
    """
    if crs_transform is None:
        return None
    if isinstance(crs_transform, (str, bytes, bytearray)) or not isinstance(
        crs_transform, Iterable
    ):
        raise TypeError(
            "crs_transform must be an iterable of six numbers "
            "(x_scale, x_shear, x_origin, y_shear, y_scale, y_origin), got "
            f"{type(crs_transform).__name__}"
        )
    try:
        terms = tuple(float(term) for term in crs_transform)
    except (TypeError, ValueError):
        raise TypeError(
            f"crs_transform must hold six numbers, got {crs_transform!r}"
        ) from None
    if len(terms) != 6:
        raise ValueError(f"crs_transform must have 6 elements, got {len(terms)}")
    if not all(math.isfinite(term) for term in terms):
        raise ValueError(f"crs_transform terms must be finite, got {terms}")
    if not terms[0] or not terms[4]:
        raise ValueError(
            "crs_transform pixel sizes must be non-zero, got "
            f"x_scale={terms[0]}, y_scale={terms[4]}"
        )
    if terms[1] or terms[3]:
        raise ValueError(
            "crs_transform shear terms must be zero: earthlens pins axis-aligned "
            f"grids, got x_shear={terms[1]}, y_shear={terms[3]}"
        )
    return terms


def same_epsg_code(region_crs: Any, target: str) -> bool:
    """Compare a CRS pyproj could not parse against the target by EPSG code.

    The last resort when `same_crs`'s object comparison cannot run. A code
    is trusted only against an explicitly `EPSG:` target, so the authority stays
    part of the comparison rather than being dropped — which is what made
    string-parsing the wrong answer in the first place.

    Args:
        region_crs: The region's CRS object.
        target: The output CRS.

    Returns:
        `True` only when the target is an `EPSG:` code the region reports as its
        own; `False` for anything unparseable or non-EPSG, since reprojecting is
        the safe answer.

    Examples:
        - A region reporting the target's own code is already in it:
            ```python
            >>> from pyproj import CRS
            >>> same_epsg_code(CRS.from_epsg(32636), "EPSG:32636")
            True

            ```
        - The authority stays part of the comparison, so a target that is not
          an `EPSG:` code is never matched on its number alone:
            ```python
            >>> from pyproj import CRS
            >>> same_epsg_code(CRS.from_epsg(32636), "+proj=utm +zone=36")
            False

            ```
    """
    to_epsg = getattr(region_crs, "to_epsg", None)
    if not callable(to_epsg) or not target.upper().startswith("EPSG:"):
        return False
    code = target.split(":", 1)[1]
    return bool(code.isdigit() and to_epsg() == int(code))


def same_crs(region_crs: Any, target: str) -> bool:
    """Return whether a region's CRS already is the output CRS.

    Compared as CRS objects rather than by string-parsing an `AUTH:CODE` tail.
    Parsing the code answers the wrong question twice: it drops the authority, so
    any non-EPSG code collides with the EPSG code of the same number, and it has
    nothing to read at all when the target is a PROJ string or WKT — under which
    an already-correct region gets warped needlessly. Comparing objects lets PROJ
    decide, which also gets the genuinely-equal cases right: `ESRI:102100` and
    `EPSG:3857` describe one CRS and compare equal, where the codes never would.

    Args:
        region_crs: The region's own CRS object.
        target: The output CRS, in any form pyproj accepts.

    Returns:
        `True` when the two describe the same CRS. When either side cannot be
        parsed the answer falls back to `same_epsg_code`, which says
        `True` only for an `EPSG:` target the region reports as its own code,
        and `False` otherwise — so an unresolvable comparison reprojects, which
        is the safe answer.

    Examples:
        - Two spellings of one CRS compare equal, which comparing the codes
          would never manage:
            ```python
            >>> same_crs("ESRI:102100", "EPSG:3857")
            True

            ```
        - Genuinely different CRSs still differ, so the region is reprojected:
            ```python
            >>> same_crs("EPSG:4326", "EPSG:3857")
            False

            ```

    Note:
        Equality here is PROJ's, which includes axis order, so some pairs that
        name the same datum still compare `False` — `OGC:CRS84` and
        `+proj=longlat +datum=WGS84` both differ from `EPSG:4326` on that
        basis. The direction is safe (a `False` costs a reproject that was not
        strictly needed, never a skipped one), but do not read this as "names
        the same datum".

    """
    from pyproj import CRS

    try:
        return bool(CRS.from_user_input(region_crs) == CRS.from_user_input(target))
    except Exception:  # noqa: BLE001
        # A parse failure on either side falls back to the code check rather
        # than assuming a match.
        return same_epsg_code(region_crs, target)


def bbox_to_crs(
    latlon_bbox: tuple[float, float, float, float], crs: str
) -> tuple[float, float, float, float]:
    """Reproject a lat/lon AOI envelope into `crs`.

    Args:
        latlon_bbox: `(min_lon, min_lat, max_lon, max_lat)` in EPSG:4326.
        crs: The target CRS.

    Returns:
        The envelope in `crs`; the input is returned unchanged when `crs` is
        already EPSG:4326. An AOI outside the projection's domain comes back
        with infinite bounds rather than raising, which
        `projected_grid` then rejects by name.

    Raises:
        pyproj.exceptions.CRSError: If `crs` is not a CRS pyproj can build a
            transformer for.
    """
    if crs.upper() == NATIVE_CRS:
        return latlon_bbox
    from pyproj import Transformer

    transformer = Transformer.from_crs(NATIVE_CRS, crs, always_xy=True)
    # transform_bounds densifies the edges, so a reprojected rectangle that bows
    # still bounds the whole AOI rather than only its corners.
    #
    # Known asymmetry for a *large* projected AOI: upstream converts this window
    # back to EPSG:4326 with a four-corner transform, which under-estimates the
    # reach of an edge that bows outward. Measured on a 10 deg x 10 deg AOI, the
    # requested northern edge came back ~0.023 deg short, so a poleward strip can
    # read as nodata, while ~0.5 deg of unrequested ground is added on each side.
    # Small AOIs (the common case) are affected at the ~0.002 deg level. Fixing it
    # properly needs the densified back-transform upstream:
    # serapeum-org/pyramids-eo#97.
    return transformer.transform_bounds(*latlon_bbox)


def bbox_to_latlon(
    bbox: tuple[float, float, float, float], crs: str
) -> tuple[float, float, float, float]:
    """Bring an envelope in `crs` back to EPSG:4326.

    The inverse of `bbox_to_crs`, which only ever converts *into* an
    output CRS. Scene discovery needs the other direction: it queries the Earth
    Engine catalog in lat/lon whatever the request's CRS, and it has to ask about
    the window the read will take.

    Args:
        bbox: `(min_x, min_y, max_x, max_y)` in `crs`.
        crs: The CRS `bbox` is expressed in.

    Returns:
        The envelope in EPSG:4326; the input unchanged when `crs` is already
        EPSG:4326.

    Raises:
        pyproj.exceptions.CRSError: If `crs` is not a CRS pyproj can build a
            transformer for.

    Examples:
        - A lat/lon window is already the answer, so it is handed back
          untouched:
            ```python
            >>> bbox_to_latlon((31.2, 29.9, 31.3, 30.0), "EPSG:4326")
            (31.2, 29.9, 31.3, 30.0)

            ```
        - A UTM window comes back in degrees:
            ```python
            >>> window = (330_000.0, 3_310_000.0, 340_000.0, 3_320_000.0)
            >>> [round(term, 2) for term in bbox_to_latlon(window, "EPSG:32636")]
            [31.24, 29.91, 31.34, 30.0]

            ```
    """
    if crs.upper() == NATIVE_CRS:
        return bbox
    from pyproj import Transformer

    transformer = Transformer.from_crs(crs, NATIVE_CRS, always_xy=True)
    # Densified like the forward call, so a reprojected rectangle whose edge
    # bows outward still bounds the whole window rather than only its corners.
    return transformer.transform_bounds(*bbox)


def geographic_grid(
    bbox: tuple[float, float, float, float], scale: float
) -> tuple[int, int]:
    """Size a pixel grid for a lat/lon `bbox` at a metre `scale`.

    The metre `scale` has to become an explicit grid, because a geographic CRS
    measures in degrees. This is deliberately **not**
    `SpatialExtent.estimate_pixel_dims`, which pyramids documents as a worst-case
    upper bound for cap pre-checks — it over-counts both axes, and unevenly, so a
    square AOI comes out non-square. Here the real span is used, with longitude
    degrees shortened by `cos(latitude)`, so the pixels are square on the ground
    at the requested `scale`.

    Args:
        bbox: `(min_lon, min_lat, max_lon, max_lat)` in EPSG:4326.
        scale: Target ground sample distance in metres.

    Returns:
        `(rows, cols)`, at least one pixel per axis.

    Raises:
        ValueError: If any bound is not finite, or `scale` is not a positive
            finite number of metres.

    Examples:
        - A small AOI over Cairo at 90 m:
            ```python
            >>> geographic_grid((31.2, 29.9, 31.3, 30.0), 90.0)
            (124, 108)

            ```
        - Spanning the equator the two axes match, because a degree of longitude
          is barely shortened there:
            ```python
            >>> geographic_grid((0.0, 0.0, 1.0, 1.0), 1000.0)
            (112, 112)

            ```
        - An AOI smaller than one pixel still yields a readable raster:
            ```python
            >>> geographic_grid((31.2, 29.9, 31.2001, 29.9001), 90.0)
            (1, 1)

            ```
    """
    min_x, min_y, max_x, max_y = bbox
    if not all(math.isfinite(bound) for bound in bbox):
        raise ValueError(f"the AOI bounds must be finite, got {bbox}")
    if scale <= 0 or not math.isfinite(scale):
        raise ValueError(f"scale must be a positive number of metres, got {scale}")
    # `cos` at the EQUATORWARD edge of the AOI, which is the edge where a
    # degree of longitude is longest and so needs the most pixels to hold the
    # requested ground sample distance. Sizing on the poleward edge instead -
    # which is what this did before, with a comment claiming the opposite -
    # takes the SMALLEST cosine, yields the FEWEST columns and so delivers
    # pixels coarser than asked: AOI (0, 50, 1, 70) at scale=1000 produced 39
    # columns, i.e. 1835 m per pixel at latitude 50, and a global AOI at 1 km
    # produced 70 columns where 40076 are needed.
    #
    # The consequence of this choice is that a tall AOI is over-sampled towards
    # the pole rather than under-sampled towards the equator. That is the right
    # way round: an over-sampled read still contains every pixel the caller
    # asked for and can be downsampled, while an under-sampled one has thrown
    # the detail away and cannot be recovered.
    #
    # The clamp keeps `cos` away from the pole itself, where it reaches zero;
    # it now applies to the far edge, since the near one cannot be the pole
    # unless the whole AOI is.
    if min_y <= 0.0 <= max_y:
        # The AOI straddles the equator, so the equator itself is inside it and
        # is the widest latitude: `cos` is 1 and longitude is not shortened at
        # all. Taking `min(|min_y|, |max_y|)` here would be wrong - for a global
        # AOI that is 90, which collapses the width to almost nothing.
        equatorward = 0.0
    else:
        equatorward = min(abs(min_y), abs(max_y))
    if equatorward > _MAX_POLEWARD_DEGREES:
        # An AOI entirely inside the polar cap: clamp so `cos` stays non-zero.
        equatorward = _MAX_POLEWARD_DEGREES
    height_m = abs(max_y - min_y) * _METRES_PER_DEGREE
    width_m = (
        abs(max_x - min_x) * _METRES_PER_DEGREE * math.cos(math.radians(equatorward))
    )
    # Round up, not to nearest: rounding down would leave the raster a little
    # coarser than the scale that was asked for.
    rows = max(1, math.ceil(height_m / scale))
    cols = max(1, math.ceil(width_m / scale))
    return rows, cols


def projected_grid(
    bbox: tuple[float, float, float, float], scale: float, crs: str
) -> tuple[int, int]:
    """Size a pixel grid for a `bbox` already in a metre-based projected `crs`.

    Args:
        bbox: `(min_x, min_y, max_x, max_y)` in `crs`.
        scale: Target ground sample distance in metres.
        crs: The projected CRS, named only so the error message can quote it.

    Returns:
        `(rows, cols)`, at least one pixel per axis.

    Raises:
        ValueError: If any bound is not finite, or `scale` is not positive.

    Examples:
        - A 10 km UTM window at 30 m, rounded up on both axes:
            ```python
            >>> projected_grid((300000.0, 3300000.0, 310000.0, 3310000.0), 30.0, "EPSG:32636")
            (334, 334)

            ```
        - A window outside the projection's domain reprojects to infinities,
          which are rejected by name rather than overflowing the budget maths:
            ```python
            >>> projected_grid((float("inf"), 0.0, 1.0, 1.0), 30.0, "EPSG:32636")
            Traceback (most recent call last):
                ...
            ValueError: the AOI bounds must all be finite, got (inf, 0.0, 1.0, 1.0) in EPSG:32636

            ```
    """
    if not all(math.isfinite(bound) for bound in bbox):
        # `transform_bounds` returns infinities for an AOI outside the
        # projection's domain; without this the budget maths would raise an
        # opaque OverflowError from `math.ceil` instead.
        raise ValueError(f"the AOI bounds must all be finite, got {bbox!r} in {crs}")
    # `isfinite` as well as the sign test: an infinite scale otherwise sized a
    # 1x1 grid and a NaN one reached `math.ceil` to raise a bare CPython
    # message, against a docstring promising a positive finite number.
    if not scale or scale <= 0 or not math.isfinite(scale):
        raise ValueError("'scale' must be a positive number of metres.")
    min_x, min_y, max_x, max_y = (float(v) for v in bbox)
    # `abs`, as the geographic sizer does: an inverted bbox silently produced a
    # single column here rather than the extent it spans.
    rows = max(math.ceil(abs(max_y - min_y) / scale), 1)
    cols = max(math.ceil(abs(max_x - min_x) / scale), 1)
    return rows, cols


class OutputGrid(SummarisedLeaf):
    """The window and pixel grid one request reads on.

    Attributes:
        bbox: `(min_x, min_y, max_x, max_y)` in `crs` — the window the grid is
            sized for. Derived from the `region`'s own bounds when one was given,
            because sizing a bbox-shaped grid for a region-shaped window would
            silently change the ground resolution by the ratio of the two extents.
        crs: The output CRS the window is expressed in.
        rows: Grid height in pixels.
        cols: Grid width in pixels.
        scale: The metre ground sample distance the grid was sized for, or `None`
            when the grid came from an explicit `crs_transform`.
        crs_transform: The 6-element affine transform the grid was pinned to, or
            `None` when `rows`/`cols` were sized from `scale`. Carried rather
            than discarded because a pinned grid has to reach the request that
            quotes it: Earth Engine takes the transform itself, not the pixel
            count it implies.
        convention: Which sizing rule produced `rows`/`cols` — `"metre"` for the
            EEDAI reader's square-metre grid, `"degree"` for Earth Engine's
            degree-equivalent reading of `scale`, `"transform"` for a pinned one.
        cutline: The region to clip the result to, or `None`. Carried alongside
            the bbox because the reader takes both: the bbox sizes the grid, the
            cutline trims the output to the exact polygon.

    Examples:
        - A grid reports its shape, its total pixels and the convention that
          sized it:
            ```python
            >>> grid = OutputGrid.for_metre_grid(
            ...     (31.2, 29.9, 31.3, 30.0), crs="EPSG:4326", scale=90.0
            ... )
            >>> grid.as_rows_cols()
            (124, 108)
            >>> grid.pixels, grid.longest_axis, grid.convention
            (13392, 124, 'metre')

            ```
        - The two conventions disagree on the column count away from the
          equator, which is exactly what this type makes visible: at 30 deg N a
          degree of longitude is about 13% shorter than a degree of latitude:
            ```python
            >>> bbox = (31.2, 29.9, 31.3, 30.0)
            >>> OutputGrid.for_metre_grid(bbox, crs="EPSG:4326", scale=90.0).cols
            108
            >>> OutputGrid.for_earth_engine(bbox, crs="EPSG:4326", scale=90.0).cols
            124

            ```
        - A budget check names the budget it broke, so the message can be
          handed to the user unchanged:
            ```python
            >>> grid = OutputGrid.for_metre_grid(
            ...     (31.2, 29.9, 31.3, 30.0), crs="EPSG:4326", scale=90.0
            ... )
            >>> grid.fits(max_pixels=10_000, max_dimension=4096)
            (False, 'about 13,392 px on a 108x124 grid, over the 10,000-px budget')
            >>> grid.fits(max_pixels=1_000_000, max_dimension=4096)
            (True, '')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    _summary_fields: ClassVar[tuple[str, ...]] = (
        "crs",
        "rows",
        "cols",
        "scale",
        "convention",
        "crs_transform",
    )

    bbox: tuple[float, float, float, float]
    crs: str
    # `ge=1`: a 0x0 grid used to satisfy `fits()` with room to spare, and a
    # negative one passed the per-axis cap through `longest_axis`.
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)
    scale: float | None = None
    convention: str = "metre"
    cutline: Any = None
    crs_transform: tuple[float, ...] | None = None

    @classmethod
    def for_metre_grid(
        cls,
        bbox: tuple[float, float, float, float],
        *,
        crs: str,
        scale: float,
        cutline: Any = None,
    ) -> OutputGrid:
        """Size for square metres on the ground — the EEDAI reader's convention.

        Dispatches on the CRS: EPSG:4326 keeps the geographic grid with its
        `cos(latitude)` longitude shortening; a metre-based projected CRS sizes
        each axis by its span over the metre `scale` directly.

        Args:
            bbox: The window in `crs`.
            crs: The output CRS.
            scale: Target ground sample distance in metres.
            cutline: An optional region to clip to.

        Returns:
            The sized grid, with `convention="metre"`.

        Raises:
            ValueError: If any bound is not finite, or `scale` is not a positive
                finite number of metres.
        """
        if crs.upper() == NATIVE_CRS:
            rows, cols = geographic_grid(bbox, scale)
        else:
            rows, cols = projected_grid(bbox, scale, crs)
        return cls(
            bbox=bbox,
            crs=crs,
            rows=rows,
            cols=cols,
            scale=scale,
            convention="metre",
            cutline=cutline,
        )

    @classmethod
    def for_earth_engine(
        cls,
        bbox: tuple[float, float, float, float],
        *,
        crs: str,
        scale: float,
        cutline: Any = None,
    ) -> OutputGrid:
        """Size the way Earth Engine reads `scale` in a geographic CRS.

        Earth Engine treats `scale` as a uniform degree-equivalent rather than
        shortening longitude by `cos(latitude)`, so away from the equator this
        produces a different column count from `for_metre_grid`. That
        difference is real and documented, not a bug in either.

        Args:
            bbox: The window in `crs`.
            crs: The output CRS.
            scale: Target ground sample distance in metres.
            cutline: An optional region to clip to.

        Returns:
            The sized grid, with `convention="degree"`.

        Raises:
            ValueError: If any bound is not finite, or `scale` is not a positive
                finite number of metres.

        See Also:
            OutputGrid.for_metre_grid: The square-metre convention the EEDAI
                reader sizes with, which disagrees on the column count away
                from the equator.
        """
        if crs.upper() == NATIVE_CRS:
            if not all(math.isfinite(bound) for bound in bbox):
                raise ValueError(f"the AOI bounds must be finite, got {bbox}")
            if scale <= 0 or not math.isfinite(scale):
                raise ValueError(
                    f"scale must be a positive number of metres, got {scale}"
                )
            degrees = scale / _METRES_PER_DEGREE
            min_x, min_y, max_x, max_y = bbox
            rows = max(1, math.ceil(abs(max_y - min_y) / degrees))
            cols = max(1, math.ceil(abs(max_x - min_x) / degrees))
        else:
            rows, cols = projected_grid(bbox, scale, crs)
        return cls(
            bbox=bbox,
            crs=crs,
            rows=rows,
            cols=cols,
            scale=scale,
            convention="degree",
            cutline=cutline,
        )

    @classmethod
    def from_crs_transform(
        cls,
        bbox: tuple[float, float, float, float],
        *,
        crs: str,
        crs_transform: tuple[float, ...],
        cutline: Any = None,
    ) -> OutputGrid:
        """Build a grid from an explicit affine transform.

        Pins the pixel size *and* the grid origin, so two requests over slightly
        different AOIs land on the same grid and differencing them needs no
        resample. Mutually exclusive with `scale`, which Earth Engine also treats
        as an either/or.

        `bbox` is a request, not the answer: the window is snapped **outward**
        onto the lattice the transform defines, so the returned grid's `bbox`
        covers the one asked for and begins on a grid line. Its `crs_transform`
        therefore carries the *snapped* origin — which is the origin the request
        must quote, since the pinned one may lie far outside the AOI — while the
        pixel sizes are the ones that were pinned.

        Args:
            bbox: The window to cover, `(min_x, min_y, max_x, max_y)` in `crs`.
            crs: The output CRS.
            crs_transform: A 6-element affine transform
                `(x_scale, x_shear, x_origin, y_shear, y_scale, y_origin)`, in
                the units of `crs`.
            cutline: An optional region to clip to.

        Returns:
            The grid implied by the transform over `bbox`, snapped outward to
            the lattice, with `convention="transform"`, no `scale`, and
            `crs_transform` carrying the snapped transform to the request.

        Raises:
            TypeError: If `crs_transform` is not an iterable of numbers.
            ValueError: If the transform is not six finite terms, its pixel
                sizes are zero, or it carries shear.

        Examples:
            - A window already on the lattice is kept, and the transform comes
              back alongside the pixel counts:
                ```python
                >>> grid = OutputGrid.from_crs_transform(
                ...     (300_000.0, 3_399_000.0, 300_100.0, 3_400_000.0),
                ...     crs="EPSG:32636",
                ...     crs_transform=(10, 0, 300_000, 0, -10, 3_400_000),
                ... )
                >>> grid.cols, grid.rows
                (10, 100)
                >>> grid.crs_transform
                (10.0, 0.0, 300000.0, 0.0, -10.0, 3400000.0)

                ```
            - A window off the lattice is widened to it, so the origin stays a
              whole number of pixels from the pinned one:
                ```python
                >>> grid = OutputGrid.from_crs_transform(
                ...     (300_005.0, 3_399_995.0, 300_025.0, 3_400_000.0),
                ...     crs="EPSG:32636",
                ...     crs_transform=(10, 0, 300_000, 0, -10, 3_400_000),
                ... )
                >>> grid.bbox
                (300000.0, 3399990.0, 300030.0, 3400000.0)
                >>> grid.cols, grid.rows
                (3, 1)
                >>> grid.scale is None, grid.convention
                (True, 'transform')

                ```
        """
        terms = validate_crs_transform(crs_transform)
        assert terms is not None  # a transform was given, so one comes back
        x_scale, x_shear, x_origin, y_shear, y_scale, y_origin = terms
        min_x, min_y, max_x, max_y = bbox
        x_pixel, y_pixel = abs(x_scale), abs(y_scale)
        # Snapped OUTWARD onto the lattice the transform defines, which is the
        # whole point of pinning: the window has to start on a grid line, or two
        # AOIs would share a pixel size and still be half a pixel apart. The
        # output then covers the AOI and begins at a whole number of pixels from
        # the pinned origin - so the request quotes THIS origin, not the one it
        # was given, which may lie far outside the AOI.
        first_col = _lattice_floor((min_x - x_origin) / x_pixel)
        last_col = _lattice_ceil((max_x - x_origin) / x_pixel)
        # y counts downward from the pinned origin, the north-up convention the
        # negative `y_scale` expresses.
        first_row = _lattice_floor((y_origin - max_y) / y_pixel)
        last_row = _lattice_ceil((y_origin - min_y) / y_pixel)
        cols = max(1, last_col - first_col)
        rows = max(1, last_row - first_row)
        snapped_min_x = x_origin + first_col * x_pixel
        snapped_max_y = y_origin - first_row * y_pixel
        return cls(
            bbox=(
                snapped_min_x,
                snapped_max_y - rows * y_pixel,
                snapped_min_x + cols * x_pixel,
                snapped_max_y,
            ),
            crs=crs,
            rows=rows,
            cols=cols,
            scale=None,
            convention="transform",
            cutline=cutline,
            crs_transform=(
                x_pixel,
                x_shear,
                snapped_min_x,
                y_shear,
                -y_pixel,
                snapped_max_y,
            ),
        )

    def latlon_bbox(self) -> tuple[float, float, float, float]:
        """Return this grid's window in EPSG:4326.

        Part of this value object's public surface, for a caller holding a
                grid in a projected CRS who needs to say where it is in lat/lon - a
                catalog query, a map extent, a sanity check against an AOI.

                The backend's own scene discovery deliberately does **not** go through
                here: it derives its lat/lon AOI from the original geometry
                (`GEE._eedai_latlon_aoi`), because transforming a projected *window*
                back loses a little ground at the edges where the envelope bows
                (pyramids-eo#97). Reprojecting a bbox is the right answer only when the
                bbox is all you have.

                Returns:
                    `(min_lon, min_lat, max_lon, max_lat)`, the bbox unchanged when the
                    grid is already in EPSG:4326.

                Examples:
                    - A geographic grid answers with its own bbox:
                        ```python
                        >>> grid = OutputGrid.for_metre_grid(
                        ...     (31.2, 29.9, 31.3, 30.0), crs="EPSG:4326", scale=90.0
                        ... )
                        >>> grid.latlon_bbox()
                        (31.2, 29.9, 31.3, 30.0)

                        ```
                    - A projected one is brought back:
                        ```python
                        >>> utm = OutputGrid.for_metre_grid(
                        ...     (330_000.0, 3_310_000.0, 340_000.0, 3_320_000.0),
                        ...     crs="EPSG:32636",
                        ...     scale=90.0,
                        ... )
                        >>> [round(term, 2) for term in utm.latlon_bbox()]
                        [31.24, 29.91, 31.34, 30.0]

                        ```
        """
        return bbox_to_latlon(self.bbox, self.crs)

    def request_transform(self) -> tuple[float, ...]:
        """Return the affine transform that describes this grid.

        Earth Engine's `computePixels` grid is pixel dimensions plus a transform;
        there is no `scale` field, and a request that sends dimensions alone gets
        the CRS's default transform - one unit per pixel at the CRS origin, which
        is how a 90 m AOI over Cairo came back as 1-degree pixels starting at
        lat/lon 0. So every request needs one, pinned or not.

        A pinned grid already is a transform. An unsized one derives it from the
        window and the pixel counts, dividing exactly, so the raster covers the
        requested bbox rather than overhanging it by the rounding the ceiling
        introduced.

        Returns:
            `(x_scale, x_shear, x_origin, y_shear, y_scale, y_origin)`, north-up
            (so `y_scale` is negative and the origin is the top-left corner).

        Examples:
            - An unpinned grid describes the window it was sized for:
                ```python
                >>> grid = OutputGrid.for_metre_grid(
                ...     (31.2, 29.9, 31.3, 30.0), crs="EPSG:4326", scale=90.0
                ... )
                >>> terms = grid.request_transform()
                >>> round(terms[0], 8), terms[2], terms[5]
                (0.00092593, 31.2, 30.0)

                ```
            - A pinned grid hands back the transform it was snapped to, so the
              request quotes the lattice rather than the AOI:
                ```python
                >>> pinned = OutputGrid.from_crs_transform(
                ...     (300_005.0, 3_399_995.0, 300_025.0, 3_400_000.0),
                ...     crs="EPSG:32636",
                ...     crs_transform=(10, 0, 300_000, 0, -10, 3_400_000),
                ... )
                >>> pinned.request_transform()
                (10.0, 0.0, 300000.0, 0.0, -10.0, 3400000.0)

                ```
        """
        if self.crs_transform is not None:
            return self.crs_transform
        min_x, min_y, max_x, max_y = self.bbox
        return (
            (max_x - min_x) / self.cols,
            0.0,
            min_x,
            0.0,
            -(max_y - min_y) / self.rows,
            max_y,
        )

    def as_rows_cols(self) -> tuple[int, int]:
        """Return `(rows, cols)`, the shape the reader's `Window` takes.

        Returns:
            The grid height and width in pixels.
        """
        return self.rows, self.cols

    @property
    def pixels(self) -> int:
        """Total pixels in one band of this grid.

        Returns:
            `rows * cols`. Multiply by the band count — or call
            `band_pixels` — before comparing against a total-pixel
            budget.
        """
        return self.rows * self.cols

    @property
    def longest_axis(self) -> int:
        """The larger of `rows` and `cols`, which per-axis caps apply to.

        Returns:
            The longer side in pixels, the figure Earth Engine's 32768-px
            per-axis limit is measured against.
        """
        return max(self.rows, self.cols)

    def band_pixels(self, band_count: int) -> int:
        """Total pixels across `band_count` bands.

        Args:
            band_count: How many bands the read requests; treated as at least 1.

        Returns:
            `pixels * max(band_count, 1)`.
        """
        return self.pixels * max(band_count, 1)

    def fits(
        self, *, max_pixels: int, max_dimension: int, band_count: int = 1
    ) -> tuple[bool, str]:
        """Say whether this grid is within a per-axis and a total-pixel budget.

        Args:
            max_pixels: Largest total pixel count allowed across all bands.
            max_dimension: Largest allowed pixels on either axis.
            band_count: How many bands the read requests.

        Returns:
            `(True, "")` when it fits, else `(False, reason)` where the reason
            names the budget that was exceeded and by how much.
        """
        if self.longest_axis > max_dimension:
            return False, (
                f"{self.longest_axis:,} px on the longest axis, over the "
                f"{max_dimension:,}-px per-axis cap"
            )
        total = self.band_pixels(band_count)
        if total > max_pixels:
            return False, (
                f"about {total:,} px on a {self.cols}x{self.rows} grid, over the "
                f"{max_pixels:,}-px budget"
            )
        return True, ""

    def tiles(self, tile_pixels: int) -> int:
        """How many tiles of `tile_pixels` per side this grid cuts into.

        Args:
            tile_pixels: Output pixels per tile side.

        Returns:
            The tile count, at least 1.

        Raises:
            ValueError: If `tile_pixels` is not positive.
        """
        if tile_pixels <= 0:
            raise ValueError(f"tile_pixels must be positive, got {tile_pixels}")
        across = math.ceil(self.cols / tile_pixels)
        down = math.ceil(self.rows / tile_pixels)
        return max(1, across * down)
