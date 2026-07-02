"""Abstract base classes and shared value objects for every data source.

Public surface re-exported from this package so callers can write
`from earthlens.base import SpatialExtent` without reaching
into the private module layout.
"""

from __future__ import annotations

from earthlens.base._dates import split_time, to_datetime
from earthlens.base.abstractdatasource import (
    AbstractCatalog,
    AbstractDataSource,
    LazyClientMixin,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.base.auth import AbstractAuth, AuthenticationError
from earthlens.base.http import HttpClient
from earthlens.base.leaves import FluxableLeaf
from earthlens.base.providers import Provider, clear_providers_cache, load_providers
from earthlens.base.region import region_affinity
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
    "LazyClientMixin",
    "METRES_PER_DEGREE",
    "OutputKind",
    "Provider",
    "RemoteProduct",
    "SpatialExtent",
    "TemporalExtent",
    "clear_providers_cache",
    "crop_to_aoi",
    "mask_to_geometry",
    "estimate_pixel_dims",
    "load_providers",
    "normalize_aoi",
    "region_affinity",
    "resolve_aoi",
    "split_time",
    "to_datetime",
]
