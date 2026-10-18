"""Bridge a downloaded `ee.Image` to numpy, and a stack to NetCDF, via pyramids.

Two terminal conversions, both thin wrappers so the heavier machinery stays in
pyramids:

* `to_numpy` reads an already-downloaded pyramids `Dataset` into a numpy array
  (`Dataset.read_array`), feeding the client-side zonal strategy and the Epic 2
  raster operations.
* `to_netcdf` writes a pyramids `DatasetCollection` (a multi-band / time stack)
  to NetCDF through `DatasetCollection.to_netcdf`.

The policy is firm: NetCDF is produced only through pyramids, so earthlens
imports neither `xee` nor `xarray` here (`DatasetCollection.to_netcdf` owns every
xarray/netCDF call). NetCDF is a terminal export requested explicitly, never
produced on the common raster path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from pyramids.dataset import Dataset, DatasetCollection

__all__ = ["to_netcdf", "to_numpy"]


def to_numpy(dataset: Dataset, band: int | str = 0) -> np.ndarray:
    """Read one band of a downloaded `Dataset` into a numpy array.

    Args:
        dataset: An in-memory pyramids `Dataset` (e.g. from a materialised
            download).
        band: The band to read, by index or name. Defaults to `0`.

    Returns:
        The band's values as a numpy array.

    Examples:
        - A one-band raster reads back to its array:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> from earthlens.gee.vector.arrays import to_numpy
            >>> ds = Dataset.from_array(
            ...     arr=np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=None,
            ... )
            >>> to_numpy(ds).tolist()
            [[1.0, 2.0], [3.0, 4.0]]

            ```
    """
    return cast("np.ndarray", dataset.read_array(band=band))


def to_netcdf(collection: DatasetCollection, path: str | Path) -> Path:
    """Write a `DatasetCollection` to NetCDF, the one sanctioned route.

    `DatasetCollection.to_netcdf` owns every xarray/netCDF call, so earthlens
    imports neither `xee` nor `xarray` itself.

    Args:
        collection: The pyramids `DatasetCollection` (a multi-band / time stack).
        path: The output NetCDF path.

    Returns:
        The output path as a `Path`.
    """
    collection.to_netcdf(path)
    return Path(path)
