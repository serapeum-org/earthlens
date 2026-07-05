"""CMIP6 climate-projections backend (Pangeo ARCO mirror on `gs://cmip6`).

Exposes the **raw, full CMIP6 archive** — every ScenarioMIP / CMIP experiment,
every ESM, on its native grid — as analysis-ready cloud Zarr on the open Pangeo
Google Cloud mirror (`gs://cmip6`), indexed by a plain consolidated-stores CSV
(no auth). This is the whole `model x scenario x variable x member` matrix, not
the single pre-downscaled product the `gee` backend exposes (`NASA/GDDP-CMIP6`)
nor the CHC-CMIP6 precipitation deltas the `chc` backend exposes.

A request is a CMIP6 *facet tuple* — `source_id` (model), `experiment_id`
(scenario), `variable_id`, `table_id` (+ optional `member_id` / `grid_label` /
`version`) — which the resolver maps to the matching `zstore` (`gs://cmip6/...`)
URI(s). The backend is **file-writing**: `download()` has pyramids open the Zarr
and write a bbox/time NetCDF subset, returning the `list[Path]`. earthlens never
imports `xarray` / `zarr` / `gcsfs` — pyramids owns the read (via GDAL's `/vsigs/`
multidim driver, read anonymously; no gcsfs needed).

Public surface (re-exported from this package):

* :class:`Catalog` — loader for the bundled `cmip6_data_catalog.yaml` (config +
  curated vocabulary).
* :class:`Cmip6Variable` / :class:`Experiment` / :class:`Table` / :class:`Source`
  — one curated variable / experiment / table / source row.
* :data:`CATALOG_PATH` — path to the bundled YAML; monkey-patchable in tests.
* :func:`clear_catalog_cache` — empty the catalog parse cache.
* :class:`StoreResolver` / :class:`ResolvedStore` — facet -> `zstore` resolution
  over the consolidated-stores CSV.

Examples:
    - Resolve a curated variable's metadata:
        ```python
        >>> from earthlens.cmip6 import Catalog
        >>> Catalog().get_dataset("tas").long_name
        'Near-surface (2 m) air temperature'

        ```
"""

from __future__ import annotations

from earthlens.cmip6.catalog import (
    CATALOG_PATH,
    Catalog,
    Cmip6Variable,
    Experiment,
    Source,
    Table,
    clear_catalog_cache,
)
from earthlens.cmip6.resolver import ResolvedStore, StoreResolver

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "Cmip6Variable",
    "Experiment",
    "ResolvedStore",
    "Source",
    "StoreResolver",
    "Table",
    "clear_catalog_cache",
]
