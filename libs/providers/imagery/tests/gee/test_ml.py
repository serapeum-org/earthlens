"""Tests for the sklearn-to-Earth-Engine `ClassifierCodec`."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("ee")
pytest.importorskip("sklearn")

from pydantic import ValidationError  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # noqa: E402
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor  # noqa: E402

from earthlens.gee import ml as ml_module  # noqa: E402
from earthlens.gee.analysis.classify import Classify, TrainingSet  # noqa: E402
from earthlens.gee.ml import ClassifierCodec, TreeEnsemble  # noqa: E402

_HAND_TREE = (
    "1) root 4 9999 9999 (0.5)\n"
    "  2) B4 <= 0.500000 2 0.0000 7 *\n"
    "  3) B4 > 0.500000 2 0.0000 9 *\n"
)

_DEEP_TREE = (
    "1) root 6 9999 9999 (1.0)\n"
    "  2) B4 <= 0.500000 4 0.5000 7\n"
    "    4) B8 <= 0.250000 2 0.0000 3 *\n"
    "    5) B8 > 0.250000 2 0.0000 7 *\n"
    "  3) B4 > 0.500000 2 0.0000 9 *\n"
)


class _FakeClassifierFactory:
    """Stands in for `ee.Classifier`, recording the factory calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def decisionTree(self, tree_string):  # noqa: N802 - EE method name
        self.calls.append(("decisionTree", tree_string))
        return SimpleNamespace(args={"treeString": tree_string})

    def decisionTreeEnsemble(self, tree_strings):  # noqa: N802 - EE method name
        self.calls.append(("decisionTreeEnsemble", tree_strings))
        return SimpleNamespace(args={"treeStrings": tree_strings})


@pytest.fixture
def fake_classifier(monkeypatch):
    """Patch `ee.Classifier` with the recording factory."""
    fake = _FakeClassifierFactory()
    monkeypatch.setattr(ml_module.ee, "Classifier", fake)
    return fake


def _separable_training():
    """A three-class set split by axis-aligned thresholds, labels 3/7/9."""
    rng = np.random.default_rng(42)
    x = rng.uniform(0, 1, size=(200, 2))
    y = np.where(x[:, 0] > 0.5, 9, np.where(x[:, 1] > 0.5, 7, 3))
    return x, y


#: Probe points on which the depth-limited forest below splits the two voting
#: rules: sklearn averages probabilities, Earth Engine hard-votes.
_DIVERGING_SAMPLES = [(0.2, 0.2), (0.2, 0.8), (0.8, 0.2), (0.9, 0.9)]


def _divergent_forest():
    """A depth-4 forest whose trees disagree, so the voting rules differ."""
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 1, size=(300, 2))
    y = np.where(x[:, 0] > 0.5, 9, np.where(x[:, 1] > 0.5, 7, 3))
    return RandomForestClassifier(n_estimators=3, max_depth=4, random_state=0).fit(x, y)


def _hard_vote(forest, samples):
    """Majority-vote the forest's members by label, ties to the smallest.

    Written out with `Counter` rather than calling `ml_module._majority`, so
    the parity assertion compares the implementation against an independent
    statement of the rule instead of against itself.
    """
    labels = np.asarray(forest.classes_)
    per_tree = np.column_stack(
        [labels[member.predict(samples).astype(int)] for member in forest.estimators_]
    )
    voted = []
    for row in per_tree:
        counts = Counter(float(vote) for vote in row)
        best = max(counts.values())
        voted.append(min(label for label, seen in counts.items() if seen == best))
    return np.asarray(voted)


class TestCodecValidation:
    """The codec's band-name mapping is validated at construction."""

    def test_it_is_frozen_with_a_default_label(self):
        """The codec keeps its names and defaults the EE output band name."""
        codec = ClassifierCodec(feature_names=("B4", "B8"))
        assert codec.feature_names == ("B4", "B8")
        assert codec.label_name == "classification"
        with pytest.raises(ValidationError):
            codec.label_name = "other"

    def test_empty_feature_names_are_refused(self):
        """A codec with no band names cannot encode any tree."""
        with pytest.raises(ValidationError, match="empty"):
            ClassifierCodec(feature_names=())

    def test_a_whitespace_band_name_is_refused(self):
        """The tree-string format is space-delimited, so names carry no spaces."""
        with pytest.raises(ValidationError, match="whitespace"):
            ClassifierCodec(feature_names=("B4", "B 8"))

    def test_duplicate_band_names_are_refused(self):
        """Two feature columns cannot share one EE band name."""
        with pytest.raises(ValidationError, match="unique"):
            ClassifierCodec(feature_names=("B4", "B4"))


class TestTreeStrings:
    """The sklearn walk emits geemap-format tree strings."""

    def test_a_known_stump_encodes_exactly(self):
        """A one-split tree produces the documented line layout verbatim."""
        model = DecisionTreeClassifier(random_state=0).fit([[0.0], [1.0]], [0, 1])
        (string,) = ClassifierCodec(feature_names=("B4",))._rf_to_strings(model)
        assert string == (
            "1) root 2 9999 9999 (0.5)\n"
            "  2) B4 <= 0.500000 1 0.0000 0 *\n"
            "  3) B4 > 0.500000 1 0.0000 1 *\n"
        )

    def test_class_labels_survive_the_encoding(self):
        """Leaf values are the estimator's own class labels, not indices."""
        model = DecisionTreeClassifier(random_state=0).fit([[0.0], [1.0]], [7, 9])
        (string,) = ClassifierCodec(feature_names=("B4",))._rf_to_strings(model)
        branches = ml_module._parse_tree(string)
        assert branches[2].value == 7.0
        assert branches[2].is_leaf
        assert branches[3].value == 9.0
        assert branches[3].is_leaf
        assert branches[2].threshold == pytest.approx(0.5)

    def test_every_forest_member_parses(self):
        """Each ensemble member's string parses back into branches."""
        x, y = _separable_training()
        model = RandomForestClassifier(n_estimators=3, random_state=0).fit(x, y)
        strings = ClassifierCodec(feature_names=("B4", "B8"))._rf_to_strings(model)
        assert len(strings) == 3
        for string in strings:
            branches = ml_module._parse_tree(string)
            assert {2, 3} <= set(branches)

    def test_a_single_leaf_tree_is_refused(self):
        """A stump with no split encodes no decision rule."""
        model = DecisionTreeClassifier(random_state=0).fit([[0.0], [1.0]], [1, 1])
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(ValueError, match="single-leaf"):
            codec._rf_to_strings(model)

    def test_a_regressor_is_refused(self):
        """Only classifiers translate; regression output mode is out of scope."""
        model = DecisionTreeRegressor(random_state=0).fit([[0.0], [1.0]], [0.1, 0.9])
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(ValueError, match="classifier"):
            codec._rf_to_strings(model)

    def test_an_unfitted_estimator_is_refused(self):
        """An unfitted forest has no trees to encode."""
        codec = ClassifierCodec(feature_names=("B4",))
        unfitted = RandomForestClassifier()
        with pytest.raises(ValueError, match="classifier"):
            codec._rf_to_strings(unfitted)

    def test_a_string_labelled_forest_is_refused(self):
        """Land-cover names cannot ride the space-delimited numeric format."""
        rng = np.random.default_rng(0)
        x = rng.uniform(0, 1, size=(60, 2))
        y = np.where(x[:, 0] > 0.5, "water", "land")
        model = RandomForestClassifier(n_estimators=2, max_depth=3, random_state=0).fit(
            x, y
        )
        codec = ClassifierCodec(feature_names=("B4", "B8"))
        with pytest.raises(ValueError, match="numeric class labels"):
            codec._rf_to_strings(model)

    def test_the_refusal_names_the_remedy(self):
        """The message points at integer encoding rather than the int() error."""
        model = DecisionTreeClassifier(random_state=0).fit(
            [[0.0], [1.0]], ["land", "water"]
        )
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(ValueError, match="LabelEncoder"):
            codec._rf_to_strings(model)

    def test_boolean_labels_are_accepted(self):
        """A two-class boolean target encodes as 0 / 1."""
        model = DecisionTreeClassifier(random_state=0).fit(
            [[0.0], [1.0]], [False, True]
        )
        (string,) = ClassifierCodec(feature_names=("B4",))._rf_to_strings(model)
        branches = ml_module._parse_tree(string)
        assert (branches[2].value, branches[3].value) == (0.0, 1.0)

    def test_integral_float_labels_are_accepted(self):
        """Float labels that are whole numbers encode without loss."""
        model = DecisionTreeClassifier(random_state=0).fit([[0.0], [1.0]], [7.0, 9.0])
        (string,) = ClassifierCodec(feature_names=("B4",))._rf_to_strings(model)
        branches = ml_module._parse_tree(string)
        assert (branches[2].value, branches[3].value) == (7.0, 9.0)

    def test_a_feature_count_mismatch_is_refused(self):
        """The codec's names must line up with the fitted feature count."""
        x, y = _separable_training()
        model = RandomForestClassifier(n_estimators=2, random_state=0).fit(x, y)
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(ValueError, match="would not line up"):
            codec._rf_to_strings(model)

    def test_an_ensemble_without_members_is_refused(self):
        """A classifier whose estimators_ is empty has nothing to encode."""
        forest = SimpleNamespace(classes_=np.array([0, 1]), estimators_=[])
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(ValueError, match="no fitted trees"):
            codec._rf_to_strings(forest)

    def test_a_foreign_ensemble_member_is_refused(self):
        """An ensemble of non-classification trees is named and rejected."""
        forest = SimpleNamespace(
            classes_=np.array([0, 1]),
            estimators_=[DecisionTreeRegressor()],
            n_features_in_=1,
        )
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(ValueError, match="member type"):
            codec._rf_to_strings(forest)


class TestToEe:
    """`to_ee` builds the server-side classifier through `ee.Classifier`."""

    def test_a_forest_becomes_a_decision_tree_ensemble(self, fake_classifier):
        """n_estimators=3 yields decisionTreeEnsemble over three strings."""
        x, y = _separable_training()
        model = RandomForestClassifier(n_estimators=3, random_state=0).fit(x, y)
        ClassifierCodec(feature_names=("B4", "B8")).to_ee(model)
        name, strings = fake_classifier.calls[0]
        assert name == "decisionTreeEnsemble"
        assert isinstance(strings, list)
        assert len(strings) == 3
        assert all(s.startswith("1) root ") for s in strings)

    def test_a_single_tree_becomes_a_decision_tree(self, fake_classifier):
        """A bare DecisionTreeClassifier routes to decisionTree."""
        model = DecisionTreeClassifier(random_state=0).fit([[0.0], [1.0]], [0, 1])
        ClassifierCodec(feature_names=("B4",)).to_ee(model)
        name, string = fake_classifier.calls[0]
        assert name == "decisionTree"
        assert string.startswith("1) root ")


class TestFromEe:
    """`from_ee` rebuilds a pure-Python predictor from the tree strings."""

    def test_round_trip_predictions_hard_vote_the_exported_trees(self, fake_classifier):
        """The rebuilt ensemble majority-votes its members, as Earth Engine does."""
        model = _divergent_forest()
        codec = ClassifierCodec(feature_names=("B4", "B8"))
        predictor = codec.from_ee(codec.to_ee(model))
        assert isinstance(predictor, TreeEnsemble)
        assert np.array_equal(
            predictor.predict(_DIVERGING_SAMPLES),
            _hard_vote(model, _DIVERGING_SAMPLES),
        )

    def test_the_hard_vote_can_differ_from_the_forests_probability_average(
        self, fake_classifier
    ):
        """A non-unanimous forest predicts differently from the exported trees."""
        model = _divergent_forest()
        codec = ClassifierCodec(feature_names=("B4", "B8"))
        predictor = codec.from_ee(codec.to_ee(model))
        assert predictor.predict(_DIVERGING_SAMPLES).tolist() == [9.0, 7.0, 9.0, 9.0]
        assert model.predict(_DIVERGING_SAMPLES).tolist() == [3, 7, 9, 9]

    def test_a_single_tree_round_trips_with_exact_parity(self, fake_classifier):
        """One DecisionTreeClassifier has no vote to take, so parity is exact."""
        x, y = _separable_training()
        model = DecisionTreeClassifier(max_depth=4, random_state=0).fit(x, y)
        codec = ClassifierCodec(feature_names=("B4", "B8"))
        predictor = codec.from_ee(codec.to_ee(model))
        axes = np.linspace(0.05, 0.95, 10)
        grid = np.column_stack([g.ravel() for g in np.meshgrid(axes, axes)])
        assert np.array_equal(predictor.predict(grid), model.predict(grid))

    def test_it_accepts_the_strings_directly(self):
        """A list of tree strings (or one string) parses without ee at all."""
        codec = ClassifierCodec(feature_names=("B4", "B8"))
        from_list = codec.from_ee([_HAND_TREE])
        from_str = codec.from_ee(_HAND_TREE)
        assert from_list.predict([[0.2, 0.0]]).tolist() == [7.0]
        assert from_str.predict([[0.9, 0.0]]).tolist() == [9.0]

    def test_it_unwraps_a_single_tree_classifier(self, fake_classifier):
        """The decisionTree-built object's args carry the one string."""
        model = DecisionTreeClassifier(random_state=0).fit([[0.0], [1.0]], [7, 9])
        codec = ClassifierCodec(feature_names=("B4",))
        predictor = codec.from_ee(codec.to_ee(model))
        assert predictor.predict([[0.0], [1.0]]).tolist() == [7.0, 9.0]

    def test_it_unwraps_ee_container_objects(self):
        """ee.List/ee.String-shaped wrappers (client-side) are unwrapped."""
        wrapped = SimpleNamespace(
            args={
                "treeStrings": SimpleNamespace(
                    _list=[SimpleNamespace(_string=_HAND_TREE)]
                )
            }
        )
        codec = ClassifierCodec(feature_names=("B4",))
        assert codec.from_ee(wrapped).predict([[0.2]]).tolist() == [7.0]

    def test_a_server_side_classifier_is_refused_with_the_remedy(self):
        """An object with no client-side strings names explain().getInfo()."""
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(TypeError, match=r"explain\(\).getInfo\(\)"):
            codec.from_ee(42)

    def test_an_args_object_without_trees_is_refused(self):
        """A computed object whose args carry no tree strings is named."""
        codec = ClassifierCodec(feature_names=("B4",))
        treeless = SimpleNamespace(args={"numberOfTrees": 3})
        with pytest.raises(TypeError, match="tree strings"):
            codec.from_ee(treeless)

    def test_a_malformed_line_is_refused(self):
        """An unparseable line raises instead of silently dropping nodes."""
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(ValueError, match="unparseable"):
            codec.from_ee("1) root 2 9999 9999 (0.5)\n  2) B4 ?? 0.5 1 0.0 0 *\n")

    def test_a_branchless_string_is_refused(self):
        """A root line alone holds no decision rule to rebuild."""
        codec = ClassifierCodec(feature_names=("B4",))
        with pytest.raises(ValueError, match="no branch lines"):
            codec.from_ee("1) root 2 9999 9999 (0.5)\n")

    def test_an_unknown_feature_name_is_refused(self):
        """A tree naming a band outside the codec's mapping cannot predict."""
        codec = ClassifierCodec(feature_names=("B2",))
        with pytest.raises(ValidationError, match="B4"):
            codec.from_ee(_HAND_TREE)


class TestTreeEnsemblePredict:
    """The rebuilt predictor walks the parsed branches."""

    def test_it_descends_internal_branches(self):
        """An unstarred branch recurses to the deeper split."""
        predictor = ClassifierCodec(feature_names=("B4", "B8")).from_ee(_DEEP_TREE)
        got = predictor.predict([[0.1, 0.1], [0.1, 0.9], [0.9, 0.1]])
        assert got.tolist() == [3.0, 7.0, 9.0]

    def test_majority_vote_breaks_ties_on_the_smallest_label(self):
        """Equal votes resolve deterministically to the smallest class."""
        left = _HAND_TREE
        right = _HAND_TREE.replace(" 7 *", " 5 *")
        predictor = ClassifierCodec(feature_names=("B4",)).from_ee([left, right])
        assert predictor.predict([[0.2]]).tolist() == [5.0]

    def test_a_wrong_column_count_is_refused(self):
        """The feature matrix must match the codec's band names."""
        predictor = ClassifierCodec(feature_names=("B4",)).from_ee(_HAND_TREE)
        with pytest.raises(ValueError, match="column"):
            predictor.predict([[0.2, 0.3]])

    def test_a_truncated_tree_is_refused_at_predict_time(self):
        """A walk that reaches a missing branch raises instead of looping."""
        truncated = (
            "1) root 6 9999 9999 (1.0)\n"
            "  2) B4 <= 0.500000 4 0.5000 7\n"
            "  3) B4 > 0.500000 2 0.0000 9 *\n"
        )
        predictor = ClassifierCodec(feature_names=("B4",)).from_ee(truncated)
        with pytest.raises(ValueError, match="no branches"):
            predictor.predict([[0.2]])

    def test_an_empty_ensemble_is_refused(self):
        """A predictor with no trees would vote on nothing."""
        with pytest.raises(ValidationError, match="tree"):
            TreeEnsemble(feature_names=("B4",), trees=())


class TestClassifyBridge:
    """The Epic 2 `Classify` operation exports through the codec."""

    def test_classify_exposes_its_fitted_estimator(self):
        """The public accessor fits lazily and returns the same forest."""
        training = TrainingSet(
            features=np.array([[0.0, 1.0], [1.0, 0.0]] * 5),
            labels=np.array([7, 9] * 5),
            bands=(2, 5),
        )
        operation = Classify(training=training, n_estimators=3)
        forest = operation.estimator
        assert forest.predict([[0.0, 1.0]]).tolist() == [7]
        assert operation.estimator is forest

    def test_for_classify_maps_training_bands_to_names(self):
        """The band-index map lines the codec up with the training bands."""
        training = TrainingSet(
            features=np.array([[0.0, 1.0], [1.0, 0.0]]),
            labels=np.array([7, 9]),
            bands=(2, 5),
        )
        operation = Classify(training=training)
        codec = ClassifierCodec.for_classify(operation, {2: "B4", 5: "B8", 9: "B11"})
        assert codec.feature_names == ("B4", "B8")

    def test_for_classify_defaults_to_positional_bands(self):
        """Without recorded band indices, columns map positionally."""
        training = TrainingSet(
            features=np.array([[0.0, 1.0], [1.0, 0.0]]), labels=np.array([7, 9])
        )
        operation = Classify(training=training)
        codec = ClassifierCodec.for_classify(
            operation, {0: "B4", 1: "B8"}, label_name="landcover"
        )
        assert codec.feature_names == ("B4", "B8")
        assert codec.label_name == "landcover"

    def test_for_classify_refuses_a_missing_index(self):
        """A training band absent from the mapping is named in the error."""
        training = TrainingSet(
            features=np.array([[0.0, 1.0], [1.0, 0.0]]),
            labels=np.array([7, 9]),
            bands=(2, 5),
        )
        step = Classify(training=training)
        with pytest.raises(ValueError, match=r"\[5\]"):
            ClassifierCodec.for_classify(step, {2: "B4"})

    def test_a_classify_trained_forest_exports(self, fake_classifier):
        """End to end: Classify -> estimator -> to_ee -> ensemble strings."""
        x, y = _separable_training()
        training = TrainingSet(features=x, labels=y, bands=(0, 1))
        operation = Classify(training=training, n_estimators=2)
        codec = ClassifierCodec.for_classify(operation, {0: "B4", 1: "B8"})
        codec.to_ee(operation.estimator)
        name, strings = fake_classifier.calls[0]
        assert name == "decisionTreeEnsemble"
        assert len(strings) == 2


class TestSklearnSeam:
    """The guarded importer gained the tree module."""

    def test_import_tree_returns_sklearn_tree(self):
        """`import_tree` hands back the module that owns the tree types."""
        from earthlens.gee.analysis._sklearn import import_tree

        assert import_tree().DecisionTreeClassifier is DecisionTreeClassifier
