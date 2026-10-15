"""Unsupervised per-pixel clustering, the client-side `ee.Clusterer` analogue.

k-means over the band values of each cell: the usual first pass at
segmentation or land cover when there are no labels to train on.

This runs on scikit-learn rather than delegating to pyramids. `Dataset.cluster`
looks like the obvious home, but it is not k-means - it groups connected values
between two bounds, which is connected-component labelling and is what
`patches.ConnectedComponents` uses it for. There is no pyramids k-means to call.

No-data cells are not clustered. They cannot be: k-means has no notion of a
missing feature, and imputing one would invent land cover. They are excluded
from the fit and written back as `no_data_label`, which defaults to `-1` so a
reader can tell "not classified" from cluster 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from pydantic import field_validator

from earthlens.gee.analysis._arrays import read_masked, result_like
from earthlens.gee.analysis._sklearn import import_cluster
from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["Cluster"]


class Cluster(RasterOperation):
    """Group cells into `n_clusters` classes by their band values.

    Every band is a feature unless `bands` names a subset, so a six-band
    raster clusters in six dimensions. `random_state` is fixed by default
    because an unseeded k-means gives a different labelling on every run, and a
    land-cover map that renumbers itself between runs is not comparable.

    Examples:
        - Two obvious groups come back as two labels:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> values = np.array([[1.0, 1.0, 50.0, 50.0]] * 4, dtype="float32")
            >>> raster = Dataset.from_array(
            ...     arr=values,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=-9999.0,
            ... )
            >>> labels = np.asarray(Cluster(n_clusters=2)(raster).read_array())
            >>> int(labels[0, 0]) == int(labels[0, 1])
            True
            >>> int(labels[0, 0]) == int(labels[0, 3])
            False

            ```
    """

    n_clusters: int = 5
    bands: tuple[int, ...] | None = None
    random_state: int | None = 0
    no_data_label: int = -1
    dtype: str = "int32"

    @field_validator("n_clusters")
    @classmethod
    def _at_least_two(cls, value: int) -> int:
        """Reject a clustering that cannot separate anything.

        Args:
            value: The configured cluster count.

        Returns:
            The count unchanged when usable.

        Raises:
            ValueError: If fewer than two clusters, which labels every cell the
                same and is never what a caller means.
        """
        if value < 2:
            raise ValueError(
                f"n_clusters must be at least 2, got {value}; one cluster labels "
                "every cell identically"
            )
        return value

    def apply(self, ds: Dataset) -> Dataset:
        """Return a raster of cluster labels.

        Args:
            ds: The source raster, whose bands are the features.

        Returns:
            A new single-band raster of labels in `[0, n_clusters)`, with
            no-data cells carrying `no_data_label`.

        Raises:
            ImportError: If scikit-learn is not installed.
            ValueError: If no cell has a value in every feature band, so there
                is nothing to fit.
        """
        indices = self.bands if self.bands is not None else tuple(range(ds.band_count))
        # A masked read, not `isfinite`: the sentinel is a number like -9999,
        # so a finiteness test treats it as data and k-means hands it a cluster
        # of its own. The mask comes from the band's declared no-data value.
        features = np.stack([read_masked(ds, index) for index in indices])
        rows, columns = features.shape[1], features.shape[2]
        flat = features.reshape(len(indices), rows * columns).T
        usable = np.isfinite(flat).all(axis=1)
        if not usable.any():
            raise ValueError(
                f"{self.name} has no cell with a value in all {len(indices)} "
                "feature band(s), so there is nothing to cluster"
            )
        kmeans = import_cluster().KMeans(
            n_clusters=self.n_clusters, random_state=self.random_state, n_init="auto"
        )
        labels = np.full(rows * columns, self.no_data_label, dtype="int64")
        labels[usable] = kmeans.fit_predict(flat[usable])
        # The result declares `no_data_label`, not the source's sentinel: a
        # label raster's voids are the cells that were not clustered.
        return result_like(
            ds,
            labels.reshape(rows, columns),
            dtype=self.dtype,
            no_data=self.no_data_label,
        )
