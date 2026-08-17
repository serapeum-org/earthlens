"""Abstract base classes and shared value objects for every data source.

Public surface re-exported from this package so callers can write
`from earthlens.base import SpatialExtent` without reaching
into the private module layout.
"""

from __future__ import annotations

from earthlens.base._dates import (
    CADENCE_ALIASES,
    WHOLE_WINDOW,
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
from earthlens.base.cache import (
    aoi_tag,
    sidecar_is_fresh,
    sidecar_path,
    write_sidecar,
)
from earthlens.base.catalog_source import (
    catalog_cache_key,
    clear_all_catalog_caches,
    load_catalog,
    yaml_files_for,
)
from earthlens.base.http import (
    HttpClient,
    HttpRangeFile,
    RangeReadError,
    Timeout,
    is_network_unreachable,
    prefer_ipv4,
    redact_url,
    retry_login_forcing_ipv4,
)
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
    "aoi_tag",
    "AuthenticationError",
    "CADENCE_ALIASES",
    "catalog_cache_key",
    "clear_all_catalog_caches",
    "clear_providers_cache",
    "clear_region_cache",
    "close_quietly",
    "crop_to_aoi",
    "date_windows",
    "estimate_pixel_dims",
    "FluxableLeaf",
    "HttpClient",
    "HttpRangeFile",
    "is_network_unreachable",
    "LazyClientMixin",
    "load_catalog",
    "load_providers",
    "mask_to_geometry",
    "METRES_PER_DEGREE",
    "normalize_aoi",
    "OutputKind",
    "PolygonAoiWarning",
    "prefer_ipv4",
    "Provider",
    "RangeReadError",
    "redact_url",
    "region_affinity",
    "RemoteProduct",
    "resolve_aoi",
    "resolve_cadence",
    "retry_login_forcing_ipv4",
    "S3Auth",
    "S3Credentials",
    "safe_filename",
    "sidecar_is_fresh",
    "sidecar_path",
    "SpatialExtent",
    "split_time",
    "TemporalExtent",
    "Timeout",
    "to_datetime",
    "warn_if_egress",
    "WHOLE_WINDOW",
    "window_labels",
    "write_sidecar",
    "yaml_files_for",
]
