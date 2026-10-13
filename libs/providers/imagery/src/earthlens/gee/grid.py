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

:class:`OutputGrid` is the value; the constructors are
:meth:`OutputGrid.for_metre_grid` (the EEDAI convention),
:meth:`OutputGrid.for_earth_engine` (the degree-equivalent one) and
:meth:`OutputGrid.from_crs_transform` (an explicitly pinned affine grid).

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
from typing import Any, ClassVar

from pydantic import ConfigDict, Field

from earthlens.base.leaves import SummarisedLeaf

__all__ = [
    "OutputGrid",
    "NATIVE_CRS",
    "same_crs",
    "same_epsg_code",
    "bbox_to_crs",
    "geographic_grid",
    "projected_grid",
]

#: The CRS scene discovery and every lat/lon AOI are expressed in.
NATIVE_CRS: str = "EPSG:4326"

#: Metres per degree of latitude, the constant the geographic grid converts with.
_METRES_PER_DEGREE: float = 111_320.0

#: Latitude beyond which `cos` is clamped, so a polar AOI cannot divide by zero.
_MAX_POLEWARD_DEGREES: float = 89.9


def same_epsg_code(region_crs: Any, target: str) -> bool:
    """Compare a CRS pyproj could not parse against the target by EPSG code.

    The last resort when :func:`same_crs`'s object comparison cannot run. A code
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
        parsed the answer falls back to :func:`same_epsg_code`, which says
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
        :func:`projected_grid` then rejects by name.

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
        produces a different column count from :meth:`for_metre_grid`. That
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

        Args:
            bbox: The window in `crs`.
            crs: The output CRS.
            crs_transform: A 6-element affine transform
                `(x_scale, x_shear, x_origin, y_shear, y_scale, y_origin)`.
            cutline: An optional region to clip to.

        Returns:
            The grid implied by the transform over `bbox`, with
            `convention="transform"` and no `scale`.

        Raises:
            ValueError: If the transform is not 6 elements, or its pixel sizes
                are zero.
        """
        if len(crs_transform) != 6:
            raise ValueError(
                f"crs_transform must have 6 elements, got {len(crs_transform)}"
            )
        x_scale, _x_shear, _x_origin, _y_shear, y_scale, _y_origin = crs_transform
        if not x_scale or not y_scale:
            raise ValueError("crs_transform pixel sizes must be non-zero")
        min_x, min_y, max_x, max_y = bbox
        cols = max(1, math.ceil(abs(max_x - min_x) / abs(x_scale)))
        rows = max(1, math.ceil(abs(max_y - min_y) / abs(y_scale)))
        return cls(
            bbox=bbox,
            crs=crs,
            rows=rows,
            cols=cols,
            scale=None,
            convention="transform",
            cutline=cutline,
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
            :meth:`band_pixels` — before comparing against a total-pixel
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
