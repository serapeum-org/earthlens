"""Public entry point for earthlens.

`earthlens` itself is a PEP 420 namespace shared by the core and provider
distributions (so each ships its own `earthlens.<name>` subpackage without a
colliding `earthlens/__init__.py`). The user-facing surface therefore lives
here, in `earthlens.core`:

* :class:`EarthLens` — the facade. Importing it succeeds without any backend
  extras installed; each backend is imported lazily on first use through the
  registry.
* :class:`AggregationConfig`, :func:`aggregate_netcdf` and its streaming
  counterpart :func:`iter_aggregate_netcdf` (yielding
  :class:`AggregatedWindow`) — temporal
  aggregation (pure pyramids/numpy/pandas, no backend SDK).
* :func:`set_output_dir` / :func:`output_dir` — the process-wide directory
  every backend writes downloads to when no explicit `path=` is given (also
  settable via `EARTHLENS_DATA_DIR`), and :func:`set_cache_dir` /
  :func:`cache_dir` — the root each backend caches regenerable intermediates
  under (also settable via `EARTHLENS_CACHE`). See `earthlens.config`.

The concrete backends (`earthlens.ecmwf.ECMWF`, `earthlens.chc.CHIRPS`, …) are
intentionally **not** re-exported here — each needs its own optional SDK, so a
top-level re-export would crash at import time when the extra is absent. Reach
them via their submodules (`from earthlens.ecmwf import ECMWF`) or, more
typically, through the :class:`EarthLens` facade's `data_source=` argument.

`PolygonAoiWarning` is re-exported here too: it is issued when a polygon
`aoi=` is given to a backend that selects by bounding box only, and callers
who want that to be fatal can escalate it with
`warnings.simplefilter("error", PolygonAoiWarning)`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from earthlens._backends import AmbiguousDataSourceError
from earthlens.aggregate import (
    AggregatedWindow,
    AggregationConfig,
    aggregate_netcdf,
    iter_aggregate_netcdf,
)
from earthlens.base import PolygonAoiWarning
from earthlens.config import cache_dir, output_dir, set_cache_dir, set_output_dir
from earthlens.earthlens import EarthLens, download, find, search, sources

__all__ = [
    "AggregatedWindow",
    "AggregationConfig",
    "AmbiguousDataSourceError",
    "EarthLens",
    "PolygonAoiWarning",
    "aggregate_netcdf",
    "cache_dir",
    "download",
    "find",
    "iter_aggregate_netcdf",
    "output_dir",
    "search",
    "set_cache_dir",
    "set_output_dir",
    "sources",
]


try:
    __version__ = version("earthlens-core")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"
