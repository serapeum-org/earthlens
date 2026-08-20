"""Cloud-masking helpers for Earth Engine Landsat / Sentinel collections.

Each helper is a per-image `ee.Image -> ee.Image` mask, suitable for
`ee.ImageCollection.map(...)` before a reducer — or for the `GEE`
backend's `cloud_mask=` hook, which `.map`-applies it to the stack
before compositing, so a cloud-screened median mosaic can be expressed
through the facade instead of raw `ee`.

:func:`landsat_sr` masks an LS7 / LS8 / LS9 Collection-2 Level-2
surface-reflectance image to its `QA_PIXEL` Clear bit (bit 6). The
Landsat C2-L2 `QA_PIXEL` bitmask is identical across LS7 / LS8 / LS9,
so the `sensor` argument is currently informational — kept on the
signature so callers can be explicit and so future per-sensor
refinements (e.g. extra confidence-bit thresholds) don't break the
public API.

:func:`sentinel2_scl` masks a Sentinel-2 L2A image via its Scene
Classification (`SCL`) band, dropping cloud shadow (class 3), cloud
medium / high probability (8 / 9) and thin cirrus (10).

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

# Bit 6 of QA_PIXEL: the "Clear" bit. Set when the pixel is neither
# Dilated Cloud (bit 1), Cloud (bit 3), nor Cloud Shadow (bit 4).
_QA_PIXEL_CLEAR_BIT: int = 1 << 6

Sensor = Literal["LS7", "LS8", "LS9", "auto"]
_SUPPORTED_SENSORS: frozenset[str] = frozenset({"LS7", "LS8", "LS9", "auto"})


def landsat_sr(image: ee.Image, sensor: Sensor = "auto") -> ee.Image:
    """Mask a Landsat C2-L2 surface-reflectance image to its Clear pixels.

    Reads `QA_PIXEL` and keeps only pixels whose bit 6 ("Clear") is set
    — i.e. no Dilated Cloud, no Cloud, no Cloud Shadow per USGS's
    derived flag.

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
    if sensor not in _SUPPORTED_SENSORS:
        raise ValueError(
            f"sensor must be one of {sorted(_SUPPORTED_SENSORS)}, got {sensor!r}"
        )
    qa = image.select("QA_PIXEL")
    clear_pixels = qa.bitwiseAnd(_QA_PIXEL_CLEAR_BIT)
    return image.updateMask(clear_pixels)


#: Sentinel-2 L2A Scene Classification (`SCL`) codes dropped by
#: :func:`sentinel2_scl`: cloud shadow (3), cloud medium probability (8),
#: cloud high probability (9) and thin cirrus (10). Every other class
#: (vegetation, bare soil, water, snow/ice, ...) is kept.
_S2_SCL_MASKED_CLASSES: tuple[int, ...] = (3, 8, 9, 10)


def sentinel2_scl(image: ee.Image) -> ee.Image:
    """Mask a Sentinel-2 L2A image to its clear pixels via the `SCL` band.

    Reads the Scene Classification (`SCL`) band and keeps only pixels
    whose class is neither cloud shadow (3), cloud medium / high
    probability (8 / 9), nor thin cirrus (10) — a common recipe for a
    clean surface-reflectance composite over `COPERNICUS/S2_SR_HARMONIZED`.

    It deliberately keeps some non-clear classes that other recipes also
    drop — notably saturated / defective (1) and snow / ice (11) — so a
    scene with those will retain them; screen them separately if that
    matters. The masked set (:data:`_S2_SCL_MASKED_CLASSES`) is the one
    specified for this helper and is not a universal standard.

    Args:
        image: An `ee.Image` from `COPERNICUS/S2_SR_HARMONIZED` (or the
            older `COPERNICUS/S2_SR`). Must carry the `SCL` band, which
            the Level-2A products provide.

    Returns:
        The input image with `updateMask(...)` applied, the mask being
        the logical AND of `SCL != c` over every dropped class `c` in
        :data:`_S2_SCL_MASKED_CLASSES`.

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
    scl = image.select("SCL")
    keep = scl.neq(_S2_SCL_MASKED_CLASSES[0])
    for masked_class in _S2_SCL_MASKED_CLASSES[1:]:
        keep = keep.And(scl.neq(masked_class))
    return image.updateMask(keep)
