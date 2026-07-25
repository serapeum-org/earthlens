"""Abstract base classes and shared value objects for every data source.

Public surface re-exported from this package so callers can write
`from earthlens.base import SpatialExtent` without reaching
into the private module layout.
"""

from __future__ import annotations

from earthlens.base._dates import (
    date_windows,
    resolve_cadence,
    split_time,
    to_datetime,
    window_labels,
)
from earthlens.base.abstractdatasource import (
    AbstractCatalog,
    AbstractDataSource,
    LazyClientMixin,
    OutputKind,
    PolygonAoiWarning,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.base.auth import AbstractAuth, AuthenticationError
from earthlens.base.http import HttpClient
from earthlens.base.leaves import FluxableLeaf
from earthlens.base.naming import safe_filename
from earthlens.base.providers import Provider, clear_providers_cache, load_providers
from earthlens.base.raster import close_quietly
from earthlens.base.region import (
    clear_region_cache,
    region_affinity,
    warn_if_egress,
)
from earthlens.base.s3 import S3Auth, S3Credentials
from earthlens.base.spatial import (
    METRES_PER_DEGREE,
    crop_to_aoi,
    estimate_pixel_dims,
    mask_to_geometry,
    normalize_aoi,
    resolve_aoi,
)

__all__ = [
    "AbstractAuth",
    "AbstractCatalog",
    "AbstractDataSource",
    "AuthenticationError",
    "FluxableLeaf",
    "HttpClient",
    "S3Auth",
    "S3Credentials",
    "LazyClientMixin",
    "METRES_PER_DEGREE",
    "OutputKind",
    "PolygonAoiWarning",
    "Provider",
    "RemoteProduct",
    "SpatialExtent",
    "TemporalExtent",
    "clear_providers_cache",
    "close_quietly",
    "clear_region_cache",
    "crop_to_aoi",
    "date_windows",
    "mask_to_geometry",
    "estimate_pixel_dims",
    "load_providers",
    "normalize_aoi",
    "region_affinity",
    "resolve_aoi",
    "resolve_cadence",
    "safe_filename",
    "split_time",
    "to_datetime",
    "warn_if_egress",
    "window_labels",
]
