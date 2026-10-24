"""Supervised per-pixel classification, the client-side `ee.Classifier` analogue.

Two pieces: `TrainingExtractor` turns a labelled raster into the `(X, y)` pair a
scikit-learn estimator wants, and `Classify` fits an estimator on that pair and
predicts over the whole raster.

`Classify` is not a pure `Dataset -> Dataset` function of its configuration
alone - it needs training data - so the training pair is a field on it. That
keeps the operation a value object: given the same raster and the same training
pair it produces the same labels, which is what makes it safe to put in a
chain.

No-data is excluded from both fitting and prediction, for the same reason as in
`cluster`: a classifier has no notion of a missing feature, and filling one in
would invent a class. Unclassifiable cells carry `no_data_label`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator

from earthlens.gee.analysis._arrays import read_masked, result_like
from earthlens.gee.analysis._sklearn import import_ensemble
from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["Classify", "TrainingExtractor", "TrainingSet"]


class TrainingSet(BaseModel):
    """A fitted-on pair of feature rows and their labels, plus their bands.

    Frozen and `arbitrary_types_allowed`, because the arrays are numpy: the
    pair travels as one value so `Classify` can hold it as a field.

    The arrays are sealed read-only on construction. `frozen=True` stops the
    *field* being reassigned but says nothing about the array it points at, so
    `training.features[0, 0] = 999` rewrote a training sample in place.

    `bands` records which band indices the features were read from, so
    applying a model to a different set of bands can be refused by identity
    rather than by counting them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    features: Any
    labels: Any
    bands: tuple[int, ...] | None = None

    @model_validator(mode="after")
    def _shapes_agree(self) -> TrainingSet:
        """Reject a pair whose rows do not line up.

        Returns:
            The training set, once its shapes are usable.

        Raises:
            ValueError: If the feature rows and labels differ in length, if
                there are no samples at all, or if `bands` does not describe
                the feature columns.
        """
        features = np.asarray(self.features)
        labels = np.asarray(self.labels)
        if features.ndim != 2:
            raise ValueError(
                f"features must be 2-D (samples, bands), got shape {features.shape}"
            )
        if len(features) != len(labels):
            raise ValueError(
                f"{len(features)} feature row(s) but {len(labels)} label(s); "
                "they have to describe the same samples"
            )
        if not len(features):
            raise ValueError("the training set is empty, so nothing can be fitted")
        if self.bands is not None and len(self.bands) != features.shape[1]:
            raise ValueError(
                f"bands={self.bands} names {len(self.bands)} band(s) but the "
                f"features have {features.shape[1]} column(s)"
            )
        # Copy before sealing, so a caller's own array does not become
        # read-only underneath them just because they handed it over.
        for name, array in (("features", features), ("labels", labels)):
            sealed = array.copy()
            sealed.setflags(write=False)
            object.__setattr__(self, name, sealed)
        return self

    def __len__(self) -> int:
        """The number of training samples."""
        return len(np.asarray(self.labels))


class TrainingExtractor(BaseModel):
    """Pull `(features, labels)` out of a raster and a label raster.

    The label raster is a classification of the same grid - hand-drawn
    polygons rasterised, or an existing map being extended. Cells whose label
    is no-data, or whose features are incomplete, are left out of the pair
    rather than trained on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bands: tuple[int, ...] | None = None
    label_band: int = 0

    def extract(self, ds: Dataset, labels: Dataset) -> TrainingSet:
        """Return the training pair for the labelled cells.

        Args:
            ds: The feature raster.
            labels: A raster of class labels on the same grid.

        Returns:
            A `TrainingSet` holding one row per labelled, fully-observed cell.

        Raises:
            ValueError: If the two rasters differ in shape, or no cell is both
                labelled and fully observed.
        """
        if (ds.rows, ds.columns) != (labels.rows, labels.columns):
            raise ValueError(
                f"the feature raster is {ds.rows}x{ds.columns} but the label "
                f"raster is {labels.rows}x{labels.columns}; they must share a grid"
            )
        # Shape alone is not the grid: two rasters of equal size in different
        # CRSs, or at different origins, pair every feature row with a label
        # from somewhere else entirely.
        if ds.epsg != labels.epsg:
            raise ValueError(
                f"the feature raster is EPSG:{ds.epsg} and the label raster is "
                f"EPSG:{labels.epsg}; labels would be matched to the wrong cells"
            )
        if tuple(ds.geotransform) != tuple(labels.geotransform):
            raise ValueError(
                "the two rasters are the same shape but have different "
                f"geotransforms ({tuple(ds.geotransform)} vs "
                f"{tuple(labels.geotransform)}), so their cells do not coincide"
            )
        indices = self.bands if self.bands is not None else tuple(range(ds.band_count))
        features = np.stack([read_masked(ds, index) for index in indices])
        flat_features = features.reshape(len(indices), -1).T
        flat_labels = read_masked(labels, self.label_band).ravel()
        labelled = np.isfinite(flat_labels) & np.isfinite(flat_features).all(axis=1)
        if not labelled.any():
            raise ValueError(
                "no cell is both labelled and observed in every feature band, so "
                "there is nothing to train on"
            )
        selected = flat_labels[labelled]
        rounded = np.rint(selected)
        if not np.allclose(selected, rounded):
            offenders = sorted({float(v) for v in selected[selected != rounded]})[:5]
            raise ValueError(
                f"the label raster holds non-integer values {offenders}; class "
                "labels have to be integers, and casting them would silently "
                "merge classes (7.6 and 7.2 would both become class 7)"
            )
        return TrainingSet(
            features=flat_features[labelled],
            labels=rounded.astype("int64"),
            bands=tuple(indices),
        )


class Classify(RasterOperation):
    """Fit a random forest on a training pair and label every cell.

    A random forest is the default because it needs no feature scaling, copes
    with the mixed ranges a multi-band raster has, and does not silently
    degrade when two bands are correlated - which, in remote sensing, they
    usually are.

    Examples:
        - Train on two obvious classes and label the rest of the raster:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> geo = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)
            >>> values = np.array([[1.0, 1.0, 50.0, 50.0]] * 4, dtype="float32")
            >>> raster = Dataset.from_array(arr=values, geo_ref=geo, no_data_value=-9999.0)
            >>> marks = np.full((4, 4), -1.0, dtype="float32")
            >>> marks[0, 0] = 7.0
            >>> marks[0, 3] = 9.0
            >>> label_raster = Dataset.from_array(arr=marks, geo_ref=geo, no_data_value=-1.0)
            >>> training = TrainingExtractor().extract(raster, label_raster)
            >>> len(training)
            2
            >>> out = np.asarray(Classify(training=training)(raster).read_array())
            >>> int(out[2, 0]), int(out[2, 3])
            (7, 9)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    training: TrainingSet
    bands: tuple[int, ...] | None = None
    n_estimators: int = 100
    max_depth: int | None = None
    random_state: int | None = 0
    no_data_label: int = -1
    dtype: str = "int32"

    #: The fitted estimator, built on first use. A private attribute rather
    #: than a field: derived state, so it stays out of equality and
    #: serialisation while the operation itself remains frozen.
    _forest: Any = PrivateAttr(default=None)

    def _fitted(self) -> Any:
        """Return the estimator, fitting it the first time it is needed.

        Refitting on every `apply` cost 0.129 s per call for a 100-tree forest
        even on a 4x4 raster - nearly all of it the fit - so a chain applied
        per time bucket paid for the same model over and over.

        Returns:
            The fitted scikit-learn estimator.

        Raises:
            ImportError: If scikit-learn is not installed.
        """
        if self._forest is None:
            forest = import_ensemble().RandomForestClassifier(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                random_state=self.random_state,
            )
            forest.fit(
                np.asarray(self.training.features), np.asarray(self.training.labels)
            )
            self._forest = forest
        return self._forest

    @property
    def estimator(self) -> Any:
        """The fitted scikit-learn forest behind this operation.

        Fits lazily on first access, through the same path `apply` uses, so
        reading it before any raster has been classified is fine. The primary
        consumer is `earthlens.gee.ml.ClassifierCodec`, which exports the
        forest to a server-side `ee.Classifier`;
        `ClassifierCodec.for_classify` builds the band-index-to-band-name
        mapping from this operation's training bands.

        Returns:
            The fitted `RandomForestClassifier`.

        Raises:
            ImportError: If scikit-learn is not installed.
        """
        return self._fitted()

    def apply(self, ds: Dataset) -> Dataset:
        """Return a raster of predicted class labels.

        Args:
            ds: The raster to classify, with the same bands the training pair
                was extracted from.

        Returns:
            A new single-band raster of labels, with unclassifiable cells
            carrying `no_data_label`.

        Raises:
            ImportError: If scikit-learn is not installed.
            ValueError: If the raster's feature count differs from the training
                pair's, which would silently misalign the bands.
        """
        indices = self.bands if self.bands is not None else tuple(range(ds.band_count))
        expected = np.asarray(self.training.features).shape[1]
        if len(indices) != expected:
            raise ValueError(
                f"{self.name} was trained on {expected} band(s) but is being "
                f"applied to {len(indices)}; the features would not line up"
            )
        trained_on = self.training.bands
        if trained_on is not None and tuple(indices) != tuple(trained_on):
            raise ValueError(
                f"{self.name} was trained on bands {tuple(trained_on)} but is "
                f"being applied to bands {tuple(indices)}; the counts match, so "
                "this would predict from the wrong features instead of failing"
            )
        features = np.stack([read_masked(ds, index) for index in indices])
        rows, columns = features.shape[1], features.shape[2]
        flat = features.reshape(len(indices), rows * columns).T
        usable = np.isfinite(flat).all(axis=1)
        if not usable.any():
            # The same contract as `Cluster`: a raster with nothing readable is
            # a problem with the input, not a map of nothing. Returning an
            # all-sentinel raster here while `Cluster` raised on the identical
            # input made two sibling operations disagree.
            raise ValueError(
                f"{self.name} has no cell with a value in all {len(indices)} "
                "feature band(s), so there is nothing to classify"
            )
        predicted = np.full(rows * columns, self.no_data_label, dtype="int64")
        predicted[usable] = self._fitted().predict(flat[usable])
        # The result declares `no_data_label`, since a class raster's voids
        # are the cells the classifier could not predict.
        return result_like(
            ds,
            predicted.reshape(rows, columns),
            dtype=self.dtype,
            no_data=self.no_data_label,
        )
