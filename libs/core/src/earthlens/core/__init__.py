"""Public entry point for earthlens.

`earthlens` itself is a PEP 420 namespace shared by the core and provider
distributions (so each ships its own `earthlens.<name>` subpackage without a
colliding `earthlens/__init__.py`). The user-facing surface therefore lives
here, in `earthlens.core`:

* :class:`EarthLens` — the facade. Importing it succeeds without any backend
  extras installed; each backend is imported lazily on first use through the
  registry.
* :class:`AggregationConfig` and :func:`aggregate_netcdf` — temporal
  aggregation (pure pyramids/numpy/pandas, no backend SDK).

The concrete backends (`earthlens.ecmwf.ECMWF`, `earthlens.chc.CHIRPS`, …) are
intentionally **not** re-exported here — each needs its own optional SDK, so a
top-level re-export would crash at import time when the extra is absent. Reach
them via their submodules (`from earthlens.ecmwf import ECMWF`) or, more
typically, through the :class:`EarthLens` facade's `data_source=` argument.
"""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError  # type: ignore
    from importlib.metadata import version
except ImportError:  # pragma: no cover
    from importlib_metadata import PackageNotFoundError  # type: ignore
    from importlib_metadata import version

from earthlens.aggregate import AggregationConfig, aggregate_netcdf
from earthlens.earthlens import EarthLens, download, find, search, sources

__all__ = [
    "AggregationConfig",
    "EarthLens",
    "aggregate_netcdf",
    "download",
    "find",
    "search",
    "sources",
]


try:
    __version__ = version("earthlens-core")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"
