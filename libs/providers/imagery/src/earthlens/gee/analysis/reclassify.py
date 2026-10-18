"""Remap values into classes, and convert a band's physical units.

Two operations that both rewrite values rather than deriving new ones.
`Reclassify` bins continuous values into classes - the land-cover, slope-class
and risk-band case - and `ConvertUnits` delegates to pyramids' conversion table,
which knows what a unit label means.

`Reclassify` is deliberately explicit about what happens to a value no bin
covers: it becomes no-data, not silently the nearest class. A reclassification
that quietly invents a class for an unmatched value is a worse outcome than one
that leaves a hole a reader can see.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

import numpy as np
from pydantic import field_validator, model_validator

from earthlens.gee.analysis._arrays import read_masked, result_like, validate_no_data
from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["UNIT_CONVERSIONS", "ConvertUnits", "Reclassify", "supported_conversions"]


class Reclassify(RasterOperation):
    """Bin a band's values into classes by half-open ranges.

    Each bin is `(lower, upper, class_value)` and matches `lower <= v < upper`,
    so adjacent bins can share an edge without overlapping or leaving a gap.
    Use `-inf` / `inf` for an open end.

    Bins are applied in order, so where two overlap the later one wins:
    `((0, 10, 1), (5, 15, 2))` labels the 5-to-10 overlap 2. That makes "a
    general rule, then an exception" expressible as two bins in that order, and
    it is what the tests pin.

    Examples:
        - Three slope classes from a ramp:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> raster = Dataset.from_array(
            ...     arr=np.array([[0.0, 5.0, 15.0, 40.0]], dtype="float32"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=-9999.0,
            ... )
            >>> flat_to_steep = Reclassify(
            ...     bins=((0.0, 10.0, 1), (10.0, 30.0, 2), (30.0, float("inf"), 3)),
            ... )
            >>> np.asarray(flat_to_steep(raster).read_array()).tolist()
            [[1.0, 1.0, 2.0, 3.0]]

            ```
        - A value no bin covers becomes no-data rather than a guessed class:
            ```python
            >>> only_high = Reclassify(bins=((30.0, float("inf"), 9),))
            >>> out = np.asarray(only_high(raster).read_array())
            >>> float(out[0, 3]), bool(np.isnan(out[0, 0]))
            (9.0, True)

            ```
    """

    bins: tuple[tuple[float, float, float], ...]
    band: int = 0
    dtype: str = "float32"
    no_data: float | int | None = None

    @field_validator("bins")
    @classmethod
    def _non_empty_and_ordered(
        cls, value: tuple[tuple[float, float, float], ...]
    ) -> tuple[tuple[float, float, float], ...]:
        """Reject an empty bin list and any bin whose bounds are inverted.

        Args:
            value: The configured bins.

        Returns:
            The bins unchanged once they are usable.

        Raises:
            ValueError: If there are no bins, or a bin's lower bound is not
                below its upper bound - which would match nothing and is
                always a mistake rather than an intent.
        """
        if not value:
            raise ValueError("bins is empty, so every value would become no-data")
        for lower, upper, class_value in value:
            if lower >= upper:
                raise ValueError(
                    f"bin ({lower}, {upper}, {class_value}) matches nothing: "
                    "the lower bound must be below the upper one"
                )
        return value

    @model_validator(mode="after")
    def _sentinel_fits_the_dtype(self) -> Reclassify:
        """Refuse a sentinel the output dtype cannot store, at construction.

        Returns:
            The operation, once its sentinel is representable.

        Raises:
            ValueError: If `no_data` cannot be stored in `dtype` - NaN in an
                integer band is cast to 0, which collides with a real class, so
                it has to fail here rather than silently at apply time.
        """
        validate_no_data(self.no_data, self.dtype)
        return self

    def apply(self, ds: Dataset) -> Dataset:
        """Return a one-band raster of class values.

        Args:
            ds: The source raster.

        Returns:
            A new single-band raster holding one class value per cell, with
            unmatched cells as no-data.
        """
        # Masked, so an open lower bin cannot sweep the sentinel into the
        # lowest class - `(-inf, 10]` otherwise matches -9999.
        values = read_masked(ds, self.band)
        classified = np.full(values.shape, np.nan, dtype="float64")
        for lower, upper, class_value in self.bins:
            inside = (values >= lower) & (values < upper)
            classified = np.where(inside, class_value, classified)
        return result_like(ds, classified, dtype=self.dtype, no_data=self.no_data)


#: Every conversion this operation performs, as `(source, target) -> (scale,
#: offset)`, applied as `value * scale + offset`.
#:
#: Owned here rather than delegated. `Dataset.convert_units` does the same
#: arithmetic, but it carries a formal deprecation - *"physical value-unit
#: conversion (K/celsius, m s-1/knots, Pa/hPa, m/mm) is domain logic, not a GIS
#: primitive. Keep unit metadata on band_units and convert values in the
#: downstream science-domain consumer"* - and earthlens is that consumer. The
#: table is eight affine pairs, so owning it costs less than depending on a
#: method scheduled for removal; the values are pyramids' own, so a raster
#: converted here matches one converted there exactly.
UNIT_CONVERSIONS: Mapping[tuple[str, str], tuple[float, float]] = MappingProxyType(
    {
        ("K", "celsius"): (1.0, -273.15),
        ("celsius", "K"): (1.0, 273.15),
        ("Pa", "hPa"): (0.01, 0.0),
        ("hPa", "Pa"): (100.0, 0.0),
        ("m s-1", "knots"): (1.943844, 0.0),
        ("knots", "m s-1"): (1.0 / 1.943844, 0.0),
        ("m", "mm"): (1000.0, 0.0),
        ("mm", "m"): (0.001, 0.0),
    }
)


def supported_conversions() -> list[tuple[str, str]]:
    """List the `(source, target)` unit pairs that can be converted.

    Returns:
        The pairs in `UNIT_CONVERSIONS`, sorted, for an error message or a
        caller checking before it asks.
    """
    return sorted(UNIT_CONVERSIONS)


def _converted(values: np.ndarray, source: str, target: str) -> np.ndarray:
    """Apply the affine conversion from `source` to `target` units.

    Args:
        values: The band's values, with voids as `NaN`.
        source: The unit the band declares.
        target: The unit asked for.

    Returns:
        The converted values; `values` unchanged when the units match.

    Raises:
        ValueError: If `source` is empty, or the pair is not in
            `UNIT_CONVERSIONS`.
    """
    if not source:
        raise ValueError(
            f"cannot convert a band with no declared unit to {target!r}; set "
            "band_units on the raster first"
        )
    if source == target:
        return values
    try:
        scale, offset = UNIT_CONVERSIONS[(source, target)]
    except KeyError:
        raise ValueError(
            f"no unit conversion from {source!r} to {target!r}; supported "
            f"pairs: {supported_conversions()}"
        ) from None
    return values * scale + offset


class ConvertUnits(RasterOperation):
    """Convert a band's values to another physical unit.

    The conversion is affine - `value * scale + offset` - and the table is
    `UNIT_CONVERSIONS`. A band declares its current unit in `band_units`; the
    result declares the target. Converting to the unit a band already has is a
    no-op rather than an error.

    This operation owns its arithmetic deliberately. pyramids has the same
    table behind `Dataset.convert_units`, but deprecated it for removal on the
    grounds that physical unit conversion is domain logic rather than a GIS
    primitive, to be done "in the downstream science-domain consumer" - which
    is this package. Delegating would have made a new public operation depend
    on a method scheduled to disappear.

    No-data is preserved as no-data: a sentinel is a number, and `-9999 K` in
    Celsius would be a plausible-looking `-10272.15`.

    Examples:
        - Kelvin to Celsius:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> raster = Dataset.from_array(
            ...     arr=np.array([[300.0, 310.0]], dtype="float64"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ... )
            >>> raster.band_units = ["K"]
            >>> result = ConvertUnits(target="celsius")(raster)
            >>> np.asarray(result.read_array()).round(2).tolist()
            [[26.85, 36.85]]
            >>> result.band_units
            ['celsius']

            ```
    """

    target: str
    band: int | None = None

    @property
    def name(self) -> str:
        """The operation and the unit asked for, for a chain's name."""
        return f"ConvertUnits({self.target})"

    def apply(self, ds: Dataset) -> Dataset:
        """Return a raster whose values are in `target` units.

        Args:
            ds: The source raster, whose bands declare their current units.

        Returns:
            A new raster in the requested unit, declaring it in `band_units`.
            Bands other than `band` are carried through unconverted.

        Raises:
            IndexError: If `band` is outside the raster.
            ValueError: If a band to convert declares no unit, or if the pair
                of units is not one this operation knows.
        """
        from pyramids.base.georeference import GeoReference
        from pyramids.dataset import Dataset as PyramidsDataset

        if self.band is not None and not 0 <= self.band < ds.band_count:
            raise IndexError(
                f"band {self.band} was requested of a raster with "
                f"{ds.band_count} band(s)"
            )
        selected = range(ds.band_count) if self.band is None else [self.band]
        units = list(ds.band_units)
        sentinels = list(ds.no_data_value)
        layers: list[np.ndarray] = []
        for index in range(ds.band_count):
            # Masked, so a sentinel is never put through the conversion: it is
            # a number, and -9999 K reads as a convincing -10272.15 degC.
            values = read_masked(ds, index)
            if index in selected:
                values = _converted(values, units[index], self.target)
                units[index] = self.target
            sentinel = sentinels[index]
            if sentinel is not None:  # pragma: no branch - see below
                # pyramids' `from_array` always declares a sentinel (-9999 by
                # default), so the None case is for a band read from a source
                # that genuinely carried no no-data metadata. Kept as a guard;
                # not reachable from a raster this package constructs.
                values = np.where(np.isnan(values), sentinel, values)
            layers.append(values)
        stacked = layers[0] if ds.band_count == 1 else np.stack(layers)
        result = PyramidsDataset.from_array(
            arr=stacked.astype("float64"),
            geo_ref=GeoReference(geo=ds.geotransform, epsg=ds.epsg),
            no_data_value=list(sentinels),
        )
        result.band_units = units
        return result
