"""Cloud-masking helpers for Earth Engine Landsat / Sentinel collections.

Each mask is a per-image `ee.Image -> ee.Image` transform, suitable for
`ee.ImageCollection.map(...)` before a reducer — or for the `GEE`
backend's `cloud_mask=` hook, which `.map`-applies it to the stack
before compositing, so a cloud-screened median mosaic can be expressed
through the facade instead of raw `ee`.

Masks come in two equivalent forms:

* **Callable classes** under the `ImageMask` base — `LandsatSrMask` and
  `Sentinel2SclMask` — frozen pydantic objects that carry validated
  config and are drop-ins for `cloud_mask=` (the hook only requires a
  callable). A typo'd keyword or an out-of-range value raises at
  construction instead of silently no-op'ing the way a misspelled
  `functools.partial` keyword would.
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

from typing import Literal

import ee
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ImageMask",
    "LandsatSrMask",
    "Sensor",
    "Sentinel2SclMask",
    "landsat_sr",
    "sentinel2_scl",
]

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
        if not (isinstance(value, str) and value in _SUPPORTED_SENSORS):
            raise ValueError(
                f"sensor must be one of {sorted(_SUPPORTED_SENSORS)}, got {value!r}"
            )
        return value

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
