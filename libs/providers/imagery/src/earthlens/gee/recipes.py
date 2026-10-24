"""`preprocess`: the catalog's analysis-ready recipe, in one call.

After the mask library and `BandScaler` the pieces all exist, but using
them still means knowing which mask belongs to which sensor, that
scaling is separate, and in which order the two apply. That assembly is
exactly what gets asked about — "does the GEE backend already include
Sentinel-2 preprocessing?" — and `preprocess` is the single call that
answers yes:

```python
el = EarthLens(
    data_source="gee",
    dataset="COPERNICUS/S2_SR_HARMONIZED",
    variables=["B4", "B3", "B2"],
    start="2024-05-01", end="2024-06-15", reducer="median",
    preprocess=True,
)
```

`GEE(preprocess=True)` is the shipped entry point, and it resolves
*through this function*: the backend holds the asset id and the requested
bands already, so the flag passes them in rather than making the caller
restate `dataset=` and `variables=` — a duplication nothing could
cross-check, where a narrower band list silently left the other requested
bands in raw DN inside the same raster. The function stays public for a
recipe you want to inspect, extend (`preprocess(...) >> SpeckleFilter()`)
or hand around, and when it is passed to `cloud_mask=` the backend
cross-checks it against the request: a mismatched asset id raises, an
uncovered scalable band warns.

The recipe is **catalog data, not an `if/elif` over asset ids**: a
`preprocess:` block on the curated dataset row names the mask and says
whether to scale, typed as `earthlens.gee.catalog.Preprocess` so the
loader validates it. That typing is the point — `Dataset` is frozen but
does not forbid extra keys, so an undeclared `preprocess:` key would
have been dropped in silence and a typo'd recipe would have read as "no
recipe". This module owns only the name-to-class map, `_MASK_RECIPES`.

Order is part of the recipe and not incidental: **mask first, then
scale**, so the mask reads its raw quality band before any arithmetic
touches the pixels. The result is a `MaskChain`, itself an `ImageMask`,
so it rides the backend's single `cloud_mask=` slot and can be extended
(`preprocess("COPERNICUS/S1_GRD") >> SpeckleFilter()`) or replaced
outright.

A dataset with no curated recipe raises rather than returning an empty
chain: `preprocess` is never a silent no-op. Sentinel-1 is the
instructive case where the recipe *is* a no-op — `COPERNICUS/S1_GRD`
already arrives thermal-noise-removed, radiometrically calibrated and
terrain-corrected to dB, so the row carries a `note` saying so and
naming speckle filtering as the one remaining gap. `SpeckleFilter`
covers that gap with `ee.Image.focalMedian`, because Epic 2's
`FocalMedian` is client-side over an already-downloaded raster and
cannot run before a composite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from earthlens.gee.catalog import Catalog
from earthlens.gee.cloud_masks import (
    BandScaler,
    CloudScorePlus,
    ImageMask,
    LandsatQaRefinedMask,
    LandsatSrMask,
    MaskChain,
    ModisState1kmMask,
    PassThrough,
    Sentinel2Cdi,
    Sentinel2Qa60Mask,
    Sentinel2SclMask,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

__all__ = ["preprocess"]

#: Recipe name → the mask it builds. The keys are exactly the members of
#: `earthlens.gee.catalog.MaskRecipe`, which is what the catalog YAML is
#: validated against, so a name can never be added to one side alone.
_MASK_RECIPES: dict[str, type[ImageMask]] = {
    "landsat_sr": LandsatSrMask,
    "landsat_qa_refined": LandsatQaRefinedMask,
    "sentinel2_scl": Sentinel2SclMask,
    "sentinel2_qa60": Sentinel2Qa60Mask,
    "sentinel2_cdi": Sentinel2Cdi,
    "cloud_score_plus": CloudScorePlus,
    "modis_state_1km": ModisState1kmMask,
}

#: Recipe name → the bands its mask reads off the scene by name. Curating a
#: recipe onto a row that does not declare these would surface only as an
#: opaque server-side graph failure on a live request, so the invariant is
#: asserted over the whole curated catalog. Keyed by the same names as
#: `_MASK_RECIPES`, which a test pins, so neither map can gain a name alone.
#:
#: `cloud_score_plus` is empty on purpose: its `cs` / `cs_cdf` band comes from
#: the linked Cloud Score+ collection, not from the row. `sentinel2_cdi` names
#: the three bands `ee.Algorithms.Sentinel2.CDI` reads for its parallax
#: computation rather than a quality band.
_MASK_REQUIRED_BANDS: dict[str, frozenset[str]] = {
    "landsat_sr": frozenset({"QA_PIXEL"}),
    "landsat_qa_refined": frozenset({"QA_PIXEL"}),
    "sentinel2_scl": frozenset({"SCL"}),
    "sentinel2_qa60": frozenset({"QA60"}),
    "sentinel2_cdi": frozenset({"B7", "B8", "B8A"}),
    "cloud_score_plus": frozenset(),
    "modis_state_1km": frozenset({"state_1km"}),
}


def preprocess(
    dataset: str,
    bands: Iterable[str] | None = None,
    catalog: Catalog | None = None,
) -> MaskChain:
    """Build a dataset's analysis-ready mask chain from the catalog.

    Resolves the `preprocess:` recipe on the catalog row into a
    `MaskChain` of the sensor's recommended mask followed, when the
    recipe asks for it, by a `BandScaler` over the row's own `scale` /
    `offset` factors. Hand the result to `GEE(cloud_mask=...)`.

    The chain forces the request onto an Earth Engine engine, as any
    `cloud_mask` does, so the EEDAI reader declines it automatically.

    Args:
        dataset: The Earth Engine asset id to look the recipe up for.
        bands: The band ids the scaler should cover, usually the
            requested ones. Defaults to every band on the row.
        catalog: The catalog to read. Defaults to a fresh `Catalog()`,
            whose parse is cached per `(path, mtime)`, so repeated calls
            do not re-read the YAML.

    Returns:
        The recipe's `MaskChain`, in mask-then-scale order. A recipe
        with nothing to apply yields a one-element chain of
        `PassThrough`, so the return type never depends on the recipe.

    Raises:
        ValueError: If the row carries no `preprocess:` recipe —
            `preprocess` reports rather than silently doing nothing.

    Examples:
        - Sentinel-2 L2A is masked on its scene classification, then
          scaled to reflectance:
            ```python
            >>> chain = preprocess("COPERNICUS/S2_SR_HARMONIZED", ["B4", "B3"])
            >>> chain.name
            'Sentinel2SclMask >> BandScaler'
            >>> [f.band for f in chain.masks[1].factors]
            ['B4', 'B3']

            ```
        - Landsat C2-L2 carries both halves of the conversion:
            ```python
            >>> chain = preprocess("LANDSAT/LC08/C02/T1_L2", ["SR_B4"])
            >>> chain.name
            'LandsatSrMask >> BandScaler'
            >>> chain.masks[1].factors[0].offset
            -0.2

            ```
        - Sentinel-1 needs neither, so the recipe is a documented
          pass-through that the caller extends:
            ```python
            >>> preprocess("COPERNICUS/S1_GRD").name
            'PassThrough'

            ```
        - A dataset with no curated recipe says so:
            ```python
            >>> preprocess("USGS/SRTMGL1_003")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'USGS/SRTMGL1_003' has no curated preprocessing recipe...

            ```
    """
    resolved = catalog if catalog is not None else Catalog()
    row = resolved.get_dataset(dataset)
    recipe = row.preprocess
    if recipe is None:
        raise ValueError(
            f"{dataset!r} has no curated preprocessing recipe. Compose one "
            "from the earthlens.gee.cloud_masks library (e.g. "
            "`Sentinel2SclMask() >> BandScaler.for_dataset(row)`), or add a "
            "`preprocess:` block to the catalog row."
        )
    if recipe.note:
        logger.info(f"{dataset!r} preprocessing: {recipe.note}")
    steps: list[ImageMask] = []
    if recipe.mask is not None:
        steps.append(_MASK_RECIPES[recipe.mask]())
    if recipe.scale:
        steps.append(BandScaler.for_dataset(row, bands))
    if not steps:
        steps.append(PassThrough())
    return MaskChain(masks=tuple(steps))
