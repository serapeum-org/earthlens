# Extents and warnings

The frozen pydantic value objects that describe *where* and *when* a request covers. A backend's `_create_grid()`
returns a `SpatialExtent` and its `_check_input_dates()` returns a `TemporalExtent`; the base class captures them
into `self.space` and `self.time`.

```python
from earthlens.base import SpatialExtent, TemporalExtent
```

These are values, not dicts — they are immutable, and `SpatialExtent` is comparable and hashable.

`TemporalExtent` is neither, because its `dates` field holds a `DatetimeIndex`: `==` raises `ValueError`
("truth value of an array ... is ambiguous") and `hash()` raises `TypeError`. Compare its scalar fields
(`start_date`, `end_date`, `resolution`) rather than the object.

## `SpatialExtent`

::: earthlens.base.abstractdatasource.SpatialExtent

## `TemporalExtent`

::: earthlens.base.abstractdatasource.TemporalExtent

## `PolygonAoiWarning`

Raised as a warning when a polygon `aoi=` is reduced to its bounding box because the chosen backend cannot clip to
a polygon. Backends advertise the capability through `SUPPORTS_POLYGON_AOI` — see
[Base contracts](contracts.md).

::: earthlens.base.abstractdatasource.PolygonAoiWarning
