"""Earth Engine temporal-extent discovery, with its process-shared cache.

When a catalog entry has no curated `end_date` (an open-ended collection) and
`GEE(discover_extent=True)` was asked for, the backend learns the real window by
querying Earth Engine for the min/max of `system:time_start`. That is a 2-5 s
`reduceColumns(minMax)` round trip, far too expensive to repeat for a second
`GEE(...)` against the same asset, so the result is cached per asset id.

`ExtentDiscovery` owns that query and that cache as one object, replacing the
module-global dict plus two bound methods and a free `clear_extent_cache` the
backend used to carry. The cache must stay **process-shared** — the whole point
is that a second instance against the same asset does not re-pay the round trip —
so a module-level `_EXTENT_DISCOVERY` singleton holds it and the backend
references that; a per-instance cache would be a regression. `clear_extent_cache`
stays as a thin shim delegating to the singleton so existing imports keep working.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import ee
from loguru import logger

__all__ = ["ExtentDiscovery", "clear_extent_cache"]


def _ms_to_naive_utc(milliseconds: float) -> dt.datetime:
    """Convert Earth Engine epoch milliseconds to a naive UTC datetime.

    Args:
        milliseconds: Milliseconds since the Unix epoch, as `system:time_start`
            reports them.

    Returns:
        The instant as a naive (`tzinfo`-free) UTC datetime, matching the rest of
        the extent path.
    """
    return dt.datetime.fromtimestamp(milliseconds / 1000.0, tz=dt.UTC).replace(
        tzinfo=None
    )


class ExtentDiscovery:
    """Discovers (and caches) a collection's `system:time_start` min/max via EE.

    Attributes:
        ee_module: The Earth Engine module the query runs against; injectable so
            a test can stand in a fake without patching a module global.

    Examples:
        - The cache is per asset: a second lookup reuses the first result rather
          than re-issuing the round trip:
            ```python
            >>> class _Reduced:
            ...     def getInfo(self):
            ...         return {"min": 0, "max": 86_400_000}
            >>> class _Collection:
            ...     count = 0
            ...     def __init__(self, asset_id):
            ...         type(self).count += 1
            ...     def reduceColumns(self, reducer, properties):
            ...         return _Reduced()
            >>> import types
            >>> fake = types.SimpleNamespace(
            ...     ImageCollection=_Collection,
            ...     Reducer=types.SimpleNamespace(minMax=lambda: "minMax"),
            ... )
            >>> disc = ExtentDiscovery(ee_module=fake)
            >>> disc.cached("A/B", enabled=True)[0]
            datetime.datetime(1970, 1, 1, 0, 0)
            >>> disc.cached("A/B", enabled=True)
            (datetime.datetime(1970, 1, 1, 0, 0), datetime.datetime(1970, 1, 2, 0, 0))
            >>> _Collection.count
            1

            ```
    """

    def __init__(self, ee_module: Any = None) -> None:
        """Bind the Earth Engine module and start with an empty cache.

        Args:
            ee_module: The module to query; defaults to the real `ee`.
        """
        self.ee_module = ee_module if ee_module is not None else ee
        self._cache: dict[str, tuple[dt.datetime | None, dt.datetime | None]] = {}

    def discover(self, asset_id: str) -> tuple[dt.datetime | None, dt.datetime | None]:
        """Query a collection's actual `system:time_start` min/max via Earth Engine.

        Issues one `reduceColumns(ee.Reducer.minMax(), ["system:time_start"])
        .getInfo()` round-trip. On any EE-side failure (network, missing
        property, image-typed asset) it downgrades to `(None, None)` with a
        warning, so the caller falls back to the catalog values or `now()`.

        Args:
            asset_id: The Earth Engine collection id to query.

        Returns:
            `(min_dt, max_dt)` as naive UTC datetimes, or `(None, None)` if the
            query failed or the collection has no time-stamped images.
        """
        try:
            collection = self.ee_module.ImageCollection(asset_id)
            result = (
                collection.reduceColumns(
                    self.ee_module.Reducer.minMax(), ["system:time_start"]
                ).getInfo()
                or {}
            )
        except Exception as exc:  # noqa: BLE001 - downgrade EE errors to a warning
            logger.warning(
                f"discover_extent: reduceColumns(minMax) failed for "
                f"{asset_id}: {type(exc).__name__}: {exc}; "
                "falling back to catalog / now()."
            )
            result = {}

        min_ms = result.get("min")
        max_ms = result.get("max")
        if min_ms is None or max_ms is None:
            extent: tuple[dt.datetime | None, dt.datetime | None] = (None, None)
        else:
            extent = (_ms_to_naive_utc(min_ms), _ms_to_naive_utc(max_ms))
        return extent

    def cached(
        self, asset_id: str, *, enabled: bool
    ) -> tuple[dt.datetime | None, dt.datetime | None]:
        """Return the asset's extent, discovering and caching it once.

        A `(None, None)` result is cached too, so a persistently failing or
        time-less asset is not re-queried every bucket.

        Args:
            asset_id: The Earth Engine collection id.
            enabled: Whether discovery is on (`GEE(discover_extent=...)`); when
                `False` no query is made and `(None, None)` is returned.

        Returns:
            `(min_dt, max_dt)` as naive UTC datetimes, each possibly `None`.
        """
        if not enabled:
            extent: tuple[dt.datetime | None, dt.datetime | None] = (None, None)
        else:
            if asset_id not in self._cache:
                self._cache[asset_id] = self.discover(asset_id)
            extent = self._cache[asset_id]
        return extent

    def clear(self) -> None:
        """Forget every cached extent, forcing the next lookup to re-query."""
        self._cache.clear()


#: Process-shared singleton: the extent cache is deliberately shared across every
#: `GEE(...)` so a second instance against the same asset does not re-pay the
#: 2-5 s round trip. A per-instance cache would be a regression.
_EXTENT_DISCOVERY = ExtentDiscovery()


def clear_extent_cache() -> None:
    """Forget every cached EE-discovered temporal extent.

    A thin shim over the shared `_EXTENT_DISCOVERY` singleton, mirroring
    `earthlens.gee.catalog.clear_catalog_cache`. Primarily useful in tests that
    need a fresh cache between runs (or when the EE-side data was updated and the
    in-process cache has gone stale).
    """
    _EXTENT_DISCOVERY.clear()
