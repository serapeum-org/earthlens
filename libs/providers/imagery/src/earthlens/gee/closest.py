"""Pick the Earth Engine image nearest a target date.

"The scene nearest my field campaign" is a constant need and currently a
hand-rolled sort by `system:time_start`. `ClosestImage` is that selection as a
frozen value object: it stamps each scene with its absolute distance to
`target`, optionally drops everything beyond `tolerance`, and returns the
nearest remaining image.

The tolerance is the point of the class: without one, the nearest image is
always *some* image, however distant, which silently returns a scene months away
from a campaign. With a tolerance, an out-of-range target yields an empty
selection (Earth Engine's null `first()`) rather than that distant scene — the
caller gets nothing, which is the honest answer, instead of a wrong something.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import ee
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = ["ClosestImage"]

#: Milliseconds per second, the unit Earth Engine stamps `system:time_start` in.
_MILLIS_PER_SECOND = 1000

#: The property each scene's absolute distance to the target is stamped under.
_DELTA_PROPERTY = "_closest_delta_millis"


class ClosestImage(BaseModel):
    """Select the collection image nearest a target date, within a tolerance.

    Attributes:
        target: The date to find the nearest scene to.
        tolerance: The maximum distance a scene may be from `target` and still
            be selected. `None` (the default) means no bound — the nearest
            scene is returned however far it is. When set, a target with no
            scene inside the window yields an empty selection rather than a
            distant image.

    Examples:
        - The default is an unbounded nearest-scene selection:
            ```python
            >>> import datetime as dt
            >>> ClosestImage(target=dt.date(2024, 6, 1)).tolerance is None
            True

            ```
        - The target is converted to Earth Engine's epoch-millisecond stamp:
            ```python
            >>> import datetime as dt
            >>> ClosestImage(target=dt.date(1970, 1, 2))._target_millis()
            86400000

            ```
        - A non-positive tolerance is refused at construction:
            ```python
            >>> import datetime as dt
            >>> ClosestImage(target=dt.date(2024, 6, 1), tolerance=dt.timedelta(0))
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for ClosestImage
            ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: dt.date
    tolerance: dt.timedelta | None = None

    @model_validator(mode="after")
    def _check_tolerance(self) -> ClosestImage:
        """Reject a zero or negative tolerance.

        Returns:
            The config unchanged once the tolerance is valid.

        Raises:
            ValueError: If `tolerance` is set and is not positive — a window of
                zero width would exclude everything.
        """
        if self.tolerance is not None and self.tolerance <= dt.timedelta(0):
            raise ValueError(
                f"tolerance must be positive, got {self.tolerance}; "
                "pass None for an unbounded nearest-scene search"
            )
        return self

    def _target_millis(self) -> int:
        """Return the target date as Earth Engine's epoch-millisecond stamp.

        Returns:
            Midnight UTC on `target`, in milliseconds since the Unix epoch — the
            same scale as a scene's `system:time_start`.
        """
        moment = dt.datetime(
            self.target.year,
            self.target.month,
            self.target.day,
            tzinfo=dt.UTC,
        )
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
        return int((moment - epoch).total_seconds()) * _MILLIS_PER_SECOND

    def _stamp_delta(self, image: Any) -> Any:
        """Stamp one scene with its absolute distance to the target.

        A bound method rather than a nested function, so the time distance is
        computed server-side against `system:time_start` and carried as a
        sortable property.

        Args:
            image: One `ee.Image` of the collection.

        Returns:
            The image with its distance-to-target in `_DELTA_PROPERTY`.
        """
        delta = (
            ee.Number(image.get("system:time_start"))
            .subtract(self._target_millis())
            .abs()
        )
        return image.set(_DELTA_PROPERTY, delta)

    def select(self, collection: Any) -> Any:
        """Return the collection image nearest the target.

        Stamps every scene with its distance to `target`, drops those beyond
        `tolerance` when one is set, and returns the nearest remaining image.

        Args:
            collection: The input `ee.ImageCollection` to search.

        Returns:
            The nearest `ee.Image`. When a `tolerance` is set and nothing falls
            inside it, this is Earth Engine's null `first()` — an empty
            selection, not a distant scene.
        """
        stamped = collection.map(self._stamp_delta)
        if self.tolerance is not None:
            tolerance_millis = int(self.tolerance.total_seconds()) * _MILLIS_PER_SECOND
            stamped = stamped.filter(ee.Filter.lte(_DELTA_PROPERTY, tolerance_millis))
        return ee.Image(stamped.sort(_DELTA_PROPERTY).first())
