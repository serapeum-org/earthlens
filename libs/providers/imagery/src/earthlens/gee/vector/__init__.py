"""Vector interop for the GEE backend: geometry conversion and feature download.

This subpackage holds the object-oriented vector bridge the sampling family
builds on:

* `GeometryConverter` - Shapely / `GeoDataFrame` -> `ee.Geometry` /
  `ee.FeatureCollection`, configured once with an EPSG code and an optional
  column selection, and supporting `LineString` (which the free
  `earthlens.gee.features.create_geometry` still refuses).
* `FeatureDownloader` - pull an `ee.FeatureCollection` into pandas / a pyramids
  `FeatureCollection`, carrying the thread-pool and retry budget as fields.
* `VectorCodec` - read vector files (shp / geojson / gpkg / kml / parquet, and
  CSV point tables) into an `ee.FeatureCollection` and write one back out, every
  read and write going through a pyramids `FeatureCollection`.
* `to_numpy` / `to_netcdf` - bridge a downloaded `Dataset` to a numpy array and a
  `DatasetCollection` to NetCDF, both via pyramids (no `xee` / `xarray`).

The procedural `earthlens.gee.features.create_geometry` / `create_feature` and
`earthlens.gee.io` download helpers remain as they were; these classes are the
configurable, reusable form the `Sampler` family depends on.
"""

from __future__ import annotations

from earthlens.gee.vector.arrays import to_netcdf, to_numpy
from earthlens.gee.vector.codec import VectorCodec
from earthlens.gee.vector.download import FeatureDownloader, FeatureDownloaderConfig
from earthlens.gee.vector.geometry import GeometryConverter, GeometryConverterConfig

__all__ = [
    "FeatureDownloader",
    "FeatureDownloaderConfig",
    "GeometryConverter",
    "GeometryConverterConfig",
    "VectorCodec",
    "to_netcdf",
    "to_numpy",
]
