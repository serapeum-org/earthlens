"""Adapters that turn exotic model grids into regular-grid `pyramids.dataset.Dataset`.

Weather/ocean/EO models emit fields on grids that are not row-major rasters: ORCA
curvilinear ocean grids, octahedral reduced-Gaussian grids, HEALPix sphere
pixelizations, and others. pyramids owns two *generic* regridding bridges — a mesh
path (`pyramids.netcdf.ugrid.interpolation.mesh_to_grid`) and a scattered-point path
(`pyramids.dataset.ops.interpolate.grid_points`). The adapters here encode only the
domain-specific *grid recognition*; they turn each exotic grid into a mesh or point
set and reuse those generic bridges, so they do not implement a new regridder.

- `from_orca` — curvilinear `(ny, nx)` lon/lat → UGRID quad mesh → raster.
- `from_octahedral` — ragged per-point lat/lon → scattered points → raster.
- `from_healpix` — HEALPix pixels (RING or NESTED) → scattered points → raster.
  The pixel→lon/lat math is implemented in plain NumPy, so no `healpy` dependency is
  required.
"""

from __future__ import annotations

from earthlens.grids.healpix import from_healpix
from earthlens.grids.octahedral import from_octahedral
from earthlens.grids.orca import from_orca

__all__ = [
    "from_healpix",
    "from_octahedral",
    "from_orca",
]
