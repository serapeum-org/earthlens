"""Temporal gap-filling for an Earth Engine collection, before reduction.

Task 3.5's `TimeSeriesSampler` puts interpolation out of scope — right for a
sampler, but it leaves the capability unowned. A cloud-masked series has holes,
and a reducer or a per-point time series over it inherits them. `GapFill` fills
those holes **on the collection, before any reduction**, so the composite or
sample downstream sees a continuous series.

It offers three methods, all driven by one temporal join that pairs each scene
with its nearest valid neighbours *within `max_gap`*:

* `linear` — interpolate between the nearest earlier and later valid pixels.
* `nearest` — take the nearest valid neighbour's pixel.
* `harmonic` — fit one annual harmonic to the whole series and predict the
  missing value (needs a single value `band` to fit).

`max_gap` is a refusal, not a hint: a pixel with no valid neighbour inside the
window stays masked rather than being extrapolated from something too far away.
For `linear` and `nearest` the join's `maxDifference` enforces that directly; for
`harmonic` the prediction is only unmasked where a within-gap neighbour exists,
so none of the three fills across a gap wider than `max_gap`.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
from collections.abc import Callable
from typing import Any, Literal

import ee
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = ["GapFill"]

#: Milliseconds in an average year, for scaling `system:time_start` to the years
#: the annual harmonic's angular frequency is expressed in.
_MILLIS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000

#: Milliseconds per second, the unit Earth Engine stamps `system:time_start` in.
_MILLIS_PER_SECOND = 1000

#: The time field scenes are joined and interpolated on.
_TIME_FIELD = "system:time_start"

#: The band each scene's own timestamp is carried in for the interpolation.
_TIME_BAND = "_time"

#: The harmonic model's independent terms: an intercept and one annual cycle.
_INDEPENDENTS = ("constant", "sin", "cos")


class GapFill(BaseModel):
    """Fill temporal holes in a collection before it is reduced or sampled.

    Attributes:
        method: How a gap is filled — `"linear"` (interpolate between the
            nearest earlier and later valid pixels), `"nearest"` (the nearest
            valid neighbour), or `"harmonic"` (an annual harmonic fit to the
            whole series).
        max_gap: The widest temporal gap that may be bridged. A pixel with no
            valid neighbour within this window stays masked — the method never
            extrapolates past it.
        band: The single value band the harmonic model fits. Required for
            `method="harmonic"`, ignored otherwise.

    Examples:
        - The default is linear interpolation; `max_gap` is required:
            ```python
            >>> import datetime as dt
            >>> GapFill(max_gap=dt.timedelta(days=30)).method
            'linear'

            ```
        - A non-positive `max_gap` is refused at construction:
            ```python
            >>> import datetime as dt
            >>> GapFill(max_gap=dt.timedelta(0))
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for GapFill
            ...

            ```
        - Harmonic gap-filling needs a band to fit:
            ```python
            >>> import datetime as dt
            >>> GapFill(method="harmonic", max_gap=dt.timedelta(days=60))
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for GapFill
            ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["linear", "nearest", "harmonic"] = "linear"
    max_gap: dt.timedelta
    band: str | None = None

    @model_validator(mode="after")
    def _check(self) -> GapFill:
        """Reject a non-positive `max_gap` and a bandless harmonic fill.

        Returns:
            The config unchanged once it is consistent.

        Raises:
            ValueError: If `max_gap` is not positive, or `method="harmonic"` was
                given without the `band` it fits.
        """
        if self.max_gap <= dt.timedelta(0):
            raise ValueError(f"max_gap must be positive, got {self.max_gap}")
        if self.method == "harmonic" and self.band is None:
            raise ValueError("harmonic gap-filling needs band= (the value band to fit)")
        return self

    def _max_gap_millis(self) -> int:
        """Return `max_gap` in milliseconds, the join's difference unit."""
        return int(self.max_gap.total_seconds()) * _MILLIS_PER_SECOND

    def _add_time_band(self, image: Any) -> Any:
        """Carry each scene's timestamp as a `_time` band for interpolation.

        Args:
            image: One `ee.Image` of the collection.

        Returns:
            The image with its `system:time_start` added as the `_time` band.
        """
        image = ee.Image(image)
        return image.addBands(image.metadata(_TIME_FIELD).rename(_TIME_BAND))

    def _join_neighbours(self, source: Any) -> Any:
        """Attach each scene's within-gap earlier and later neighbours.

        Two `saveAll` joins hang a `before` list (nearest earlier scenes first)
        and an `after` list (nearest later scenes first) on every image, both
        bounded by `max_gap` through `ee.Filter.maxDifference`.

        Args:
            source: The time-banded `ee.ImageCollection`.

        Returns:
            The `ee.ImageCollection` with `before` / `after` neighbour lists.
        """
        max_diff = self._max_gap_millis()
        after_filter = ee.Filter.And(
            ee.Filter.greaterThanOrEquals(
                leftField=_TIME_FIELD, rightField=_TIME_FIELD
            ),
            ee.Filter.maxDifference(
                difference=max_diff, leftField=_TIME_FIELD, rightField=_TIME_FIELD
            ),
        )
        before_filter = ee.Filter.And(
            ee.Filter.lessThanOrEquals(leftField=_TIME_FIELD, rightField=_TIME_FIELD),
            ee.Filter.maxDifference(
                difference=max_diff, leftField=_TIME_FIELD, rightField=_TIME_FIELD
            ),
        )
        join_after = ee.Join.saveAll(
            matchesKey="after", ordering=_TIME_FIELD, ascending=True
        )
        join_before = ee.Join.saveAll(
            matchesKey="before", ordering=_TIME_FIELD, ascending=False
        )
        after_joined = ee.ImageCollection(
            join_after.apply(primary=source, secondary=source, condition=after_filter)
        )
        both_joined = ee.ImageCollection(
            join_before.apply(
                primary=after_joined, secondary=source, condition=before_filter
            )
        )
        return both_joined

    def _mosaic(self, image: Any, key: str) -> Any:
        """Mosaic one neighbour list into a single image.

        Args:
            image: The joined `ee.Image` carrying the neighbour lists.
            key: Which list to mosaic — `"before"` or `"after"`.

        Returns:
            The mosaicked `ee.Image` of that neighbour list.
        """
        return ee.ImageCollection.fromImages(image.get(key)).mosaic()

    def _interpolate_image(self, image: Any) -> Any:
        """Linearly interpolate a scene between its within-gap neighbours.

        Args:
            image: The joined `ee.Image` to fill.

        Returns:
            The image with masked pixels filled by the time-weighted mean of the
            nearest earlier and later valid pixels; pixels with a missing
            neighbour stay masked, so nothing is extrapolated past `max_gap`.
        """
        image = ee.Image(image)
        before = self._mosaic(image, "before")
        after = self._mosaic(image, "after")
        this_time = image.select(_TIME_BAND)
        ratio = this_time.subtract(before.select(_TIME_BAND)).divide(
            after.select(_TIME_BAND).subtract(before.select(_TIME_BAND))
        )
        interpolated = after.subtract(before).multiply(ratio).add(before)
        return image.unmask(interpolated).copyProperties(image, [_TIME_FIELD])

    def _nearest_image(self, image: Any) -> Any:
        """Fill a scene from the nearest within-gap neighbour.

        Args:
            image: The joined `ee.Image` to fill.

        Returns:
            The image with masked pixels filled by the nearest valid neighbour
            (earlier preferred, then later); isolated pixels stay masked.
        """
        image = ee.Image(image)
        before = self._mosaic(image, "before")
        after = self._mosaic(image, "after")
        return image.unmask(before).unmask(after).copyProperties(image, [_TIME_FIELD])

    def _add_harmonic_bands(self, image: Any) -> Any:
        """Add the annual-harmonic independent terms to a scene.

        Args:
            image: One `ee.Image` of the collection.

        Returns:
            The image with `constant`, `sin` and `cos` bands of the annual cycle.
        """
        image = ee.Image(image)
        radians = (
            image.metadata(_TIME_FIELD).divide(_MILLIS_PER_YEAR).multiply(2 * math.pi)
        )
        return (
            image.addBands(ee.Image(1).rename("constant"))
            .addBands(radians.sin().rename("sin"))
            .addBands(radians.cos().rename("cos"))
        )

    def _fit_harmonic(self, with_harmonics: Any) -> Any:
        """Fit one annual harmonic to the whole series, per pixel.

        Args:
            with_harmonics: The collection carrying the harmonic bands and the
                value `band`.

        Returns:
            The per-pixel coefficient `ee.Image` flattened to the independent
            term names.
        """
        regression = with_harmonics.select([*_INDEPENDENTS, self.band]).reduce(
            ee.Reducer.linearRegression(numX=len(_INDEPENDENTS), numY=1)
        )
        return (
            regression.select("coefficients")
            .arrayProject([0])
            .arrayFlatten([list(_INDEPENDENTS)])
        )

    def _fill_with_harmonic(self, image: Any, *, coefficients: Any) -> Any:
        """Fill a scene from the harmonic prediction, only within `max_gap`.

        Args:
            image: The joined `ee.Image` to fill.
            coefficients: The fitted harmonic coefficients.

        Returns:
            The image with masked pixels filled by the harmonic prediction,
            masked to where a within-gap neighbour exists so the fit is never
            extrapolated across a gap wider than `max_gap`.
        """
        image = ee.Image(image)
        before = self._mosaic(image, "before")
        after = self._mosaic(image, "after")
        neighbour = before.select(_TIME_BAND).mask().Or(after.select(_TIME_BAND).mask())
        predicted = (
            image.select(list(_INDEPENDENTS))
            .multiply(coefficients)
            .reduce("sum")
            .rename(self.band)
        )
        filled = image.select(self.band).unmask(predicted.updateMask(neighbour))
        return filled.copyProperties(image, [_TIME_FIELD])

    def fill(self, collection: Any) -> Any:
        """Fill the collection's temporal gaps, before any reduction.

        Args:
            collection: The (cloud-masked) input `ee.ImageCollection`.

        Returns:
            An `ee.ImageCollection` of the same scenes with within-gap holes
            filled by the configured method. Run it before compositing or
            sampling — it operates on the collection, not a reduced image.
        """
        prepared = collection.map(self._add_time_band)
        mapper: Callable[[Any], Any]
        if self.method == "harmonic":
            with_harmonics = prepared.map(self._add_harmonic_bands)
            coefficients = self._fit_harmonic(with_harmonics)
            mapper = functools.partial(
                self._fill_with_harmonic, coefficients=coefficients
            )
            joined = self._join_neighbours(with_harmonics)
        else:
            mapper = (
                self._interpolate_image
                if self.method == "linear"
                else self._nearest_image
            )
            joined = self._join_neighbours(prepared)
        return ee.ImageCollection(joined.map(mapper))
