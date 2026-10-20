"""Earth Engine time-series segmentation: LandTrendr and CCDC.

The rest of this epic reduces a window into one composite — it answers "what was
the average" and "is it trending", but not "**when did it change, and by how
much**", which is the question most land-change and hydrology work is actually
asking. Earth Engine answers it server-side with two flagship algorithms, and
this module exposes each as a frozen, validated config object:

* `LandTrendr` — `ee.Algorithms.TemporalSegmentation.LandTrendr`, which breaks a
  trajectory into straight segments at detected breakpoints. The first band
  drives breakpoint detection; the fit is then applied to the remaining bands.
* `Ccdc` — `ee.Algorithms.TemporalSegmentation.Ccdc`, continuous change
  detection, which fits per-segment harmonic models and reports change between
  them. It takes a parameter dictionary rather than positional arguments.

Two design points are encoded rather than left to the caller:

* **The sign convention is a correctness trap.** LandTrendr's fit assumes
  *increasing values are disturbance and decreasing values are recovery*. A
  vegetation index (NDVI, NBR) drops on disturbance, so it must be negated
  before fitting; a wetness or burn-severity index that rises on disturbance
  must not. Each config carries an explicit `invert` flag — defaulted to `True`
  on `LandTrendr` (the vegetation-index case is overwhelmingly the common one)
  and `False` on `Ccdc` (whose change detection is direction-agnostic) — and
  `segment()` negates the series when it is set. Getting this backwards
  silently inverts every result, so it is a field, documented and tested, not a
  convention.
* **The output is an `ee.Array` image, not an ordinary raster.** LandTrendr
  returns an image whose `LandTrendr` band is a 2-D array of vertices and fitted
  values; CCDC returns per-segment coefficient arrays. `segment()` wraps that in
  a `SegmentationResult` with array/band accessors and a documented `.client`
  hand-off, so nothing downstream mistakes it for a plottable `Dataset`.

Earth Engine's own parameter names are kept verbatim (camelCase) so a user can
follow the Earth Engine documentation without a translation table — the same
intentional deviation from `snake_case` the ECMWF backend makes for `ERA5` and
`CDS`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

import ee
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Ccdc", "LandTrendr", "SegmentationResult", "TemporalSegmentation"]


def _negate_image(image: Any) -> Any:
    """Negate one scene's pixels, keeping its acquisition time.

    The transform `segment()` maps over the input collection when `invert` is
    set: multiplying by `-1` flips the trajectory so a vegetation-index drop
    reads as the increase LandTrendr treats as disturbance. `system:time_start`
    is copied back because arithmetic drops an image's properties.

    Args:
        image: One `ee.Image` of the input collection.

    Returns:
        The negated `ee.Image`, carrying the original `system:time_start`.
    """
    return ee.Image(image.multiply(-1).copyProperties(image, ["system:time_start"]))


class SegmentationResult(BaseModel):
    """A segmentation's array-valued output, with accessors for its bands.

    The array image a segmentation returns is not a plottable raster, so it is
    surfaced as this value object rather than a pyramids `Dataset`: `array()`
    selects the primary array band, `band()` selects any named band, and
    `client` is the documented hand-off to the raw `ee.Image` for callers who
    want to post-process it with Earth Engine directly.

    Attributes:
        image: The raw `ee.Image` the algorithm returned — the `.client`
            hand-off.
        algorithm: Which algorithm produced it (`"LandTrendr"` or `"Ccdc"`).
        array_band: The name of the primary array band (`"LandTrendr"` for
            LandTrendr; a representative segment band such as `"tStart"` for
            CCDC).

    Examples:
        - The accessors forward to the wrapped image (shown with a stand-in):
            ```python
            >>> from types import SimpleNamespace
            >>> image = SimpleNamespace(select=lambda name: f"select:{name}")
            >>> result = SegmentationResult(
            ...     image=image, algorithm="LandTrendr", array_band="LandTrendr"
            ... )
            >>> result.array()
            'select:LandTrendr'
            >>> result.band("rmse")
            'select:rmse'
            >>> result.client is image
            True

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    image: Any
    algorithm: str
    array_band: str

    def array(self) -> Any:
        """Select the primary array band from the wrapped image.

        Returns:
            The `ee.Image` of the single array band, the usual input to
            `arraySlice` / `arrayFlatten` post-processing.
        """
        return self.image.select(self.array_band)

    def band(self, name: str) -> Any:
        """Select a named band from the wrapped image.

        Args:
            name: The band to select (e.g. `"rmse"` on a LandTrendr result).

        Returns:
            The `ee.Image` of that band.
        """
        return self.image.select(name)

    @property
    def client(self) -> Any:
        """Return the raw `ee.Image` for direct Earth Engine post-processing.

        The documented hand-off: array manipulation beyond selecting a band is
        left to the caller's own `ee` code rather than wrapped here.

        Returns:
            The underlying `ee.Image`.
        """
        return self.image


class TemporalSegmentation(BaseModel, ABC):
    """Frozen base for a configured segmentation algorithm.

    A subclass carries its algorithm's parameters with Earth Engine's documented
    defaults and implements `_run`, the single call into
    `ee.Algorithms.TemporalSegmentation`. The base owns the sign convention: when
    `invert` is set, `segment()` negates the input series before handing it to
    the algorithm, and wraps the array-valued output in a `SegmentationResult`.

    Attributes:
        invert: Whether to negate the input series before fitting, so that a
            disturbance reads as an increase. Set it `True` for an index that
            *drops* on disturbance (NDVI, NBR) and `False` for one that rises
            (wetness, burn severity). Subclasses default it to the common case.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The algorithm name recorded on the `SegmentationResult`.
    ALGORITHM: ClassVar[str]
    #: The primary array band the result exposes through `array()`.
    ARRAY_BAND: ClassVar[str]

    invert: bool = False

    def _orient(self, collection: Any) -> Any:
        """Negate the input series when `invert` is set, else pass it through.

        Args:
            collection: The (cloud-masked) input `ee.ImageCollection`.

        Returns:
            The collection oriented for the algorithm's disturbance convention.
        """
        if self.invert:
            oriented = collection.map(_negate_image)
        else:
            oriented = collection
        return oriented

    @abstractmethod
    def _run(self, time_series: Any) -> Any:
        """Run the algorithm on an already-oriented collection.

        Args:
            time_series: The oriented input `ee.ImageCollection`.

        Returns:
            The array-valued `ee.Image` the algorithm returns.
        """

    def segment(self, collection: Any) -> SegmentationResult:
        """Segment a (cloud-masked) input collection.

        Orients the series per `invert`, runs the algorithm server-side, and
        wraps the array-valued output so nothing downstream treats it as a
        plottable raster.

        Args:
            collection: The input `ee.ImageCollection` whose first band drives
                breakpoint detection. Mask clouds before passing it in — the
                algorithms assume a clean trajectory.

        Returns:
            A `SegmentationResult` over the algorithm's `ee.Array` image.
        """
        oriented = self._orient(collection)
        image = self._run(oriented)
        return SegmentationResult(
            image=image, algorithm=self.ALGORITHM, array_band=self.ARRAY_BAND
        )


class LandTrendr(TemporalSegmentation):
    """LandTrendr trajectory segmentation, with Earth Engine's documented defaults.

    Wraps `ee.Algorithms.TemporalSegmentation.LandTrendr`. The parameter names are
    Earth Engine's own (camelCase), so the `LandTrendr` entry in the Earth Engine
    documentation reads directly onto these fields. `invert` defaults to `True`,
    the orientation a vegetation index (NDVI, NBR) needs so that vegetation loss
    reads as the increase LandTrendr treats as a disturbance.

    Attributes:
        invert: Negate the series before fitting. Defaults to `True` for the
            vegetation-index case; set it `False` for a wetness or burn-severity
            index that already rises on disturbance.
        maxSegments: Maximum number of segments to fit. Required — Earth Engine
            declares no default.
        spikeThreshold: Threshold for dampening spikes (`0`-`1`); `0.9` keeps
            all but the sharpest.
        vertexCountOvershoot: How many extra vertices the first model may carry
            before pruning back to `maxSegments + 1`.
        preventOneYearRecovery: Whether to forbid segments that recover within a
            single year.
        recoveryThreshold: Reject a fit whose fastest recovery is quicker than
            `1 / recoveryThreshold` years.
        pvalThreshold: The p-value a model's F-statistic must clear to be kept.
        bestModelProportion: Keep the model with the most vertices whose p-value
            is within this proportion of the lowest p-value found.
        minObservationsNeeded: Minimum valid observations a pixel needs to be
            fitted.

    Examples:
        - The documented defaults are visible and validated at construction:
            ```python
            >>> lt = LandTrendr(maxSegments=6)
            >>> (lt.spikeThreshold, lt.vertexCountOvershoot, lt.recoveryThreshold)
            (0.9, 3, 0.25)
            >>> (lt.pvalThreshold, lt.bestModelProportion, lt.minObservationsNeeded)
            (0.1, 0.75, 6)
            >>> lt.invert
            True

            ```
        - `maxSegments` is required and must be at least one:
            ```python
            >>> LandTrendr(maxSegments=0)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for LandTrendr
            ...

            ```
    """

    ALGORITHM: ClassVar[str] = "LandTrendr"
    ARRAY_BAND: ClassVar[str] = "LandTrendr"

    invert: bool = True

    maxSegments: int = Field(ge=1)
    spikeThreshold: float = Field(default=0.9, ge=0, le=1)
    vertexCountOvershoot: int = Field(default=3, ge=0)
    preventOneYearRecovery: bool = False
    recoveryThreshold: float = Field(default=0.25, ge=0, le=1)
    pvalThreshold: float = Field(default=0.1, ge=0, le=1)
    bestModelProportion: float = Field(default=0.75, ge=0, le=1)
    minObservationsNeeded: int = Field(default=6, ge=1)

    def _run(self, time_series: Any) -> Any:
        """Call `ee.Algorithms.TemporalSegmentation.LandTrendr`.

        Args:
            time_series: The oriented input `ee.ImageCollection`.

        Returns:
            The `ee.Image` carrying the `LandTrendr` array band and `rmse`.
        """
        return ee.Algorithms.TemporalSegmentation.LandTrendr(
            timeSeries=time_series,
            maxSegments=self.maxSegments,
            spikeThreshold=self.spikeThreshold,
            vertexCountOvershoot=self.vertexCountOvershoot,
            preventOneYearRecovery=self.preventOneYearRecovery,
            recoveryThreshold=self.recoveryThreshold,
            pvalThreshold=self.pvalThreshold,
            bestModelProportion=self.bestModelProportion,
            minObservationsNeeded=self.minObservationsNeeded,
        )


class Ccdc(TemporalSegmentation):
    """Continuous Change Detection and Classification, with a parameter dict.

    Wraps `ee.Algorithms.TemporalSegmentation.Ccdc`. Unlike LandTrendr, CCDC takes
    a parameter dictionary, carried here as `params` and splatted into the call
    alongside the collection, so a user can copy any key straight from the Earth
    Engine documentation. `invert` defaults to `False`: CCDC detects change in
    either direction, so the disturbance-sign convention LandTrendr needs does
    not apply — but it is still honoured if set, for parity.

    Attributes:
        invert: Negate the series before fitting. Defaults to `False`; CCDC's
            change detection is direction-agnostic, so it rarely matters.
        params: The CCDC parameter dictionary (`breakpointBands`, `tmaskBands`,
            `minObservations`, `chiSquareProbability`, `dateFormat`, ...),
            spelled as Earth Engine spells them.

    Examples:
        - The parameter dict is carried verbatim and `invert` is off by default:
            ```python
            >>> ccdc = Ccdc(params={"breakpointBands": ["B3", "B4", "B5"]})
            >>> ccdc.params["breakpointBands"]
            ['B3', 'B4', 'B5']
            >>> ccdc.invert
            False

            ```
    """

    ALGORITHM: ClassVar[str] = "Ccdc"
    ARRAY_BAND: ClassVar[str] = "tStart"

    invert: bool = False

    params: Mapping[str, Any] = Field(default_factory=dict)

    def _run(self, time_series: Any) -> Any:
        """Call `ee.Algorithms.TemporalSegmentation.Ccdc`.

        Args:
            time_series: The oriented input `ee.ImageCollection`.

        Returns:
            The `ee.Image` of per-segment coefficient and change arrays.
        """
        return ee.Algorithms.TemporalSegmentation.Ccdc(
            collection=time_series, **dict(self.params)
        )
