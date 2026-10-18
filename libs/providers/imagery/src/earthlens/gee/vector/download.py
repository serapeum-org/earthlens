"""Pull an `ee.FeatureCollection` into pandas / pyramids, as a configured class.

`FeatureDownloader` is the object-oriented form of the three
`earthlens.gee.io` helpers: it carries the thread-pool and retry budget once
(`pool_size` / `tries` / `backoff`) instead of re-passing them per call, and its
`to_gdf` returns a pyramids `FeatureCollection` (a `GeoDataFrame` subclass) so
the result's `to_file` / `to_parquet` come for free.

The procedural `earthlens.gee.io` functions remain the implementation (their
retry / transient-exception helpers are a monkeypatch seam the io tests rely on);
`FeatureDownloader` delegates to them and only `to_gdf` adds the
`FeatureCollection` wrapping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from pyramids.feature import FeatureCollection

from earthlens.gee import io as _io

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    import ee
    import pandas as pd

__all__ = ["FeatureDownloader", "FeatureDownloaderConfig"]


class FeatureDownloaderConfig(BaseModel):
    """Frozen thread-pool and retry budget a `FeatureDownloader` carries.

    Attributes:
        pool_size: Worker threads for the parallel download path.
        tries: Per-collection retry attempts (including the first). The small
            budget is deliberate - the legacy `gee_utils` `tries=100` masked
            real errors as transient.
        backoff: Multiplier applied to the inter-retry delay.

    Examples:
        - The defaults match the `earthlens.gee.io` helpers:
            ```python
            >>> from earthlens.gee.vector.download import FeatureDownloaderConfig
            >>> cfg = FeatureDownloaderConfig()
            >>> (cfg.pool_size, cfg.tries, cfg.backoff)
            (25, 5, 2.0)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool_size: int = 25
    tries: int = 5
    backoff: float = 2.0


class FeatureDownloader:
    """Download Earth Engine feature collections into frames, with a set budget.

    Examples:
        - A downloader exposes its configured budget:
            ```python
            >>> from earthlens.gee.vector.download import FeatureDownloader
            >>> d = FeatureDownloader(tries=3)
            >>> (d.pool_size, d.tries, d.backoff)
            (25, 3, 2.0)

            ```
    """

    def __init__(
        self, *, pool_size: int = 25, tries: int = 5, backoff: float = 2.0
    ) -> None:
        """Build a downloader.

        Args:
            pool_size: Worker threads for the parallel path. Defaults to `25`.
            tries: Per-collection retry attempts. Defaults to `5`.
            backoff: Inter-retry delay multiplier. Defaults to `2.0`.
        """
        self._config = FeatureDownloaderConfig(
            pool_size=pool_size, tries=tries, backoff=backoff
        )

    @property
    def pool_size(self) -> int:
        """Return the configured worker-thread count."""
        return self._config.pool_size

    @property
    def tries(self) -> int:
        """Return the configured per-collection retry attempts."""
        return self._config.tries

    @property
    def backoff(self) -> float:
        """Return the configured inter-retry delay multiplier."""
        return self._config.backoff

    def to_dataframe(
        self, fc: ee.FeatureCollection, selectors: Iterable[str] | None = None
    ) -> pd.DataFrame:
        """Download one collection to a `DataFrame` via the CSV path.

        Args:
            fc: The Earth Engine `FeatureCollection` to download.
            selectors: Property names to include; `None` fetches everything and
                drops the synthetic `system:index` / `.geo` columns.

        Returns:
            A `DataFrame` with one row per feature.
        """
        return _io.feature_collection_to_dataframe(fc, selectors)

    def to_dataframes(self, fcs: Iterable[ee.FeatureCollection]) -> pd.DataFrame:
        """Download many collections in parallel using this budget.

        Args:
            fcs: An iterable of `ee.FeatureCollection`s.

        Returns:
            One column-concatenated `DataFrame` over the per-collection frames.
        """
        return _io.feature_collections_to_dataframe(
            fcs,
            pool_size=self._config.pool_size,
            tries=self._config.tries,
            backoff=self._config.backoff,
        )

    def to_gdf(
        self, fc: ee.FeatureCollection, crs: int | str = 4326
    ) -> FeatureCollection:
        """Download one collection to a pyramids `FeatureCollection` via `getInfo`.

        Args:
            fc: The Earth Engine `FeatureCollection` to convert (small - `getInfo`
                is capped at ~5000 features / ~10 MB).
            crs: Output CRS; an `int` is read as an EPSG code. Defaults to `4326`.

        Returns:
            A pyramids `FeatureCollection` (a `GeoDataFrame`), so `to_file` /
            `to_parquet` are available on the result.
        """
        return FeatureCollection(_io.feature_collection_to_gdf(fc, crs=crs))
