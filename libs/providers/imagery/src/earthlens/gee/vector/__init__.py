"""Vector interop for the GEE backend: geometry conversion and feature download.

This subpackage holds the object-oriented vector bridge the sampling family
builds on:

* `GeometryConverter` - Shapely / `GeoDataFrame` -> `ee.Geometry` /
  `ee.FeatureCollection`, configured once with an EPSG code and an optional
  column selection, and supporting `LineString` (which the free
  `earthlens.gee.features.create_geometry` still refuses).

The procedural `earthlens.gee.features.create_geometry` / `create_feature` and
`earthlens.gee.io` download helpers remain as they were; these classes are the
configurable, reusable form the `Sampler` family depends on.
"""

from __future__ import annotations

from earthlens.gee.vector.geometry import GeometryConverter, GeometryConverterConfig

__all__ = [
    "GeometryConverter",
    "GeometryConverterConfig",
]
