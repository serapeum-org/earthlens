"""Adapt a HEALPix field to a regular `pyramids.dataset.Dataset`.

HEALPix (Hierarchical Equal Area isoLatitude Pixelization) stores values per pixel,
indexed by the resolution parameter `nside` (so there are `12 * nside**2` pixels).
Turning a HEALPix field into a raster only needs one operation: the centre
longitude/latitude of each pixel. That mapping is the closed-form `pix2ang` from the
HEALPix paper (Górski et al. 2005); it is implemented here in plain NumPy for both the
RING and NESTED pixel orderings, so **no HEALPix C library (`healpy`) is required**.
The pixel centres are then handed to the same scattered-point bridge
(`pyramids.dataset.ops.interpolate.grid_points`) used by `from_octahedral`. No new
third-party dependencies.
"""

from __future__ import annotations

import math

import numpy as np
from geopandas import GeoDataFrame, points_from_xy
from pyramids.dataset import Dataset
from pyramids.dataset.ops.interpolate import grid_points

# Per-base-face ring/phi offsets for the NESTED -> RING index conversion, matching the
# canonical HEALPix `xyf2ring` tables (Górski et al. 2005).
_JRLL = np.array([2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4], dtype=np.int64)
_JPLL = np.array([1, 3, 5, 7, 0, 2, 4, 6, 1, 3, 5, 7], dtype=np.int64)


def _ring_pix2lonlat(nside: int, ipix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return centre longitude/latitude (degrees) for RING-ordered pixel indices."""
    npix = 12 * nside * nside
    ncap = 2 * nside * (nside - 1)
    z = np.empty(ipix.shape, dtype=np.float64)
    phi = np.empty(ipix.shape, dtype=np.float64)

    north = ipix < ncap
    p = ipix[north]
    ph = (p + 1) / 2.0
    i = np.floor(np.sqrt(ph - np.sqrt(np.floor(ph)))).astype(np.int64) + 1
    j = p + 1 - 2 * i * (i - 1)
    z[north] = 1.0 - (i * i) / (3.0 * nside * nside)
    phi[north] = (j - 0.5) * (np.pi / 2.0) / i

    equator = (ipix >= ncap) & (ipix < npix - ncap)
    p = ipix[equator] - ncap
    i = (p // (4 * nside)) + nside
    j = (p % (4 * nside)) + 1
    shift = (i - nside + 1) % 2
    z[equator] = (2 * nside - i) * (2.0 / (3.0 * nside))
    phi[equator] = (j - shift / 2.0) * (np.pi / 2.0) / nside

    south = ipix >= npix - ncap
    p = npix - 1 - ipix[south]
    ph = (p + 1) / 2.0
    i = np.floor(np.sqrt(ph - np.sqrt(np.floor(ph)))).astype(np.int64) + 1
    j = p + 1 - 2 * i * (i - 1)
    z[south] = -(1.0 - (i * i) / (3.0 * nside * nside))
    phi[south] = (j - 0.5) * (np.pi / 2.0) / i

    lon = np.degrees(phi) % 360.0
    lat = np.degrees(np.arcsin(z))
    return lon, lat


def _nest2ring(nside: int, ipix: np.ndarray) -> np.ndarray:
    """Convert NESTED-ordered pixel indices to RING-ordered indices."""
    order = int(round(math.log2(nside)))
    npix = 12 * nside * nside
    ncap = 2 * nside * (nside - 1)
    nl4 = 4 * nside

    face = ipix >> (2 * order)
    in_face = ipix & (nside * nside - 1)
    ix = np.zeros_like(in_face)
    iy = np.zeros_like(in_face)
    for bit in range(order):
        ix |= ((in_face >> (2 * bit)) & 1) << bit
        iy |= ((in_face >> (2 * bit + 1)) & 1) << bit

    jr = _JRLL[face] * nside - ix - iy - 1
    nr = np.empty_like(jr)
    n_before = np.empty_like(jr)
    kshift = np.empty_like(jr)

    north = jr < nside
    nr[north] = jr[north]
    n_before[north] = 2 * nr[north] * (nr[north] - 1)
    kshift[north] = 0

    south = jr > 3 * nside
    nr[south] = nl4 - jr[south]
    n_before[south] = npix - 2 * (nr[south] + 1) * nr[south]
    kshift[south] = 0

    equator = (~north) & (~south)
    nr[equator] = nside
    n_before[equator] = ncap + (jr[equator] - nside) * nl4
    kshift[equator] = (jr[equator] - nside) & 1

    jp = (_JPLL[face] * nr + ix - iy + 1 + kshift) // 2
    jp = np.where(jp > nl4, jp - nl4, jp)
    jp = np.where(jp < 1, jp + nl4, jp)
    ring: np.ndarray = n_before + jp - 1
    return ring


def _pix2lonlat(
    nside: int, ipix: np.ndarray, nest: bool
) -> tuple[np.ndarray, np.ndarray]:
    """Return centre longitude/latitude (degrees) for pixel indices in either ordering."""
    ring_ipix = _nest2ring(nside, ipix) if nest else ipix
    return _ring_pix2lonlat(nside, ring_ipix)


def from_healpix(
    values: np.ndarray,
    *,
    nside: int | None = None,
    nest: bool = False,
    cell_size: float,
    method: str = "nearest",
    epsg: int = 4326,
    bbox: tuple[float, float, float, float] | None = None,
) -> Dataset:
    """Regrid a HEALPix field onto a regular-grid `Dataset`.

    Each HEALPix pixel's centre longitude/latitude is computed in plain NumPy (no
    `healpy` dependency) and the resulting points are interpolated with `gdal.Grid`
    via `pyramids.dataset.ops.interpolate.grid_points`.

    Args:
        values: 1-D array of per-pixel HEALPix values, length `12 * nside**2`.
        nside: HEALPix resolution parameter. Derived from `len(values)` when omitted.
        nest: `True` for NESTED pixel ordering, `False` (default) for RING.
        cell_size: Output pixel size in the target CRS units (degrees for EPSG:4326).
        method: A `gdal.Grid` algorithm string (e.g. `"nearest"`, `"linear"`,
            `"invdist:power=2.0:smoothing=0.0"`).
        epsg: Output EPSG code.
        bbox: Optional `(minx, miny, maxx, maxy)` output extent in the target CRS.
            Defaults to the pixel-centres' bounding box; pass e.g.
            `(-180, -90, 180, 90)` to pin a fixed global grid.

    Returns:
        A single-band `pyramids.dataset.Dataset` of the interpolated surface.

    Raises:
        ValueError: `values` is not 1-D; `len(values)` is not a valid HEALPix pixel
            count (`12 * nside**2`); `nside` disagrees with `len(values)`; or
            `nest=True` with an `nside` that is not a power of two.

    Examples:
        - Regrid a synthetic `nside=1` field (12 pixels) and inspect the raster:
            ```python
            >>> import numpy as np
            >>> from earthlens.grids import from_healpix
            >>> ds = from_healpix(np.arange(12.0), cell_size=30.0)
            >>> ds.band_count
            1
            >>> ds.epsg
            4326

            ```
        - NESTED ordering is supported and requires a power-of-two `nside`:
            ```python
            >>> import numpy as np
            >>> from earthlens.grids import from_healpix
            >>> ds = from_healpix(np.arange(48.0), nside=2, nest=True, cell_size=20.0)
            >>> ds.band_count
            1

            ```
        - A length that is not a valid HEALPix pixel count is rejected:
            ```python
            >>> import numpy as np
            >>> from earthlens.grids import from_healpix
            >>> try:
            ...     from_healpix(np.zeros(10), cell_size=30.0)
            ... except ValueError as exc:
            ...     print("valid HEALPix pixel count" in str(exc))
            True

            ```

    See Also:
        - `from_octahedral`: the sibling point-based adapter this function delegates to
          via `grid_points`.
    """
    values = np.asarray(values, dtype=np.float64).ravel()
    npix = values.size

    if nside is None:
        derived = int(math.isqrt(npix // 12))
        if derived < 1 or 12 * derived * derived != npix:
            raise ValueError(
                f"values length {npix} is not a valid HEALPix pixel count "
                "(12 * nside**2); pass an explicit nside if this is intentional."
            )
        nside = derived
    elif nside < 1 or 12 * nside * nside != npix:
        raise ValueError(
            f"nside={nside} implies {12 * (nside or 0) ** 2} pixels but values has "
            f"{npix}; they must be a valid HEALPix pair (12 * nside**2)."
        )

    if nest and (nside & (nside - 1)) != 0:
        raise ValueError(
            f"NESTED ordering requires nside to be a power of two; got nside={nside}."
        )

    lon, lat = _pix2lonlat(nside, np.arange(npix), nest)
    gdf = GeoDataFrame(
        {"z": values},
        geometry=points_from_xy(lon, lat),
        crs=epsg,
    )
    result = grid_points(
        gdf, "z", Dataset, algorithm=method, cell_size=cell_size, bbox=bbox, epsg=epsg
    )
    return result
