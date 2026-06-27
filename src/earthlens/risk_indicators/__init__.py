"""Country/admin-indexed risk-indicator backend (`earthlens.risk_indicators`).

One mixed backend over three public/keyed risk sources queried by country (ISO3)
or admin code:

* **ThinkHazard!** (GFDRR) — hazard screening across 11 hazards, by admin
  division; public, no auth; `tabular`.
* **INFORM Risk** (JRC) — composite country-risk index (+ a climate variant);
  public, no auth; `tabular`.
* **Global Forest Watch** Data API — forest indicators (tree-cover loss) by
  country, and the GADM admin geometry; needs an `x-api-key`; `tabular` /
  `vector`.

Output is **per instance**: a dataset's `output_kind` decides whether
:meth:`~earthlens.risk_indicators.backend.RiskIndicators.download` returns a
:class:`pandas.DataFrame` (`tabular`) or a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` (`vector`). Auth is
**per source** — only a `gfw` dataset builds a :class:`GfwAuth`; ThinkHazard and
INFORM stay keyless. Every parse is REST JSON -> pandas / FeatureCollection, with
no gridded-array dependency in this subpackage.

The public surface is the :class:`Catalog` (dataset id -> provider + output kind
+ request detail, plus an ISO3 -> ThinkHazard ADM0 code lookup) and its
:class:`Dataset` rows, the GFW-only :class:`GfwAuth` / :class:`GfwCredentials`,
and the pure query/parse helpers.
"""

from __future__ import annotations

from earthlens.risk_indicators._helpers import (
    empty_canonical,
    gfw_geostore,
    gfw_query,
    inform_query,
    inform_to_frame,
    resolve_admin,
    thinkhazard_query,
    thinkhazard_to_frame,
    to_feature_collection,
    to_frame,
)
from earthlens.risk_indicators.auth import (
    AuthenticationError,
    GfwAuth,
    GfwCredentials,
)
from earthlens.risk_indicators.backend import RiskIndicators
from earthlens.risk_indicators.catalog import Catalog, Dataset

__all__ = [
    "RiskIndicators",
    "Catalog",
    "Dataset",
    "GfwAuth",
    "GfwCredentials",
    "AuthenticationError",
    "gfw_query",
    "gfw_geostore",
    "thinkhazard_query",
    "thinkhazard_to_frame",
    "inform_query",
    "inform_to_frame",
    "to_frame",
    "to_feature_collection",
    "resolve_admin",
    "empty_canonical",
]
