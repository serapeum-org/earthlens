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
    end_is_date_only,
    expand_bare_date_end,
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
from earthlens.base.auth import (
    AbstractAuth,
    AuthenticationError,
    SingleSecretAuth,
)
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
    bbox_overlaps,
    crop_to_aoi,
    ensure_no_data,
    estimate_pixel_dims,
    mask_to_geometry,
    normalize_aoi,
    resolve_aoi,
    vsicurl_config,
    widen_degenerate_bbox,
    windowed_bbox_crop,
)
from earthlens.base.upstream import (
    UpstreamUnavailableError,
    exception_chain,
    http_status,
    is_http_status,
    response_status,
    status_in_message,
)

__all__ = [
    "AbstractAuth",
    "AbstractCatalog",
    "AbstractDataSource",
    "AuthenticationError",
    "CADENCE_ALIASES",
    "FluxableLeaf",
    "HttpClient",
    "HttpRangeFile",
    "LazyClientMixin",
    "METRES_PER_DEGREE",
    "OutputKind",
    "PolygonAoiWarning",
    "Provider",
    "RangeReadError",
    "RemoteProduct",
    "S3Auth",
    "S3Credentials",
    "SpatialExtent",
    "TemporalExtent",
    "Timeout",
    "UpstreamUnavailableError",
    "WHOLE_WINDOW",
    "aoi_tag",
    "bbox_overlaps",
    "catalog_cache_key",
    "clear_all_catalog_caches",
    "clear_providers_cache",
    "clear_region_cache",
    "close_quietly",
    "crop_to_aoi",
    "date_windows",
    "end_is_date_only",
    "ensure_no_data",
    "estimate_pixel_dims",
    "exception_chain",
    "expand_bare_date_end",
    "http_status",
    "is_http_status",
    "is_network_unreachable",
    "load_catalog",
    "load_providers",
    "mask_to_geometry",
    "normalize_aoi",
    "prefer_ipv4",
    "redact_url",
    "region_affinity",
    "resolve_aoi",
    "resolve_cadence",
    "response_status",
    "retry_login_forcing_ipv4",
    "safe_filename",
    "status_in_message",
    "SingleSecretAuth",
    "sidecar_is_fresh",
    "sidecar_path",
    "split_time",
    "to_datetime",
    "vsicurl_config",
    "warn_if_egress",
    "widen_degenerate_bbox",
    "window_labels",
    "windowed_bbox_crop",
    "write_sidecar",
    "yaml_files_for",
]
