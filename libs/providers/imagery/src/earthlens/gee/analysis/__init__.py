"""Client-side raster analysis over a downloaded pyramids `Dataset`.

Every operation here is a callable value object with one shape: it takes a
`pyramids.dataset.Dataset` and returns a new one. Nothing in this subpackage
builds, maps over or composites an `ee.Image`, so none of it reaches Earth
Engine.

That matters for routing. The backend's eligibility gate refuses the EEDAI raw
read when a request carries server-side shaping (`cloud_mask=`, `filters=`, a
reducer), because those change the pixels Earth Engine would serve. These
operations run *after* the fetch, on a `Dataset` the reader has already
produced, so they are invisible to that gate: a raw EEDAI download stays
eligible and the analysis happens locally.

The intended shape of a call is therefore download, then analyse:

```python
>>> from earthlens.core import EarthLens
>>> el = EarthLens(data_source="gee", materialize=True)  # doctest: +SKIP
>>> result = el.download(progress_bar=False)[0]  # doctest: +SKIP
>>> ndvi = Ndvi(nir=0, red=1)(result.objects[0])  # doctest: +SKIP
```

Operations compose with `>>`, which builds an `OperationChain` - itself an
operation, so a chain is accepted anywhere a single one is. `RasterAnalysis`
adds the one thing the operations do not know about: how to take the in-memory
raster out of Epic 1's `DownloadResult` and, optionally, write what comes back.

The families:

* `NormalizedDifference` and its presets `Ndvi` / `Ndwi` / `Ndbi` - two-band
  index arithmetic.
* `BandExpression` - the general case, a formula over named bands compiled from
  an allow-list with no `eval`.
* `Slope` / `Aspect` / `Hillshade` - terrain derivatives of an elevation band.
* `FocalMean` / `FocalStd` / `FocalMedian` / `FocalApply` - neighbourhood
  statistics over a square window.
* `Texture` - grey-level co-occurrence measures, built on the focal window.
* `Proximity` - distance from every cell to the nearest target cell.
* `Reclassify` / `ConvertUnits` - rewriting values into classes, or into
  another physical unit.
* `ConnectedComponents` / `Sieve` - labelling contiguous patches and dropping
  the ones too small to keep.
* `Cluster` / `Classify` with `TrainingExtractor` - unsupervised and supervised
  per-pixel learning, both through scikit-learn.
"""

from __future__ import annotations

from earthlens.gee.analysis.base import OperationChain, RasterOperation
from earthlens.gee.analysis.classify import Classify, TrainingExtractor, TrainingSet
from earthlens.gee.analysis.cluster import Cluster
from earthlens.gee.analysis.expression import ALLOWED_FUNCTIONS, BandExpression
from earthlens.gee.analysis.facade import RasterAnalysis
from earthlens.gee.analysis.focal import (
    FocalApply,
    FocalMean,
    FocalMedian,
    FocalStd,
)
from earthlens.gee.analysis.indices import Ndbi, Ndvi, Ndwi, NormalizedDifference
from earthlens.gee.analysis.patches import ConnectedComponents, Sieve
from earthlens.gee.analysis.proximity import Proximity
from earthlens.gee.analysis.reclassify import ConvertUnits, Reclassify
from earthlens.gee.analysis.terrain import Aspect, Hillshade, Slope
from earthlens.gee.analysis.texture import MEASURES, Texture

__all__ = [
    "ALLOWED_FUNCTIONS",
    "MEASURES",
    "Aspect",
    "BandExpression",
    "Classify",
    "Cluster",
    "ConnectedComponents",
    "ConvertUnits",
    "FocalApply",
    "FocalMean",
    "FocalMedian",
    "FocalStd",
    "Hillshade",
    "Ndbi",
    "Ndvi",
    "Ndwi",
    "NormalizedDifference",
    "OperationChain",
    "Proximity",
    "RasterAnalysis",
    "RasterOperation",
    "Reclassify",
    "Sieve",
    "Slope",
    "Texture",
    "TrainingExtractor",
    "TrainingSet",
]
