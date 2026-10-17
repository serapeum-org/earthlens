"""Tests for the clustering and supervised-classification operations."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis._sklearn import sklearn_available
from earthlens.gee.analysis.classify import Classify, TrainingExtractor, TrainingSet
from earthlens.gee.analysis.cluster import Cluster

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)

#: Two columns of low values and two of high ones, so any sane clustering
#: separates them and a classifier trained on one cell of each generalises.
TWO_GROUPS = np.array([[1.0, 1.0, 50.0, 50.0]] * 4, dtype="float32")


def _raster(values: np.ndarray, no_data: float = -9999.0) -> Dataset:
    """Build a one-band raster declaring `no_data`."""
    return Dataset.from_array(
        arr=values.astype("float32"), geo_ref=GEO, no_data_value=no_data
    )


def _labels(ds: Dataset) -> np.ndarray:
    """Read a raster's first band as int64."""
    return np.asarray(ds.read_array()).astype("int64")


def _marks() -> Dataset:
    """A label raster marking one cell of each group, the rest unlabelled."""
    marks = np.full((4, 4), -1.0, dtype="float32")
    marks[0, 0] = 7.0
    marks[0, 3] = 9.0
    return _raster(marks, no_data=-1.0)


def _raise_missing_module(module: str):  # noqa: ANN201
    """Fail an import the way an uninstalled package does."""
    raise ImportError(f"No module named {module!r}")


class TestCluster:
    """Unsupervised per-pixel k-means."""

    def test_two_obvious_groups_get_two_labels(self):
        """Cells with the same value cluster together."""
        labels = _labels(Cluster(n_clusters=2).apply(_raster(TWO_GROUPS)))
        assert labels[0, 0] == labels[0, 1]
        assert labels[0, 0] != labels[0, 3]

    def test_the_labelling_is_reproducible(self):
        """A seeded k-means gives the same numbering on every run."""
        source = _raster(TWO_GROUPS)
        first = _labels(Cluster(n_clusters=2).apply(source))
        second = _labels(Cluster(n_clusters=2).apply(source))
        assert np.array_equal(first, second)

    def test_no_data_is_excluded_rather_than_clustered(self):
        """The sentinel is a number, so a finiteness test would cluster it."""
        values = TWO_GROUPS.copy()
        values[:, 3] = -9999.0
        labels = _labels(Cluster(n_clusters=2).apply(_raster(values)))
        assert labels[0, 3] == -1
        assert set(labels[:, 0].tolist()) != {-1}

    def test_the_no_data_label_is_configurable(self):
        """A caller may need a sentinel that fits their dtype."""
        values = TWO_GROUPS.copy()
        values[:, 3] = -9999.0
        labels = _labels(Cluster(n_clusters=2, no_data_label=99).apply(_raster(values)))
        assert labels[0, 3] == 99

    def test_a_band_subset_is_honoured(self):
        """Clustering on one band of several uses only that band."""
        stack = np.stack([TWO_GROUPS, np.zeros_like(TWO_GROUPS)])
        source = Dataset.from_array(arr=stack, geo_ref=GEO, no_data_value=-9999.0)
        labels = _labels(Cluster(n_clusters=2, bands=(0,)).apply(source))
        assert labels[0, 0] != labels[0, 3]

    def test_one_cluster_is_refused(self):
        """It would label every cell identically, which is never the intent."""
        with pytest.raises(ValidationError, match="at least 2"):
            Cluster(n_clusters=1)

    def test_an_all_no_data_raster_says_there_is_nothing_to_fit(self):
        """Better a named error than an opaque sklearn failure."""
        empty = _raster(np.full((4, 4), -9999.0, dtype="float32"))
        with pytest.raises(ValueError, match="nothing to cluster"):
            Cluster(n_clusters=2).apply(empty)

    def test_the_output_keeps_the_source_grid(self):
        """A label raster sits on the same footprint."""
        source = _raster(TWO_GROUPS)
        result = Cluster(n_clusters=2).apply(source)
        assert (result.rows, result.columns, result.epsg) == (
            source.rows,
            source.columns,
            source.epsg,
        )


class TestTrainingExtractor:
    """Turning a raster plus a label raster into a training pair."""

    def test_it_takes_only_the_labelled_cells(self):
        """Two marks give two samples, not sixteen."""
        training = TrainingExtractor().extract(_raster(TWO_GROUPS), _marks())
        assert len(training) == 2

    def test_the_features_line_up_with_the_labels(self):
        """Sample order must pair each row with its own label."""
        training = TrainingExtractor().extract(_raster(TWO_GROUPS), _marks())
        pairs = dict(
            zip(
                np.asarray(training.labels).tolist(),
                np.asarray(training.features)[:, 0].tolist(),
                strict=True,
            )
        )
        assert pairs == {7: 1.0, 9: 50.0}

    def test_a_grid_mismatch_is_refused(self):
        """Labels for a different raster would silently mislabel samples."""
        other = Dataset.from_array(
            arr=np.zeros((2, 2), dtype="float32"), geo_ref=GEO, no_data_value=-1.0
        )
        with pytest.raises(ValueError, match="must share a grid"):
            TrainingExtractor().extract(_raster(TWO_GROUPS), other)

    def test_an_unlabelled_raster_says_there_is_nothing_to_train_on(self):
        """An all-no-data label raster is a mistake worth naming."""
        blank = _raster(np.full((4, 4), -1.0, dtype="float32"), no_data=-1.0)
        with pytest.raises(ValueError, match="nothing to train on"):
            TrainingExtractor().extract(_raster(TWO_GROUPS), blank)


class TestTrainingSet:
    """The value object the extractor returns."""

    def test_mismatched_lengths_are_refused(self):
        """Features and labels must describe the same samples."""
        with pytest.raises(ValidationError, match="same samples"):
            TrainingSet(features=np.zeros((3, 2)), labels=np.zeros(2))

    def test_an_empty_set_is_refused(self):
        """Nothing could be fitted from it."""
        with pytest.raises(ValidationError, match="empty"):
            TrainingSet(features=np.zeros((0, 2)), labels=np.zeros(0))

    def test_one_dimensional_features_are_refused(self):
        """The shape has to be (samples, bands) for sklearn to accept it."""
        with pytest.raises(ValidationError, match="2-D"):
            TrainingSet(features=np.zeros(3), labels=np.zeros(3))


class TestClassify:
    """Supervised per-pixel classification."""

    def test_it_generalises_from_the_marked_cells(self):
        """Trained on one cell of each group, it labels the rest accordingly."""
        source = _raster(TWO_GROUPS)
        training = TrainingExtractor().extract(source, _marks())
        labels = _labels(Classify(training=training).apply(source))
        assert labels[2, 0] == 7
        assert labels[2, 3] == 9

    def test_no_data_cells_are_not_predicted(self):
        """A classifier has no notion of a missing feature."""
        values = TWO_GROUPS.copy()
        source = _raster(values)
        training = TrainingExtractor().extract(source, _marks())
        values[3, 3] = -9999.0
        labels = _labels(Classify(training=training).apply(_raster(values)))
        assert labels[3, 3] == -1

    def test_a_band_count_mismatch_is_refused(self):
        """Applying a one-band model to two bands would misalign features."""
        source = _raster(TWO_GROUPS)
        training = TrainingExtractor().extract(source, _marks())
        stack = Dataset.from_array(
            arr=np.stack([TWO_GROUPS, TWO_GROUPS]), geo_ref=GEO, no_data_value=-9999.0
        )
        with pytest.raises(ValueError, match="would not line up"):
            Classify(training=training).apply(stack)

    def test_it_is_reproducible(self):
        """A seeded forest gives the same map twice."""
        source = _raster(TWO_GROUPS)
        training = TrainingExtractor().extract(source, _marks())
        operation = Classify(training=training)
        assert np.array_equal(
            _labels(operation.apply(source)), _labels(operation.apply(source))
        )

    def test_it_is_frozen_despite_holding_arrays(self):
        """The training pair travels as part of the value object."""
        training = TrainingExtractor().extract(_raster(TWO_GROUPS), _marks())
        operation = Classify(training=training)
        with pytest.raises(ValidationError, match="frozen"):
            operation.n_estimators = 10

    def test_it_composes_with_a_sieve(self):
        """Classify then sieve is the motivating sequence for patch cleanup."""
        from earthlens.gee.analysis.patches import Sieve

        source = _raster(TWO_GROUPS)
        training = TrainingExtractor().extract(source, _marks())
        chain = Classify(training=training) >> Sieve(min_patch_cells=2)
        assert chain.apply(source).band_count == 1


class TestTheGuardedImport:
    """scikit-learn is reached through one seam, like the pyramids-eo reader."""

    def test_the_availability_check_finds_the_installed_package(self):
        """scikit-learn is on the `[gee]` extra, so it is importable here.

        The previous version of this test imported `importlib.util` in its own
        body, which guaranteed the attribute the implementation reached for
        existed by the time the assertion ran - so it could not fail even
        though the implementation raised `AttributeError` on a clean
        interpreter.
        """
        assert sklearn_available() is True

    def test_the_availability_check_reports_a_missing_package(self, monkeypatch):
        """The false branch was never exercised, so it is pinned here."""
        import earthlens.gee.analysis._sklearn as guard

        monkeypatch.setattr(guard, "find_spec", lambda name: None)
        assert guard.sklearn_available() is False

    def test_a_missing_install_names_the_extra(self, monkeypatch):
        """The error should say what to install, not fail on an attribute."""
        import earthlens.gee.analysis._sklearn as guard

        monkeypatch.setattr(guard.importlib, "import_module", _raise_missing_module)
        with pytest.raises(ImportError, match=r"pip install earthlens\[gee\]"):
            guard.import_cluster()
        with pytest.raises(ImportError, match=r"pip install earthlens\[gee\]"):
            guard.import_ensemble()
