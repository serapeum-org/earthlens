"""Adapt an ORCA curvilinear ocean grid to a regular `pyramids.dataset.Dataset`.

ORCA grids (NEMO ocean model) store coordinates as two-dimensional `(ny, nx)`
longitude/latitude arrays describing quadrilateral cells. pyramids already has a
mesh-to-raster bridge (`pyramids.netcdf.ugrid.interpolation.mesh_to_grid`, reached
through `pyramids.netcdf.ugrid.dataset.UgridDataset.to_dataset`); this module turns the
curvilinear field into a UGRID quad mesh and reuses that bridge for the actual
interpolation. No new third-party dependencies.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

import numpy as np
from pyramids.netcdf.ugrid.dataset import UgridDataset

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset


def from_orca(
    lon2d: np.ndarray,
    lat2d: np.ndarray,
    data2d: np.ndarray,
    *,
    cell_size: float,
    method: str = "nearest",
    epsg: int = 4326,
    nodata: float = -9999.0,
) -> Dataset:
    """Regrid an ORCA curvilinear field onto a regular-grid `Dataset`.

    The `(ny, nx)` coordinate arrays are treated as mesh nodes and stitched into
    `(ny - 1) * (nx - 1)` quadrilateral faces. Each face value is the NaN-aware mean
    of its four corner nodes (`numpy.nanmean`), so every value in `data2d` (including
    the last row and column) contributes; `NaN`-masked nodes are ignored and a face is
    `NaN` only when all four of its corners are `NaN`. Use `NaN` (not a finite
    sentinel) to mark missing input values. The resulting UGRID mesh is interpolated to
    a regular grid via `UgridDataset.to_dataset`.

    Args:
        lon2d: `(ny, nx)` array of node longitudes (x-coordinates).
        lat2d: `(ny, nx)` array of node latitudes (y-coordinates), same shape as
            `lon2d`.
        data2d: `(ny, nx)` array of field values aligned with the coordinate grid.
        cell_size: Output pixel size in the target CRS units.
        method: Interpolation method passed to `mesh_to_grid` (`"nearest"` or
            `"linear"`).
        epsg: Output EPSG code.
        nodata: No-data value stamped on cells the mesh does not cover.

    Returns:
        A single-band `pyramids.dataset.Dataset` of the interpolated surface.

    Raises:
        ValueError: `lon2d`, `lat2d` and `data2d` are not all the same 2-D shape, or
            the grid is smaller than `2 x 2` (no quad cells can be formed).

    Examples:
        - Regrid a small curvilinear field and inspect the raster it produces:
            ```python
            >>> import numpy as np
            >>> from earthlens.grids import from_orca
            >>> lon2d = np.array([[0.0, 1.0, 2.0], [0.0, 1.0, 2.0]])
            >>> lat2d = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]])
            >>> data2d = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
            >>> ds = from_orca(lon2d, lat2d, data2d, cell_size=0.5)
            >>> ds.band_count
            1
            >>> ds.epsg
            4326

            ```
        - Mismatched coordinate/data shapes are rejected:
            ```python
            >>> import numpy as np
            >>> from earthlens.grids import from_orca
            >>> try:
            ...     from_orca(np.zeros((2, 3)), np.zeros((2, 2)), np.zeros((2, 3)), cell_size=0.5)
            ... except ValueError as exc:
            ...     print("same shape" in str(exc))
            True

            ```

    See Also:
        - `from_octahedral`: regrid ragged per-point fields.
        - `pyramids.netcdf.ugrid.dataset.UgridDataset.to_dataset`: the mesh→raster
          bridge this adapter delegates to.
    """
    lon2d = np.asarray(lon2d, dtype=np.float64)
    lat2d = np.asarray(lat2d, dtype=np.float64)
    data2d = np.asarray(data2d, dtype=np.float64)
    if not (lon2d.shape == lat2d.shape == data2d.shape):
        raise ValueError(
            "lon2d, lat2d and data2d must share the same shape; got "
            f"{lon2d.shape}, {lat2d.shape}, {data2d.shape}."
        )
    if lon2d.ndim != 2:
        raise ValueError(f"ORCA inputs must be 2-D; got {lon2d.ndim}-D arrays.")
    ny, nx = lon2d.shape
    if ny < 2 or nx < 2:
        raise ValueError(
            f"ORCA grid must be at least 2 x 2 to form quad cells; got {ny} x {nx}."
        )

    node_x = lon2d.ravel()
    node_y = lat2d.ravel()
    idx = np.arange(ny * nx).reshape(ny, nx)
    faces = np.stack(
        [
            idx[:-1, :-1].ravel(),
            idx[:-1, 1:].ravel(),
            idx[1:, 1:].ravel(),
            idx[1:, :-1].ravel(),
        ],
        axis=1,
    )
    # Each face value is the NaN-aware mean of its four corner nodes, so every node in
    # data2d contributes (no last row/column dropped) and a NaN-masked node only blanks
    # a face when all four of its corners are NaN.
    corners = np.stack(
        [data2d[:-1, :-1], data2d[:-1, 1:], data2d[1:, 1:], data2d[1:, :-1]], axis=0
    )
    with warnings.catch_warnings():
        # All-NaN faces are intended (fully-masked cell -> NaN); suppress the warning.
        warnings.simplefilter("ignore", category=RuntimeWarning)
        face_values = np.nanmean(corners, axis=0)

    mesh = UgridDataset.from_arrays(
        node_x,
        node_y,
        faces,
        data={"z": face_values.ravel()},
        data_locations={"z": "face"},
        epsg=epsg,
    )
    result = mesh.to_dataset(
        "z", cell_size=cell_size, method=method, epsg=epsg, nodata=nodata
    )
    return result
