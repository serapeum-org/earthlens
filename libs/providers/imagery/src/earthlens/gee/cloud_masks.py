"""Cloud-masking helpers for Earth Engine Landsat / Sentinel collections.

Each mask is a per-image `ee.Image -> ee.Image` transform, suitable for
`ee.ImageCollection.map(...)` before a reducer — or for the `GEE`
backend's `cloud_mask=` hook, which `.map`-applies it to the stack
before compositing, so a cloud-screened median mosaic can be expressed
through the facade instead of raw `ee`.

Masks come in two equivalent forms:

* **Callable classes** under the `ImageMask` base — `LandsatSrMask`,
  `LandsatQaRefinedMask`, `Sentinel2SclMask`, `Sentinel2Qa60Mask`,
  `ModisState1kmMask` and `CloudScorePlus` — frozen pydantic objects
  that carry validated config and are drop-ins for `cloud_mask=` (the
  hook only requires a callable). A typo'd keyword or an out-of-range
  value raises at construction instead of silently no-op'ing the way a
  misspelled `functools.partial` keyword would.
* **Module functions** — `landsat_sr(image, sensor="auto")` and
  `sentinel2_scl(image)` — the original public surface, kept as thin
  behaviour-identical wrappers that construct the class and call it.

`landsat_sr` / `LandsatSrMask` mask an LS7 / LS8 / LS9 Collection-2
Level-2 surface-reflectance image to its `QA_PIXEL` Clear bit (bit 6).
The Landsat C2-L2 `QA_PIXEL` bitmask is identical across LS7 / LS8 /
LS9, so the `sensor` argument is currently informational — kept on the
signature so callers can be explicit and so future per-sensor
refinements (e.g. extra confidence-bit thresholds) don't break the
public API.

`sentinel2_scl` / `Sentinel2SclMask` mask a Sentinel-2 L2A image via its
Scene Classification (`SCL`) band, dropping cloud shadow (class 3),
cloud medium / high probability (8 / 9) and thin cirrus (10).

`CloudScorePlus` masks a Sentinel-2 scene with Google's Cloud Score+
dataset (`GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED`): it links the
scene's `cs` / `cs_cdf` quality band by `system:index` and keeps pixels
at or above a `[0, 1]` threshold.

The expanded library (Task 6.4) broadens coverage across the catalog's
optical datasets:

* `Sentinel2Qa60Mask` — the standard pre-Scene-Classification recipe:
  masks the `QA60` opaque-cloud bit (10) and cirrus bit (11), for TOA
  scenes (`COPERNICUS/S2_HARMONIZED`) that carry `QA60` but no `SCL`.
* `LandsatQaRefinedMask` — gives the `sensor` argument teeth: reads the
  `QA_PIXEL` confidence bit-pairs (cloud at bits 8-9, cloud shadow at
  bits 10-11) and keeps pixels at or below the configured confidence
  ceilings, instead of trusting the single derived Clear bit.
* `ModisState1kmMask` — the daily MODIS surface-reflectance products
  (`MODIS/061/MOD09GA` / `MYD09GA`): keeps only pixels whose 2-bit
  cloud-state field (bits 0-1 of `state_1km`) reads "clear" (0).

Each mask reads a quality band the target dataset must carry; the
curated catalog declares `QA60` and `state_1km` on the datasets named
above, so a facade request against those rows has the band. On a
dataset without it, Earth Engine fails at graph-compute time — the
documented contract for a mask reading an absent band.

Two members of the family are not masks at all, but the same shape —
per-image `ee.Image -> ee.Image` ops, and so drop-ins for the same hook:

* `BandScaler` (with `BandFactor`) converts raw DN to physical units
  using the `scale` / `offset` the catalog already declares per band,
  server-side and before the composite. `BandScaler.for_dataset(row)`
  resolves the factors from a catalog row.
* `SpeckleFilter` runs `ee.Image.focalMedian` on the graph — the
  server-side counterpart of the client-side `analysis.FocalMedian`,
  and the remaining Sentinel-1 preprocessing step.

Because the backend has a **single** `cloud_mask` slot, the ops compose
into a `MaskChain` — itself an `ImageMask` — either with `>>`
(`Sentinel2SclMask() >> BandScaler(...)`) or via `MaskChain.of(...)`,
which also accepts the plain-function forms. Order matters and is part
of the recipe: a cloud mask must read its raw quality band before a
scaler's arithmetic touches the pixels. `earthlens.gee.preprocess`
assembles that chain per dataset from the catalog.

Every op here preserves the image's properties. For the `updateMask`
masks that is automatic; the ops that rebuild the image copy them back
with `copyProperties(source, source.propertyNames())`, because
`CompositeEngine.composite_ee` applies its per-bucket `filterDate`
*after* the hook and the reducers and samplers read
`system:time_start` — dropping it would empty every bucket.

`Sentinel2Cdi` masks a Sentinel-2 **TOA** scene
(`COPERNICUS/S2_HARMONIZED`) by its Cloud Displacement Index —
`ee.Algorithms.Sentinel2.CDI(image)`, a parallax-based cloud
discriminator — keeping pixels whose CDI is at or above a `[-1, 1]`
threshold (clouds push the CDI strongly negative). It is also the
per-image half of `earthlens.gee.reducers.Sentinel2Composite`.

Bit layout (USGS LSDS-1330):

```
Bit 0: Fill
Bit 1: Dilated Cloud
Bit 2: Cirrus              (LS8/LS9 only — set to 0 on LS7)
Bit 3: Cloud
Bit 4: Cloud Shadow
Bit 5: Snow
Bit 6: Clear               (set when bits 1,3,4 are all clear)
Bit 7: Water
Bit 8-9:   Cloud Confidence
Bit 10-11: Cloud Shadow Confidence
Bit 12-13: Snow/Ice Confidence
Bit 14-15: Cirrus Confidence
```

Ported from `gee_utils.raster.cloud_mask_ls7_sr`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

import ee
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:  # pragma: no cover - import cycle / cost avoidance only
    from collections.abc import Iterable

    from earthlens.gee.catalog import Dataset

__all__ = [
    "BandFactor",
    "BandScaler",
    "CloudScorePlus",
    "ImageMask",
    "LandsatQaRefinedMask",
    "LandsatSrMask",
    "MaskChain",
    "ModisState1kmMask",
    "PassThrough",
    "Sensor",
    "Sentinel2Cdi",
    "Sentinel2Qa60Mask",
    "Sentinel2SclMask",
    "SpeckleFilter",
    "landsat_sr",
    "sentinel2_scl",
]

#: A per-image Earth Engine op: what the `cloud_mask=` hook accepts, and so
#: what a `MaskChain` links together — every `ImageMask` is one, and so are
#: the module-function forms (`landsat_sr` / `sentinel2_scl`).
PerImageOp = Callable[[ee.Image], ee.Image]

# Bit 6 of QA_PIXEL: the "Clear" bit. Set when the pixel is neither
# Dilated Cloud (bit 1), Cloud (bit 3), nor Cloud Shadow (bit 4).
_QA_PIXEL_CLEAR_BIT: int = 1 << 6

Sensor = Literal["LS7", "LS8", "LS9", "auto"]
_SUPPORTED_SENSORS: frozenset[str] = frozenset({"LS7", "LS8", "LS9", "auto"})

#: Sentinel-2 L2A Scene Classification (`SCL`) codes dropped by
#: `sentinel2_scl`: cloud shadow (3), cloud medium probability (8),
#: cloud high probability (9) and thin cirrus (10). Every other class
#: (vegetation, bare soil, water, snow/ice, ...) is kept.
_S2_SCL_MASKED_CLASSES: tuple[int, ...] = (3, 8, 9, 10)

#: Google's Cloud Score+ collection, aligned to the harmonized Sentinel-2
#: archive by `system:index` — the default `CloudScorePlus.cs_collection`.
_CS_PLUS_COLLECTION: str = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"

#: Bit 10 of the Sentinel-2 `QA60` bitmask: opaque clouds.
_S2_QA60_CLOUD_BIT: int = 1 << 10

#: Bit 11 of the Sentinel-2 `QA60` bitmask: cirrus clouds.
_S2_QA60_CIRRUS_BIT: int = 1 << 11

#: Right-shift bringing the `QA_PIXEL` Cloud Confidence bit-pair (bits 8-9,
#: USGS LSDS-1330) down to the low two bits.
_LS_CLOUD_CONF_SHIFT: int = 8

#: Right-shift bringing the `QA_PIXEL` Cloud Shadow Confidence bit-pair
#: (bits 10-11) down to the low two bits.
_LS_SHADOW_CONF_SHIFT: int = 10

#: The two-bit mask isolating one shifted `QA_PIXEL` confidence field:
#: 0 (not set) / 1 (low) / 2 (medium) / 3 (high).
_LS_CONF_PAIR_MASK: int = 0b11

#: The two-bit cloud-state field at bits 0-1 of the MODIS `state_1km` band:
#: 0 clear, 1 cloudy, 2 mixed, 3 not set (assumed clear by MODIS, masked here).
_MODIS_CLOUD_STATE_MASK: int = 0b11


def _op_name(op: PerImageOp) -> str:
    """Name one chain element, whether it is a mask object or a function.

    Args:
        op: An `ImageMask` instance or a plain `ee.Image -> ee.Image`
            callable.

    Returns:
        The class name for an object, the function name for a function.

    Examples:
        ```python
        >>> _op_name(Sentinel2Qa60Mask()), _op_name(sentinel2_scl)
        ('Sentinel2Qa60Mask', 'sentinel2_scl')

        ```
    """
    return type(op).__name__ if isinstance(op, ImageMask) else op.__name__


def _with_properties(derived: ee.Image, source: ee.Image) -> ee.Image:
    """Copy every property of `source` — system ones included — onto `derived`.

    Band arithmetic builds a new image that carries no metadata, and
    `copyProperties` omits the `system:*` properties unless they are named
    explicitly (its `properties` argument defaults to the *ordinary* ones), so
    the property list is passed in. `system:time_start` is the one that
    matters: `CompositeEngine.composite_ee` applies its per-bucket
    `filterDate` *after* the `cloud_mask` hook, and the temporal reducers and
    samplers read the timestamp — an op that dropped it would empty every
    bucket.

    Args:
        derived: The image computed from `source`.
        source: The image whose properties should survive.

    Returns:
        `derived` with `source`'s full property set, as an `ee.Image`.
    """
    copied = ee.Image(derived.copyProperties(source, source.propertyNames()))
    return cast("ee.Image", copied)


def _checked_cdi_threshold(value: float) -> float:
    """Refuse a CDI threshold outside the inclusive `[-1, 1]` range.

    The one check `Sentinel2Cdi` and the `Sentinel2Composite` reducer
    share, so the error text cannot drift between them.

    Args:
        value: The parsed `threshold` input.

    Returns:
        The value unchanged when it is in range.

    Raises:
        ValueError: If `value` is outside `[-1, 1]` — the CDI band is a
            `[-1, 1]` index, not a probability or a percentage.
    """
    if not -1 <= value <= 1:
        raise ValueError(f"threshold must be in [-1, 1], got {value!r}")
    return value


def _checked_sensor(value: object) -> object:
    """Refuse an unsupported sensor name with the long-standing message.

    The one check both Landsat masks share, so the error text cannot
    drift between them.

    Args:
        value: The raw `sensor` input, before pydantic's `Literal` check.

    Returns:
        The value unchanged when it names a supported sensor.

    Raises:
        ValueError: If `value` is not one of the supported names.
    """
    if not (isinstance(value, str) and value in _SUPPORTED_SENSORS):
        raise ValueError(
            f"sensor must be one of {sorted(_SUPPORTED_SENSORS)}, got {value!r}"
        )
    return value


class ImageMask(BaseModel):
    """Callable per-image mask — a frozen drop-in for the `cloud_mask=` hook.

    Subclasses carry their config as validated pydantic fields
    (`frozen=True`, `extra="forbid"`) and implement
    `__call__(image) -> ee.Image`, so an instance satisfies the
    `earthlens.gee.CloudMask` alias (`Callable[[ee.Image], ee.Image]`)
    and plugs into `GEE(cloud_mask=...)` — which `.map`-applies it to
    the collection before the reducer — with no backend change.

    Setting any mask forces server-side compute: the EEDAI fast path
    declines the request (`EngineConfig.needs_server_side_compute`) and
    it runs on an Earth Engine engine instead.

    Subclasses must derive their output with `updateMask`, which keeps
    the image's properties — `system:time_start` in particular, which
    the composite engine's per-bucket date filter reads *after* the mask
    has been applied. A mask that rebuilt the image without its
    properties would silently empty every bucket.

    Because the backend has a *single* `cloud_mask` slot, masks compose
    with `>>` into a `MaskChain` — itself an `ImageMask` — so a cloud
    mask and a `BandScaler` can both apply to one request. The
    client-side `analysis.OperationChain` cannot do this: it composes
    `RasterOperation`s over a downloaded pyramids `Dataset`, not
    `ee.Image` graphs.

    Examples:
        - A concrete mask carries its config as readable fields:
            ```python
            >>> LandsatSrMask(sensor="LS8").sensor
            'LS8'

            ```
        - The base class only names the contract; calling it raises:
            ```python
            >>> ImageMask()(None)
            Traceback (most recent call last):
                ...
            NotImplementedError: ImageMask must implement __call__(image)

            ```
        - `>>` composes two ops into one chain, left to right:
            ```python
            >>> chain = Sentinel2SclMask() >> Sentinel2Qa60Mask()
            >>> chain.name
            'Sentinel2SclMask >> Sentinel2Qa60Mask'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def __call__(self, image: ee.Image) -> ee.Image:
        """Mask one image; concrete masks implement this.

        Args:
            image: The `ee.Image` to mask.

        Returns:
            The masked `ee.Image`.

        Raises:
            NotImplementedError: Always — `ImageMask` itself is the
                contract, not a mask.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement __call__(image)"
        )

    @property
    def name(self) -> str:
        """A short label for this op, used in chain names and errors."""
        return type(self).__name__

    def __rshift__(self, other: ImageMask) -> MaskChain:
        """Compose with `other`, returning a chain that runs self first.

        Chains flatten rather than nest — `a >> b >> c` is one chain of
        three — so `name` and iteration stay readable however the
        expression was grouped. This mirrors
        `analysis.RasterOperation.__rshift__`, one level down: there the
        elements run client-side over a pyramids `Dataset`, here they run
        server-side over an `ee.Image`.

        Args:
            other: The mask to run on this one's output.

        Returns:
            A `MaskChain` of both, left to right.

        Raises:
            TypeError: If `other` is not an `ImageMask`. A plain
                callable has no `>>`, so compose it with
                `MaskChain.of(...)` instead.
        """
        if not isinstance(other, ImageMask):
            raise TypeError(
                f"cannot compose {self.name} with {type(other).__name__}: "
                ">> needs an ImageMask on both sides (use MaskChain.of(...) "
                "for a plain callable)"
            )
        left = self.masks if isinstance(self, MaskChain) else (self,)
        right = other.masks if isinstance(other, MaskChain) else (other,)
        return MaskChain(masks=(*left, *right))


class PassThrough(ImageMask):
    """The identity op — returns the image untouched.

    The honest step of a recipe that has nothing to apply. Sentinel-1 is
    the case: Earth Engine's `COPERNICUS/S1_GRD` already arrives
    thermal-noise-removed, radiometrically calibrated and
    terrain-corrected to dB, so its catalog recipe names no mask and
    nothing to scale — yet `earthlens.gee.preprocess` must still hand
    back a usable `MaskChain` the caller can extend
    (`>> SpeckleFilter()`). The recipe's `note` carries the explanation;
    this carries the no-op, so "nothing to do here" is a documented
    object rather than a missing one.

    Examples:
        - It hands the image straight back, properties and all:
            ```python
            >>> PassThrough()("an image")
            'an image'

            ```
        - And it composes like any other op:
            ```python
            >>> (PassThrough() >> SpeckleFilter()).name
            'PassThrough >> SpeckleFilter'

            ```
    """

    def __call__(self, image: ee.Image) -> ee.Image:
        """Return the image unchanged.

        Args:
            image: The `ee.Image` the chain was handed.

        Returns:
            That same image — nothing is rebuilt, so its properties are
            trivially preserved.
        """
        return image


class MaskChain(ImageMask):
    """An ordered run of per-image ops, itself an `ImageMask`.

    Being an `ImageMask` is the point: the backend's one `cloud_mask`
    slot accepts a chain wherever it accepts a mask, so "mask *and*
    scale" needs no second hook. Elements run left to right, each on the
    previous one's output, which is why order is part of the recipe — a
    cloud mask reads a raw quality band, so it must run *before* a
    `BandScaler` touches the pixels with arithmetic.

    Attributes:
        masks: The ops to run, in order. Each is an `ImageMask` or any
            `ee.Image -> ee.Image` callable (so the module-function forms
            `landsat_sr` / `sentinel2_scl` compose too). At least one.

    Examples:
        - Build a chain with `>>` and read its name:
            ```python
            >>> chain = Sentinel2SclMask() >> BandScaler(
            ...     factors=(BandFactor(band="B4", scale=0.0001),)
            ... )
            >>> chain.name
            'Sentinel2SclMask >> BandScaler'
            >>> len(chain.masks)
            2

            ```
        - Composition flattens, so grouping does not change the result:
            ```python
            >>> a, b, c = Sentinel2SclMask(), Sentinel2Qa60Mask(), Sentinel2Cdi()
            >>> len(((a >> b) >> c).masks), len((a >> (b >> c)).masks)
            (3, 3)

            ```
        - `of` takes the ops positionally, and accepts the function forms:
            ```python
            >>> MaskChain.of(sentinel2_scl, Sentinel2Qa60Mask()).name
            'sentinel2_scl >> Sentinel2Qa60Mask'

            ```
    """

    masks: tuple[PerImageOp, ...] = Field(min_length=1)

    @classmethod
    def of(cls, *masks: PerImageOp) -> MaskChain:
        """Build a chain from ops given positionally.

        Args:
            *masks: The ops to run, in order. At least one.

        Returns:
            The chain running them left to right.

        Examples:
            ```python
            >>> len(MaskChain.of(Sentinel2Qa60Mask(), sentinel2_scl).masks)
            2

            ```
        """
        return cls(masks=masks)

    @property
    def name(self) -> str:
        """The element names joined by `>>`, in run order."""
        return " >> ".join(_op_name(mask) for mask in self.masks)

    def __call__(self, image: ee.Image) -> ee.Image:
        """Run every op in order, threading each one's output into the next.

        Args:
            image: The `ee.Image` the first op receives.

        Returns:
            The last op's output. Each element preserves the image's
            properties, so `system:time_start` survives the whole chain.
        """
        result = image
        for mask in self.masks:
            result = mask(result)
        return result


class LandsatSrMask(ImageMask):
    """Mask a Landsat C2-L2 surface-reflectance image to its Clear pixels.

    Reads `QA_PIXEL` and keeps only pixels whose bit 6 ("Clear") is set
    — i.e. no Dilated Cloud, no Cloud, no Cloud Shadow per USGS's
    derived flag. The class form of `landsat_sr`, for carrying the
    sensor choice as validated config through the `cloud_mask=` hook
    (which forces the request onto an Earth Engine engine).

    Attributes:
        sensor: Source sensor name; one of `"LS7"`, `"LS8"`, `"LS9"`,
            or `"auto"` (the default). Currently informational — the
            Clear bit is at the same position across all three C2-L2
            sensors — but kept so callers can declare intent and future
            per-sensor refinements have a seam. An unknown name is
            refused at construction.

    Examples:
        - The default accepts any supported sensor's imagery:
            ```python
            >>> LandsatSrMask().sensor
            'auto'

            ```
        - An unknown sensor is refused at construction:
            ```python
            >>> LandsatSrMask(sensor="S2")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for LandsatSrMask
            ...

            ```
        - Hand an instance to the GEE backend as the per-image mask:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="LANDSAT/LC08/C02/T1_L2",
            ...     variables=["SR_B4", "SR_B3", "SR_B2"],
            ...     start="2023-06-01", end="2023-09-01",
            ...     reducer="median", cloud_mask=LandsatSrMask(sensor="LS8"),
            ... )

            ```
    """

    sensor: Sensor = "auto"

    @field_validator("sensor", mode="before")
    @classmethod
    def _check_sensor(cls, value: object) -> object:
        """Refuse an unsupported sensor name with the long-standing message.

        Args:
            value: The raw `sensor` input, before pydantic's `Literal`
                check.

        Returns:
            The value unchanged when it names a supported sensor.

        Raises:
            ValueError: If `value` is not one of the supported names.
        """
        return _checked_sensor(value)

    def __call__(self, image: ee.Image) -> ee.Image:
        """Mask one image to its `QA_PIXEL` Clear pixels.

        Args:
            image: An `ee.Image` from `LANDSAT/LE07/C02/T1_L2`,
                `LANDSAT/LC08/C02/T1_L2`, or `LANDSAT/LC09/C02/T1_L2`.
                Must carry the `QA_PIXEL` band.

        Returns:
            The input image with `updateMask(qa.bitwiseAnd(1 << 6))`
            applied — properties, `system:time_start` included, are
            preserved.
        """
        qa = image.select("QA_PIXEL")
        clear_pixels = qa.bitwiseAnd(_QA_PIXEL_CLEAR_BIT)
        return image.updateMask(clear_pixels)


class Sentinel2SclMask(ImageMask):
    """Mask a Sentinel-2 L2A image to its clear pixels via the `SCL` band.

    Reads the Scene Classification (`SCL`) band and keeps only pixels
    whose class is in none of `masked_classes`. The class form of
    `sentinel2_scl`, with the dropped-class set as validated config,
    for the `cloud_mask=` hook (which forces the request onto an Earth
    Engine engine).

    The default set deliberately keeps some non-clear classes that
    other recipes also drop — notably saturated / defective (1) and
    snow / ice (11); pass a wider `masked_classes` to screen them too.

    Attributes:
        masked_classes: The `SCL` class codes to drop. Defaults to
            cloud shadow (3), cloud medium / high probability (8 / 9)
            and thin cirrus (10). Must name at least one class.

    Examples:
        - The default drops the four cloud-related classes:
            ```python
            >>> Sentinel2SclMask().masked_classes
            (3, 8, 9, 10)

            ```
        - Widen the drop set to screen snow / ice as well:
            ```python
            >>> Sentinel2SclMask(masked_classes=(3, 8, 9, 10, 11)).masked_classes
            (3, 8, 9, 10, 11)

            ```
        - Hand an instance to the GEE backend as the per-image mask:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="COPERNICUS/S2_SR_HARMONIZED",
            ...     variables=["B4", "B3", "B2"],
            ...     start="2024-05-01", end="2024-06-15",
            ...     reducer="median", cloud_mask=Sentinel2SclMask(),
            ... )

            ```
    """

    masked_classes: tuple[int, ...] = Field(
        default=_S2_SCL_MASKED_CLASSES, min_length=1
    )

    def __call__(self, image: ee.Image) -> ee.Image:
        """Mask one image to the pixels outside every dropped `SCL` class.

        Args:
            image: An `ee.Image` from `COPERNICUS/S2_SR_HARMONIZED` (or
                the older `COPERNICUS/S2_SR`). Must carry the `SCL`
                band, which the Level-2A products provide.

        Returns:
            The input image with `updateMask(...)` applied, the mask
            being the logical AND of `SCL != c` over every class `c` in
            `masked_classes` — properties, `system:time_start`
            included, are preserved.
        """
        scl = image.select("SCL")
        keep = scl.neq(self.masked_classes[0])
        for masked_class in self.masked_classes[1:]:
            keep = keep.And(scl.neq(masked_class))
        return image.updateMask(keep)


class CloudScorePlus(ImageMask):
    """Mask a Sentinel-2 scene with Google's Cloud Score+ quality score.

    Cloud Score+ is the current state-of-the-art Sentinel-2 cloud /
    shadow mask. Its scores live in a *separate* collection
    (`GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED`) aligned to the
    harmonized S2 archive, so the mask links each scene to its score
    image with `ee.Image.linkCollection` — a per-image match on
    `system:index` — and keeps pixels whose score is at or above
    `threshold`. Because the link is per image, the mask works
    unchanged under `collection.map(...)` and therefore slots straight
    into the `cloud_mask=` hook (which forces the request onto an Earth
    Engine engine — reading a second collection is server-side compute
    the EEDAI reader cannot run).

    The two score bands differ: `cs` scores each pixel on its own,
    while `cs_cdf` integrates a cumulative distribution and tends to be
    more conservative over bright surfaces. `cs` with a `0.6` threshold
    is Google's documented default. Cloud Score+ folds shadow signal
    into both bands, so no separate shadow projection is applied here.

    Attributes:
        band: The score band to threshold — `"cs"` (the default) or
            `"cs_cdf"`.
        threshold: Pixels scoring below this are masked out. A `[0, 1]`
            quality score, not a percentage; validated at construction.
            Defaults to `0.6`.
        cs_collection: The Cloud Score+ collection to link. Defaults to
            `GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED`.

    Examples:
        - The defaults are Google's documented recipe:
            ```python
            >>> mask = CloudScorePlus()
            >>> mask.band, mask.threshold
            ('cs', 0.6)

            ```
        - Pick the conservative CDF band with a looser threshold:
            ```python
            >>> CloudScorePlus(band="cs_cdf", threshold=0.5).band
            'cs_cdf'

            ```
        - A threshold outside the unit interval is refused at construction:
            ```python
            >>> CloudScorePlus(threshold=1.5)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for CloudScorePlus
            ...

            ```
        - Hand an instance to the GEE backend as the per-image mask:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="COPERNICUS/S2_SR_HARMONIZED",
            ...     variables=["B4", "B3", "B2"],
            ...     start="2024-05-01", end="2024-06-15",
            ...     reducer="median", cloud_mask=CloudScorePlus(),
            ... )

            ```
    """

    band: Literal["cs", "cs_cdf"] = "cs"
    threshold: float = 0.6
    cs_collection: str = _CS_PLUS_COLLECTION

    @field_validator("threshold")
    @classmethod
    def _in_unit_interval(cls, value: float) -> float:
        """Refuse a threshold outside the inclusive `[0, 1]` range.

        Args:
            value: The parsed `threshold` input.

        Returns:
            The value unchanged when it is in range.

        Raises:
            ValueError: If `value` is outside `[0, 1]` — the score bands
                are unit-interval qualities, not percentages.
        """
        if not 0 <= value <= 1:
            raise ValueError(f"threshold must be in [0, 1], got {value!r}")
        return value

    def __call__(self, image: ee.Image) -> ee.Image:
        """Link the score band onto one scene and mask by the threshold.

        Args:
            image: An `ee.Image` from the harmonized Sentinel-2 archive
                (`COPERNICUS/S2_SR_HARMONIZED` / `S2_HARMONIZED`) — the
                collections Cloud Score+ shares `system:index` with.

        Returns:
            The scene, with the score band added by `linkCollection`
            and `updateMask(score >= threshold)` applied — properties,
            `system:time_start` included, are preserved. A scene with
            no Cloud Score+ match gets a fully masked score band
            (Earth Engine's `linkCollection` contract), so it drops out
            of a composite rather than passing unmasked.
        """
        linked = image.linkCollection(
            ee.ImageCollection(self.cs_collection), [self.band]
        )
        return linked.updateMask(linked.select(self.band).gte(self.threshold))


class Sentinel2Cdi(ImageMask):
    """Mask a Sentinel-2 TOA scene by its Cloud Displacement Index.

    Wraps `ee.Algorithms.Sentinel2.CDI(image)` — Earth Engine's
    parallax-based cloud discriminator, computed from the scene's own
    band geometry — and keeps pixels whose CDI is at or above
    `threshold`. Clouds push the CDI strongly negative, so the `-0.5`
    default keeps the recommended operating point. CDI runs on the
    **TOA** product (`COPERNICUS/S2_HARMONIZED`), not L2A — pointing it
    at a surface-reflectance collection fails with a band-not-found
    error server-side when the graph is computed.

    A named algorithm is server-side compute, so the mask — like every
    `cloud_mask=` — forces the request onto an Earth Engine engine. It
    is also the per-image half of the
    `earthlens.gee.reducers.Sentinel2Composite` temporal reducer, which
    maps it over the collection before reducing.

    Attributes:
        threshold: Pixels whose CDI falls below this are masked out. A
            `[-1, 1]` index value, validated at construction; defaults
            to `-0.5`.

    Examples:
        - The default is the recommended operating point:
            ```python
            >>> Sentinel2Cdi().threshold
            -0.5

            ```
        - A threshold outside the CDI range is refused at construction:
            ```python
            >>> Sentinel2Cdi(threshold=1.5)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for Sentinel2Cdi
            ...

            ```
        - Hand an instance to the GEE backend as the per-image mask:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="COPERNICUS/S2_HARMONIZED",
            ...     variables=["B4", "B3", "B2"],
            ...     start="2024-05-01", end="2024-06-15",
            ...     reducer="median", cloud_mask=Sentinel2Cdi(),
            ... )

            ```
    """

    threshold: float = -0.5

    @field_validator("threshold")
    @classmethod
    def _in_cdi_range(cls, value: float) -> float:
        """Refuse a threshold outside the inclusive `[-1, 1]` CDI range.

        Args:
            value: The parsed `threshold` input.

        Returns:
            The value unchanged when it is in range.

        Raises:
            ValueError: If `value` is outside `[-1, 1]`.
        """
        return _checked_cdi_threshold(value)

    def __call__(self, image: ee.Image) -> ee.Image:
        """Mask one TOA scene where its CDI falls below the threshold.

        Args:
            image: An `ee.Image` from `COPERNICUS/S2_HARMONIZED` — the
                TOA product the CDI algorithm is defined on.

        Returns:
            The input image with `updateMask(cdi >= threshold)` applied
            — properties, `system:time_start` included, are preserved.
        """
        cdi = ee.Algorithms.Sentinel2.CDI(image)
        return image.updateMask(cdi.gte(self.threshold))


class Sentinel2Qa60Mask(ImageMask):
    """Mask a Sentinel-2 image via the `QA60` cloud bitmask.

    The standard pre-Scene-Classification recipe: reads the `QA60` band
    and keeps only pixels with neither the opaque-cloud bit (10) nor
    the cirrus bit (11) set. Use it on TOA scenes
    (`COPERNICUS/S2_HARMONIZED`), which carry `QA60` but no `SCL` band;
    the Level-2A products can use `Sentinel2SclMask` instead (both
    harmonized catalog rows declare `QA60`). Like every mask here it
    forces the request onto an Earth Engine engine.

    Examples:
        - The mask carries no config, so its name is all there is to read:
            ```python
            >>> Sentinel2Qa60Mask().name
            'Sentinel2Qa60Mask'

            ```
        - Pair it with the CDI mask for a stricter TOA screen:
            ```python
            >>> (Sentinel2Qa60Mask() >> Sentinel2Cdi()).name
            'Sentinel2Qa60Mask >> Sentinel2Cdi'

            ```
        - Hand an instance to the GEE backend as the per-image mask:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="COPERNICUS/S2_HARMONIZED",
            ...     variables=["B4", "B3", "B2"],
            ...     start="2024-05-01", end="2024-06-15",
            ...     reducer="median", cloud_mask=Sentinel2Qa60Mask(),
            ... )

            ```
    """

    def __call__(self, image: ee.Image) -> ee.Image:
        """Mask one image to the pixels with no `QA60` cloud or cirrus flag.

        Args:
            image: An `ee.Image` carrying the `QA60` band — the
                harmonized Sentinel-2 TOA / SR products declare it.

        Returns:
            The input image with `updateMask(...)` applied, the mask
            being the AND of "bit 10 clear" and "bit 11 clear" —
            properties, `system:time_start` included, are preserved.
        """
        qa = image.select("QA60")
        clear = (
            qa.bitwiseAnd(_S2_QA60_CLOUD_BIT)
            .eq(0)
            .And(qa.bitwiseAnd(_S2_QA60_CIRRUS_BIT).eq(0))
        )
        return image.updateMask(clear)


class LandsatQaRefinedMask(ImageMask):
    """Mask a Landsat C2-L2 image by its `QA_PIXEL` confidence bit-pairs.

    The per-sensor refinement the `LandsatSrMask.sensor` argument was
    reserved for: instead of trusting the single derived Clear bit, it
    reads the Cloud Confidence (bits 8-9) and Cloud Shadow Confidence
    (bits 10-11) fields of `QA_PIXEL` (USGS LSDS-1330) and keeps only
    pixels at or below the configured confidence ceilings — so a caller
    can demand, say, cloud confidence `<= 1` rather than accepting
    whatever the Clear bit folded in. Forces the request onto an Earth
    Engine engine, like every `cloud_mask=`.

    Attributes:
        sensor: Source sensor name; one of `"LS7"`, `"LS8"`, `"LS9"`,
            or `"auto"` (the default). The confidence bit-pairs sit at
            the same positions across the three C2-L2 sensors, so the
            field declares intent; an unknown name is refused at
            construction.
        max_cloud_confidence: Highest Cloud Confidence value kept —
            `0` (not set) to `3` (high). Defaults to `2` (keep up to
            medium confidence).
        max_shadow_confidence: Highest Cloud Shadow Confidence value
            kept, same `0`-`3` scale. Defaults to `2`.

    Examples:
        - The defaults keep up-to-medium confidence on both fields:
            ```python
            >>> mask = LandsatQaRefinedMask()
            >>> mask.max_cloud_confidence, mask.max_shadow_confidence
            (2, 2)

            ```
        - A ceiling outside the 2-bit field is refused at construction:
            ```python
            >>> LandsatQaRefinedMask(max_cloud_confidence=4)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for LandsatQaRefinedMask
            ...

            ```
        - Hand an instance to the GEE backend as the per-image mask:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="LANDSAT/LC08/C02/T1_L2",
            ...     variables=["SR_B4", "SR_B3", "SR_B2"],
            ...     start="2023-06-01", end="2023-09-01",
            ...     reducer="median",
            ...     cloud_mask=LandsatQaRefinedMask(max_cloud_confidence=1),
            ... )

            ```
    """

    sensor: Sensor = "auto"
    max_cloud_confidence: int = Field(default=2, ge=0, le=3)
    max_shadow_confidence: int = Field(default=2, ge=0, le=3)

    @field_validator("sensor", mode="before")
    @classmethod
    def _check_sensor(cls, value: object) -> object:
        """Refuse an unsupported sensor name with the long-standing message.

        Args:
            value: The raw `sensor` input, before pydantic's `Literal`
                check.

        Returns:
            The value unchanged when it names a supported sensor.

        Raises:
            ValueError: If `value` is not one of the supported names.
        """
        return _checked_sensor(value)

    def __call__(self, image: ee.Image) -> ee.Image:
        """Mask one image by both `QA_PIXEL` confidence ceilings.

        Args:
            image: An `ee.Image` from `LANDSAT/LE07/C02/T1_L2`,
                `LANDSAT/LC08/C02/T1_L2`, or `LANDSAT/LC09/C02/T1_L2`.
                Must carry the `QA_PIXEL` band.

        Returns:
            The input image with `updateMask(...)` applied, the mask
            being `cloud_conf <= max_cloud_confidence AND shadow_conf
            <= max_shadow_confidence` — properties,
            `system:time_start` included, are preserved.
        """
        qa = image.select("QA_PIXEL")
        cloud_conf = qa.rightShift(_LS_CLOUD_CONF_SHIFT).bitwiseAnd(_LS_CONF_PAIR_MASK)
        shadow_conf = qa.rightShift(_LS_SHADOW_CONF_SHIFT).bitwiseAnd(
            _LS_CONF_PAIR_MASK
        )
        keep = cloud_conf.lte(self.max_cloud_confidence).And(
            shadow_conf.lte(self.max_shadow_confidence)
        )
        return image.updateMask(keep)


class ModisState1kmMask(ImageMask):
    """Mask a daily MODIS surface-reflectance image via `state_1km`.

    Covers the daily MODIS products (`MODIS/061/MOD09GA` /
    `MODIS/061/MYD09GA`, both of which declare the band in the
    catalog): the cloud state lives in the 2-bit field at bits 0-1 of
    `state_1km`, and only the "clear" code (0) is kept — cloudy (1),
    mixed (2) and not-set (3) pixels are all masked. Forces the request
    onto an Earth Engine engine, like every `cloud_mask=`.

    Examples:
        - The mask carries no config, so its name is all there is to read:
            ```python
            >>> ModisState1kmMask().name
            'ModisState1kmMask'

            ```
        - Compose it with the reflectance scaler the MODIS rows declare:
            ```python
            >>> factors = (BandFactor(band="sur_refl_b01", scale=0.0001),)
            >>> (ModisState1kmMask() >> BandScaler(factors=factors)).name
            'ModisState1kmMask >> BandScaler'

            ```
        - Hand an instance to the GEE backend as the per-image mask:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="MODIS/061/MOD09GA",
            ...     variables=["sur_refl_b01", "sur_refl_b04", "sur_refl_b03"],
            ...     start="2023-06-01", end="2023-07-01",
            ...     reducer="median", cloud_mask=ModisState1kmMask(),
            ... )

            ```
    """

    def __call__(self, image: ee.Image) -> ee.Image:
        """Mask one image to the pixels whose cloud state reads "clear".

        Args:
            image: An `ee.Image` carrying the `state_1km` band — the
                daily MOD09GA / MYD09GA products provide it.

        Returns:
            The input image with
            `updateMask(state.bitwiseAnd(0b11).eq(0))` applied —
            properties, `system:time_start` included, are preserved.
        """
        state = image.select("state_1km")
        clear = state.bitwiseAnd(_MODIS_CLOUD_STATE_MASK).eq(0)
        return image.updateMask(clear)


class BandFactor(BaseModel):
    """One band's linear conversion from raw DN to physical units.

    The pair the catalog declares per band: `physical = raw * scale +
    offset`. Either half may be left at its identity, so a band that
    declares only a multiplier needs only `scale`.

    Attributes:
        band: The band id the factor applies to, as the catalog spells
            it (e.g. `"B4"`, `"SR_B4"`). Must be non-empty.
        scale: The multiplier. Defaults to `1.0` (no scaling).
        offset: The additive term, applied after the multiplier.
            Defaults to `0.0` (no offset).

    Examples:
        - Sentinel-2 surface reflectance is a pure multiplier:
            ```python
            >>> f = BandFactor(band="B4", scale=0.0001)
            >>> f.band, f.scale, f.offset
            ('B4', 0.0001, 0.0)

            ```
        - Landsat C2-L2 reflectance carries both halves:
            ```python
            >>> BandFactor(band="SR_B4", scale=2.75e-05, offset=-0.2).offset
            -0.2

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    band: str = Field(min_length=1)
    scale: float = 1.0
    offset: float = 0.0


class BandScaler(ImageMask):
    """Convert raw DN to physical units with the catalog's own factors.

    The catalog records `Band.scale` ("multiply the raw DN by this to get
    physical units") and `Band.offset` on the rows that need them —
    `COPERNICUS/S2_SR_HARMONIZED` declares `scale: 0.0001` on every
    reflectance band, Landsat C2-L2 declares `2.75e-05` / `-0.2` — and
    until now nothing consumed either field, so a surface-reflectance
    request returned 0-10000 integers with nothing saying a conversion
    was outstanding. `BandScaler` applies them **server-side**, before
    the composite, so the temporal reduction happens in physical units.

    It is an `ImageMask` so it rides the backend's existing `cloud_mask=`
    hook — which also means it forces the request onto an Earth Engine
    engine (`EngineConfig.needs_server_side_compute`), the EEDAI reader
    declining automatically, since stored pixels cannot carry the
    arithmetic. Scaling raises the dtype from integer to float;
    `ExportConfig.dtype` is the escape hatch if a narrower output is
    wanted.

    Compose it *after* a cloud mask — `Sentinel2SclMask() >>
    BandScaler(...)` — so the mask reads its quality band before any
    arithmetic touches the image. `earthlens.gee.preprocess` builds that
    chain from the catalog for you.

    Attributes:
        factors: The per-band conversions to apply. Bands not named here
            pass through untouched, and an empty tuple makes the scaler
            an identity (which is what `for_dataset` returns, with a
            warning, for a dataset declaring no factors).

    Examples:
        - Construct from the factors directly:
            ```python
            >>> scaler = BandScaler(factors=(BandFactor(band="B4", scale=0.0001),))
            >>> scaler.name
            'BandScaler'

            ```
        - Or resolve them from a catalog row, which skips the bands that
          declare neither:
            ```python
            >>> from earthlens.gee.catalog import Band, Dataset, Extent
            >>> row = Dataset(
            ...     id="COPERNICUS/S2_SR_HARMONIZED",
            ...     title="S2 L2A",
            ...     extent=Extent(start_date="2017-03-28"),
            ...     bands={
            ...         "B4": Band(id="B4", scale=0.0001),
            ...         "SCL": Band(id="SCL"),
            ...     },
            ... )
            >>> [f.band for f in BandScaler.for_dataset(row).factors]
            ['B4']

            ```
        - Hand the composed chain to the GEE backend:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="COPERNICUS/S2_SR_HARMONIZED",
            ...     variables=["B4", "B3", "B2"],
            ...     start="2024-05-01", end="2024-06-15", reducer="median",
            ...     cloud_mask=Sentinel2SclMask() >> BandScaler.for_dataset(row),
            ... )

            ```
    """

    factors: tuple[BandFactor, ...] = ()

    @classmethod
    def for_dataset(
        cls, dataset: Dataset, bands: Iterable[str] | None = None
    ) -> BandScaler:
        """Resolve the factors a catalog row declares into a scaler.

        A band is included only when its row declares `scale` or
        `offset`; the missing half falls back to its identity. A band
        named in `bands` that the row does not carry is skipped — the
        backend already refuses an unknown band id before this point.

        Args:
            dataset: The catalog row to read the factors from.
            bands: The band ids to consider, usually the requested ones.
                Defaults to every band on the row.

        Returns:
            A `BandScaler` over the resolved factors — empty, after a
            `logger.warning`, when the row declares none, so a request
            asking to scale an unscalable dataset says so rather than
            silently doing nothing.

        Examples:
            - A row declaring no factors warns and yields an identity:
                ```python
                >>> from earthlens.gee.catalog import Band, Dataset, Extent
                >>> row = Dataset(
                ...     id="USGS/SRTMGL1_003", title="SRTM v3",
                ...     extent=Extent(start_date="2000-02-11"),
                ...     bands={"elevation": Band(id="elevation")},
                ... )
                >>> BandScaler.for_dataset(row).factors
                ()

                ```
        """
        wanted = tuple(bands) if bands is not None else tuple(dataset.bands)
        factors = []
        for band in wanted:
            row = dataset.bands.get(band)
            if row is None or (row.scale is None and row.offset is None):
                continue
            factors.append(
                BandFactor(
                    band=band,
                    scale=1.0 if row.scale is None else row.scale,
                    offset=0.0 if row.offset is None else row.offset,
                )
            )
        if not factors:
            logger.warning(
                f"{dataset.id!r} declares no band scale/offset for "
                f"{list(wanted)} — the scaler is an identity and the values "
                "stay raw DN; drop the scaling from the request, or curate "
                "the factors onto the catalog row."
            )
        return cls(factors=tuple(factors))

    def __call__(self, image: ee.Image) -> ee.Image:
        """Rescale the named bands, leaving every other band in place.

        Each factor's band is recomputed as `band * scale + offset` and
        written back over the original with `addBands(..., overwrite=
        True)`, so band order and the unnamed bands survive. The
        arithmetic itself produces a propertyless image, so the source's
        full property set — `system:time_start` included — is copied back
        on; see `_with_properties` for why that is load-bearing.

        Args:
            image: The `ee.Image` to rescale. Must carry every band the
                factors name, which holds on the backend path because
                the `cloud_mask` hook runs *before* `select(bands)`.

        Returns:
            The image with the named bands in physical units (as
            floats), carrying the input's properties.
        """
        scaled = image
        for factor in self.factors:
            converted = (
                image.select(factor.band)
                .multiply(factor.scale)
                .add(factor.offset)
                .rename(factor.band)
            )
            scaled = scaled.addBands(converted, overwrite=True)
        return _with_properties(scaled, image)


class SpeckleFilter(ImageMask):
    """Smooth speckle out of a radar scene with a focal median.

    The server-side counterpart of the client-side
    `analysis.FocalMedian`: it runs `ee.Image.focalMedian` on the Earth
    Engine graph instead of on a downloaded pyramids raster, so it can
    sit in a `cloud_mask=` chain and apply *before* a composite rather
    than after the download.

    It exists for the Sentinel-1 recipe. Earth Engine's
    `COPERNICUS/S1_GRD` already arrives thermal-noise-removed,
    radiometrically calibrated and terrain-corrected to dB, so speckle
    filtering is the one genuine preprocessing gap — which is why the
    catalog's S1 recipe is a documented no-op that points here rather
    than pretending the absence is an oversight. Appending it is the
    "extend the default recipe" case:
    `preprocess("COPERNICUS/S1_GRD") >> SpeckleFilter()`.

    Despite the name it is an `ImageMask`-family per-image op, not one of
    the collection-level `earthlens.gee.filters`.

    Attributes:
        radius: Kernel radius, in `units`. Defaults to `50` (metres),
            roughly a 5x5 window on 10 m S1 GRD. Must be positive.
        units: Whether `radius` is read in `"meters"` (the default) or
            `"pixels"`.
        kernel_type: The kernel shape — `"circle"` (the default),
            `"square"`, `"cross"`, `"plus"`, `"octagon"` or
            `"diamond"`.
        iterations: How many times to apply the kernel. Defaults to `1`.

    Examples:
        - The defaults are a 50 m circular median:
            ```python
            >>> f = SpeckleFilter()
            >>> f.radius, f.units, f.kernel_type, f.iterations
            (50.0, 'meters', 'circle', 1)

            ```
        - A non-positive radius is refused at construction:
            ```python
            >>> SpeckleFilter(radius=0)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for SpeckleFilter
            ...

            ```
    """

    radius: float = Field(default=50.0, gt=0)
    units: Literal["meters", "pixels"] = "meters"
    kernel_type: Literal["circle", "square", "cross", "plus", "octagon", "diamond"] = (
        "circle"
    )
    iterations: int = Field(default=1, ge=1)

    def __call__(self, image: ee.Image) -> ee.Image:
        """Replace every pixel with the median of its neighbourhood.

        Args:
            image: The `ee.Image` to smooth — every band is filtered.

        Returns:
            The focal-median image, carrying the input's properties
            (`focalMedian` builds a new image, so they are copied back
            on).
        """
        smoothed = image.focalMedian(
            radius=self.radius,
            kernelType=self.kernel_type,
            units=self.units,
            iterations=self.iterations,
        )
        return _with_properties(smoothed, image)


def landsat_sr(image: ee.Image, sensor: Sensor = "auto") -> ee.Image:
    """Mask a Landsat C2-L2 surface-reflectance image to its Clear pixels.

    The function form of `LandsatSrMask` — it constructs the mask and
    applies it, preserving the original signature, defaults and errors.

    Args:
        image: An `ee.Image` from `LANDSAT/LE07/C02/T1_L2`,
            `LANDSAT/LC08/C02/T1_L2`, or `LANDSAT/LC09/C02/T1_L2`.
            Must carry the `QA_PIXEL` band.
        sensor: Source sensor name; one of `"LS7"`, `"LS8"`, `"LS9"`,
            or `"auto"` (the default). Currently informational — the
            Clear bit is at the same position across all three C2-L2
            sensors — but kept on the signature for future per-sensor
            refinements (e.g. tighter confidence thresholds) and so
            callers can declare intent.

    Returns:
        The input image with `updateMask(qa.bitwiseAnd(1 << 6))`
        applied.

    Raises:
        ValueError: If `sensor` is not one of the supported names.

    Examples:
        - Screen a Landsat-8 C2-L2 stack to clear pixels, then composite
          (`.map` the mask over the collection before the reducer):
            ```python
            >>> import ee  # doctest: +SKIP
            >>> stack = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")  # doctest: +SKIP
            >>> clear_median = stack.map(landsat_sr).median()  # doctest: +SKIP

            ```
        - Hand it to the GEE backend as the per-image `cloud_mask` so the
          facade builds the cloud-free composite for you:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="LANDSAT/LC08/C02/T1_L2",
            ...     variables=["SR_B4", "SR_B3", "SR_B2"],
            ...     start="2023-06-01", end="2023-09-01",
            ...     reducer="median", cloud_mask=landsat_sr,
            ... )

            ```
    """
    return LandsatSrMask(sensor=sensor)(image)


def sentinel2_scl(image: ee.Image) -> ee.Image:
    """Mask a Sentinel-2 L2A image to its clear pixels via the `SCL` band.

    The function form of `Sentinel2SclMask` — it constructs the mask
    with the default drop set and applies it, preserving the original
    signature and behaviour: pixels whose class is neither cloud shadow
    (3), cloud medium / high probability (8 / 9), nor thin cirrus (10)
    are kept — a common recipe for a clean surface-reflectance
    composite over `COPERNICUS/S2_SR_HARMONIZED`.

    It deliberately keeps some non-clear classes that other recipes also
    drop — notably saturated / defective (1) and snow / ice (11) — so a
    scene with those will retain them; screen them separately if that
    matters. The masked set (`_S2_SCL_MASKED_CLASSES`) is the one
    specified for this helper and is not a universal standard.

    Args:
        image: An `ee.Image` from `COPERNICUS/S2_SR_HARMONIZED` (or the
            older `COPERNICUS/S2_SR`). Must carry the `SCL` band, which
            the Level-2A products provide.

    Returns:
        The input image with `updateMask(...)` applied, the mask being
        the logical AND of `SCL != c` over every dropped class `c` in
        `_S2_SCL_MASKED_CLASSES`.

    Examples:
        - Drop cloudy pixels from a Sentinel-2 L2A stack before compositing:
            ```python
            >>> import ee  # doctest: +SKIP
            >>> s2 = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")  # doctest: +SKIP
            >>> clear_median = s2.map(sentinel2_scl).median()  # doctest: +SKIP

            ```
        - Use it as the GEE backend `cloud_mask` for a facade-built RGB mosaic
          — no raw `ee` needed:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="COPERNICUS/S2_SR_HARMONIZED",
            ...     variables=["B4", "B3", "B2"],
            ...     start="2024-05-01", end="2024-06-15",
            ...     reducer="median", cloud_mask=sentinel2_scl,
            ... )

            ```
    """
    return Sentinel2SclMask()(image)
