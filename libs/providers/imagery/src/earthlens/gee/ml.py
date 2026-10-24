"""`ClassifierCodec`: run a locally trained sklearn tree ensemble server-side.

Earth Engine's `ee.Classifier.decisionTree` / `decisionTreeEnsemble` accept a
plain-text tree format (the R/rpart-style node listing), which means a
decision-tree model fitted *locally* — on samples that never left the machine
— can classify an arbitrarily large image *server-side* with
`image.classify(codec.to_ee(model))`: no retraining, no upload of the
training data, no feature-collection round trip. The bridge is a pure-Python
**port** of geemap's `ml` module (`rf_to_strings` / `tree_to_string` /
`strings_to_classifier`); geemap itself is not a dependency and must not
become one — the walk over the fitted `tree_` arrays is implemented fresh
against that string format.

`to_ee` is the primary, fully supported direction. `from_ee` is the partial,
lossy one: it parses tree strings back into a `TreeEnsemble`, a small
pure-Python predictor (per-tree descent plus majority vote), not a
scikit-learn estimator — the fitted probabilities are gone. It reads the
strings off the classifier `to_ee` built (whose client-side arguments still
carry them) or takes the strings directly; a classifier trained *server-side*
keeps its trees on the server, so fetch them first (e.g.
`classifier.explain().getInfo()`) and pass the strings.

This is the link between Epic 2's client-side `Classify`
(`earthlens.gee.analysis.classify`) and server-side classification: `Classify`
trains on band *indices*, Earth Engine classifies by band *name*, and
`ClassifierCodec.for_classify` is the index-to-name mapping between them,
pairing with the public `Classify.estimator` accessor. scikit-learn ships on
the `[gee]` extra and is imported through the guarded
`earthlens.gee.analysis._sklearn.import_tree` seam, so a missing install is a
readable message.

`image.classify` with the exported classifier is server-side compute: a
request running one is Earth Engine-only (`engine="ee"`), never the EEDAI
reader. The client-side `Classify` counterpart runs after the download and
affects no engine choice.

Examples:
    - Rebuild a predictor from a tree string, with no Earth Engine at all:
        ```python
        >>> from earthlens.gee import ClassifierCodec
        >>> codec = ClassifierCodec(feature_names=("B4", "B8"))
        >>> tree = (
        ...     "1) root 4 9999 9999 (0.5)\\n"
        ...     "  2) B4 <= 0.500000 2 0.0000 7 *\\n"
        ...     "  3) B4 > 0.500000 2 0.0000 9 *\\n"
        ... )
        >>> predictor = codec.from_ee([tree])
        >>> predictor.predict([[0.2, 0.0], [0.9, 0.0]]).tolist()
        [7.0, 9.0]

        ```
    - Export a fitted forest and classify server-side (needs credentials):
        ```python
        >>> import ee  # doctest: +SKIP
        >>> from earthlens.gee import ClassifierCodec  # doctest: +SKIP
        >>> codec = ClassifierCodec(feature_names=("B4", "B8"))  # doctest: +SKIP
        >>> labelled = image.classify(codec.to_ee(model))  # doctest: +SKIP

        ```
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

import ee
import numpy as np
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from earthlens.gee.analysis._sklearn import import_tree

if TYPE_CHECKING:  # pragma: no cover - typing only
    from earthlens.gee.analysis.classify import Classify

__all__ = ["ClassifierCodec", "TreeEnsemble"]

#: One branch line of the rpart-style format `ee.Classifier.decisionTree`
#: accepts: `<heap-number>) <feature> <sign> <threshold> <samples>
#: <criterion> <value>[ *]`, where the trailing `*` marks a leaf.
_BRANCH_RE = re.compile(
    r"^\s*(?P<number>\d+)\) (?P<feature>\S+) (?P<sign><=|>) "
    r"(?P<threshold>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?) "
    r"(?P<samples>\d+) (?P<criterion>-?\d+(?:\.\d+)?) "
    r"(?P<value>-?\d+(?:\.\d+)?)(?P<leaf> \*)?\s*$"
)

#: Every tree string opens with this root line prefix; the root carries no
#: decision, so the parser skips it.
_ROOT_PREFIX = "1) root "


class _Branch(BaseModel):
    """One parsed branch line: the split condition and the node it leads to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int  # the node's binary-heap number (root is 1, unlisted)
    feature: str  # the parent split's band name
    sign: Literal["<=", ">"]  # which side of the split this branch is
    threshold: float  # the parent split's threshold
    value: float  # the node's class label (its subtree's majority)
    is_leaf: bool  # a starred line predicts; an unstarred one splits again


def _tree_to_string(tree: Any, feature_names: Sequence[str], labels: Any) -> str:
    """Encode one fitted sklearn tree into the EE decision-tree format.

    Walks the fitted `tree_` arrays (`children_left` / `children_right` /
    `feature` / `threshold` / `value` / `n_node_samples` / `impurity`)
    pre-order, numbering nodes binary-heap style (root 1, children of `n` at
    `2n` and `2n+1`) the way the rpart text format does.

    Args:
        tree: A fitted estimator's `tree_` attribute.
        feature_names: The band name for each feature column, in order.
        labels: The estimator's `classes_`, mapping leaf argmaxes to labels.

    Returns:
        The tree string, one line per branch, each line newline-terminated.

    Raises:
        ValueError: If the tree is a single leaf, which encodes no decision
            rule.
    """
    if int(tree.node_count) < 2:
        raise ValueError(
            "a single-leaf tree encodes no decision rule, so there is "
            "nothing to export; fit on more than one class"
        )
    lines = [
        f"1) root {int(tree.n_node_samples[0])} 9999 9999 "
        f"({float(tree.impurity.sum())})"
    ]
    _append_branch_lines(tree, feature_names, labels, lines, node=0, number=1, depth=1)
    return "\n".join(lines) + "\n"


def _append_branch_lines(
    tree: Any,
    feature_names: Sequence[str],
    labels: Any,
    lines: list[str],
    *,
    node: int,
    number: int,
    depth: int,
) -> None:
    """Append the two branch lines of split `node`, recursing pre-order.

    Args:
        tree: The fitted `tree_` arrays being walked.
        feature_names: The band name for each feature column, in order.
        labels: The estimator's `classes_`.
        lines: The accumulating output lines, appended in place.
        node: The split node whose branches are being emitted.
        number: That node's binary-heap number.
        depth: The child nodes' depth, which sets the indent.
    """
    feature = feature_names[int(tree.feature[node])]
    threshold = float(tree.threshold[node])
    for sign, child, child_number in (
        ("<=", int(tree.children_left[node]), 2 * number),
        (">", int(tree.children_right[node]), 2 * number + 1),
    ):
        is_leaf = int(tree.children_left[child]) == int(tree.children_right[child])
        value = int(labels[int(np.argmax(tree.value[child][0]))])
        star = " *" if is_leaf else ""
        lines.append(
            f"{'  ' * depth}{child_number}) {feature} {sign} {threshold:.6f} "
            f"{int(tree.n_node_samples[child])} {float(tree.impurity[child]):.4f} "
            f"{value}{star}"
        )
        if not is_leaf:
            _append_branch_lines(
                tree,
                feature_names,
                labels,
                lines,
                node=child,
                number=child_number,
                depth=depth + 1,
            )


def _parse_tree(text: str) -> dict[int, _Branch]:
    """Parse one tree string into its branches, keyed by heap number.

    Args:
        text: A tree string in the format `_tree_to_string` emits (and
            `ee.Classifier.decisionTree` accepts).

    Returns:
        Every branch line as a `_Branch`, keyed by its node number.

    Raises:
        ValueError: If a non-root line does not parse, or no branch line is
            present at all.
    """
    branches: dict[int, _Branch] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_ROOT_PREFIX):
            continue
        match = _BRANCH_RE.match(line)
        if match is None:
            raise ValueError(f"unparseable decision-tree line: {line!r}")
        number = int(match.group("number"))
        branches[number] = _Branch(
            number=number,
            feature=match.group("feature"),
            # The regex alternation admits exactly the two literals.
            sign=cast("Literal['<=', '>']", match.group("sign")),
            threshold=float(match.group("threshold")),
            value=float(match.group("value")),
            is_leaf=match.group("leaf") is not None,
        )
    if not branches:
        raise ValueError(
            "the tree string holds no branch lines, so there is no decision "
            "rule to rebuild"
        )
    return branches


def _predict_tree(
    branches: Mapping[int, _Branch], sample: Mapping[str, float]
) -> float:
    """Descend one parsed tree for one sample and return its leaf value.

    Args:
        branches: The tree's branches keyed by heap number.
        sample: Feature values keyed by band name.

    Returns:
        The class label of the leaf the sample lands in.

    Raises:
        ValueError: If the walk reaches a node whose branches are missing —
            a truncated or hand-mangled tree string.
    """
    number = 1
    result: float | None = None
    while result is None:
        left = branches.get(2 * number)
        right = branches.get(2 * number + 1)
        if left is None or right is None:
            raise ValueError(
                f"the tree string is truncated: node {number} has no branches"
            )
        chosen = left if sample[left.feature] <= left.threshold else right
        if chosen.is_leaf:
            result = chosen.value
        else:
            number = chosen.number
    return result


def _majority(votes: Any) -> float:
    """Return the most common vote, breaking ties on the smallest label.

    Args:
        votes: One row of per-tree class votes.

    Returns:
        The winning class label.
    """
    values, counts = np.unique(np.asarray(votes), return_counts=True)
    return float(values[int(np.argmax(counts))])


def _tree_strings_of(value: Any) -> tuple[str, ...]:
    """Pull client-side tree strings out of what `to_ee` built.

    Handles the shapes a client-side classifier carries: the computed
    object's `args` (`treeStrings` / `treeString`), `ee.List` / `ee.String`
    wrappers (`_list` / `_string`), plain iterables, and the strings
    themselves.

    Args:
        value: A classifier built client-side, a wrapper, an iterable of
            tree strings, or one string.

    Returns:
        The tree strings, in order.

    Raises:
        TypeError: If no client-side strings can be found — a classifier
            trained server-side keeps its trees on the server, so fetch them
            first (e.g. `classifier.explain().getInfo()`) and pass the
            strings.
    """
    result: tuple[str, ...]
    if isinstance(value, str):
        result = (value,)
    elif isinstance(value, (list, tuple)):
        result = tuple(string for item in value for string in _tree_strings_of(item))
    elif isinstance(getattr(value, "args", None), dict):
        payload = value.args.get("treeStrings", value.args.get("treeString"))
        if payload is None:
            raise TypeError(
                f"{type(value).__name__}.args carries no tree strings "
                "(expected 'treeStrings' or 'treeString'); only a classifier "
                "built from decision-tree strings can be decoded client-side"
            )
        result = _tree_strings_of(payload)
    elif hasattr(value, "_list"):
        result = _tree_strings_of(list(value._list))
    elif hasattr(value, "_string"):
        result = _tree_strings_of(value._string)
    else:
        raise TypeError(
            f"cannot read decision-tree strings from {type(value).__name__}; "
            "pass the classifier to_ee built, or the tree strings themselves "
            "— a server-side-trained classifier keeps its trees on the "
            "server, so fetch them first (classifier.explain().getInfo())"
        )
    return result


class TreeEnsemble(BaseModel):
    """A pure-Python predictor rebuilt from decision-tree strings.

    What `ClassifierCodec.from_ee` returns: each tree descends its parsed
    branches to a leaf, and the ensemble takes the majority vote (ties break
    to the smallest label). It is *not* a scikit-learn estimator — the fitted
    probabilities did not survive the string format — but on the same inputs
    it reproduces the hard-class predictions of the forest that was exported.

    Examples:
        - Predict from hand-written branches (columns follow `feature_names`):
            ```python
            >>> from earthlens.gee.ml import TreeEnsemble, _parse_tree
            >>> tree = _parse_tree(
            ...     "1) root 4 9999 9999 (0.5)\\n"
            ...     "  2) B4 <= 0.500000 2 0.0000 7 *\\n"
            ...     "  3) B4 > 0.500000 2 0.0000 9 *\\n"
            ... )
            >>> TreeEnsemble(feature_names=("B4",), trees=(tree,)).predict(
            ...     [[0.4], [0.6]]
            ... ).tolist()
            [7.0, 9.0]

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_names: tuple[str, ...]  # column order of the feature matrix
    trees: tuple[dict[int, _Branch], ...]  # one parsed tree per ensemble member

    @model_validator(mode="after")
    def _consistent(self) -> TreeEnsemble:
        """Refuse an ensemble that could never predict.

        Returns:
            The predictor, once its trees and names line up.

        Raises:
            ValueError: If there is no tree at all, or a branch names a
                feature outside `feature_names`.
        """
        if not self.trees:
            raise ValueError("the ensemble holds no tree, so it cannot vote")
        known = set(self.feature_names)
        unknown = sorted(
            {
                branch.feature
                for tree in self.trees
                for branch in tree.values()
                if branch.feature not in known
            }
        )
        if unknown:
            raise ValueError(
                f"the trees split on feature(s) {unknown} that feature_names "
                f"{self.feature_names} does not carry"
            )
        return self

    def predict(self, features: Any) -> np.ndarray:
        """Majority-vote a class label for each feature row.

        Args:
            features: A 2-D array-like of shape `(samples, features)`, with
                columns ordered like `feature_names`.

        Returns:
            One class label per row, as a float array.

        Raises:
            ValueError: If the matrix is not 2-D with one column per feature
                name, or a tree string turns out truncated mid-descent.
        """
        rows = np.asarray(features, dtype=float)
        if rows.ndim != 2 or rows.shape[1] != len(self.feature_names):
            raise ValueError(
                f"expected a 2-D matrix with {len(self.feature_names)} "
                f"column(s) ordered like {self.feature_names}, got shape "
                f"{rows.shape}"
            )
        votes = [
            [
                _predict_tree(tree, dict(zip(self.feature_names, row)))
                for tree in self.trees
            ]
            for row in rows
        ]
        return np.asarray([_majority(row_votes) for row_votes in votes])


class ClassifierCodec(BaseModel):
    """Bridge a scikit-learn tree ensemble to and from `ee.Classifier`.

    A frozen value object holding the band-index-to-band-name mapping in
    positional form: `feature_names[i]` is the Earth Engine band name for
    feature column `i` of the fitted estimator. `label_name` is the output
    band the server-side `image.classify(classifier, outputName)` call pairs
    with (Earth Engine's default is `classification`).

    `to_ee` encodes a fitted `DecisionTreeClassifier` into
    `ee.Classifier.decisionTree`, and a `RandomForestClassifier` /
    `ExtraTreesClassifier` into `ee.Classifier.decisionTreeEnsemble` — a pure
    Python walk, no server round trip, no geemap dependency. `from_ee` is the
    partial reverse: a `TreeEnsemble` predictor, not a scikit-learn
    estimator. Classifiers only; EE's `REGRESSION` output mode is out of this
    codec's scope.

    Examples:
        - The mapping is validated at construction:
            ```python
            >>> from earthlens.gee import ClassifierCodec
            >>> ClassifierCodec(feature_names=("B4", "B 8"))
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_names: tuple[str, ...]  # EE band name per feature column, in order
    label_name: str = "classification"  # the classify() output band name

    @field_validator("feature_names")
    @classmethod
    def _usable_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse a mapping the space-delimited tree format cannot carry.

        Args:
            value: The parsed `feature_names` input.

        Returns:
            The names unchanged when they are usable.

        Raises:
            ValueError: If the tuple is empty, a name is empty or carries
                whitespace, or two columns share one name.
        """
        if not value:
            raise ValueError(
                "feature_names is empty; name one Earth Engine band per feature column"
            )
        broken = [
            name for name in value if not name or any(ch.isspace() for ch in name)
        ]
        if broken:
            raise ValueError(
                "feature names must be non-empty and whitespace-free (the "
                f"tree-string format is space-delimited), got {broken}"
            )
        if len(set(value)) != len(value):
            raise ValueError(f"feature names must be unique, got {value}")
        return value

    @classmethod
    def for_classify(
        cls,
        operation: Classify,
        band_names: Mapping[int, str],
        *,
        label_name: str = "classification",
    ) -> ClassifierCodec:
        """Build a codec whose feature names mirror a `Classify`'s bands.

        Epic 2's `Classify` trains on band *indices* (its
        `TrainingSet.bands`), while Earth Engine classifies by band *name* —
        this is the index-to-name mapping that lets the locally trained
        forest run server-side: `codec.to_ee(operation.estimator)`.

        Args:
            operation: The fitted-or-not `Classify` whose training bands set
                the column order. Without recorded band indices, columns map
                positionally.
            band_names: Band index to Earth Engine band name, covering every
                training band (extras are ignored).
            label_name: The output band name for the server-side classify.

        Returns:
            The codec, with `feature_names` in the training bands' order.

        Raises:
            ValueError: If a training band index is missing from
                `band_names`.
        """
        features = np.asarray(operation.training.features)
        bands = operation.training.bands
        indices = bands if bands is not None else tuple(range(features.shape[1]))
        missing = [index for index in indices if index not in band_names]
        if missing:
            raise ValueError(
                f"band_names is missing the training band index(es) {missing}; "
                f"the operation trained on bands {tuple(indices)}"
            )
        return cls(
            feature_names=tuple(band_names[index] for index in indices),
            label_name=label_name,
        )

    def to_ee(self, estimator: Any) -> ee.Classifier:
        """Export a fitted sklearn tree classifier to an `ee.Classifier`.

        The primary direction: `image.classify(codec.to_ee(model))` then runs
        the model server-side over any image — Earth Engine-only compute, so
        the request takes `engine="ee"`, never the EEDAI reader.

        Args:
            estimator: A fitted `DecisionTreeClassifier`, or an ensemble of
                them (`RandomForestClassifier` / `ExtraTreesClassifier`).

        Returns:
            `ee.Classifier.decisionTree` for a single tree,
            `ee.Classifier.decisionTreeEnsemble` for an ensemble.

        Raises:
            ImportError: If scikit-learn is not installed (it ships on the
                `[gee]` extra).
            ValueError: If the estimator is not a fitted tree classifier, or
                its feature count does not match `feature_names`.
        """
        tree_mod = import_tree()
        strings = self._rf_to_strings(estimator)
        return (
            ee.Classifier.decisionTree(strings[0])
            if isinstance(estimator, tree_mod.DecisionTreeClassifier)
            else ee.Classifier.decisionTreeEnsemble(list(strings))
        )

    def from_ee(self, classifier: Any) -> TreeEnsemble:
        """Rebuild a pure-Python predictor from a classifier's tree strings.

        The partial, lossy direction: the result is a `TreeEnsemble`
        (per-tree descent, majority vote), not a scikit-learn estimator — the
        fitted probabilities did not survive the string format. Accepts the
        classifier `to_ee` built (whose client-side arguments still carry the
        strings), an iterable of tree strings, or one string; a classifier
        trained *server-side* keeps its trees on the server, so fetch them
        first (e.g. `classifier.explain().getInfo()`) and pass the strings.

        Args:
            classifier: The client-built `ee.Classifier`, or the tree
                strings themselves.

        Returns:
            The rebuilt predictor, with this codec's `feature_names` as its
            column order.

        Raises:
            TypeError: If no client-side tree strings can be found on
                `classifier`.
            ValueError: If a tree string does not parse.
        """
        strings = _tree_strings_of(classifier)
        return TreeEnsemble(
            feature_names=self.feature_names,
            trees=tuple(_parse_tree(text) for text in strings),
        )

    def _rf_to_strings(self, estimator: Any) -> tuple[str, ...]:
        """Encode a fitted sklearn tree classifier into EE tree strings.

        The port of geemap's `rf_to_strings` / `tree_to_string`, implemented
        fresh over the fitted `tree_` arrays.

        Args:
            estimator: The fitted classifier or ensemble.

        Returns:
            One tree string per ensemble member (one for a single tree).

        Raises:
            ImportError: If scikit-learn is not installed.
            ValueError: If the estimator is not a fitted classifier, an
                ensemble member is not a decision-tree classifier, or the
                fitted feature count does not match `feature_names`.
        """
        tree_mod = import_tree()
        if not hasattr(estimator, "classes_"):
            raise ValueError(
                f"{type(estimator).__name__} is not a fitted classifier (no "
                "classes_); fit a DecisionTreeClassifier / "
                "RandomForestClassifier first — regressors are out of this "
                "codec's scope"
            )
        if isinstance(estimator, tree_mod.DecisionTreeClassifier):
            members: tuple[Any, ...] = (estimator,)
        else:
            members = tuple(getattr(estimator, "estimators_", ()))
        if not members:
            raise ValueError(
                f"{type(estimator).__name__} carries no fitted trees "
                "(estimators_ is empty), so there is nothing to encode"
            )
        foreign = sorted(
            {
                type(member).__name__
                for member in members
                if not isinstance(member, tree_mod.DecisionTreeClassifier)
            }
        )
        if foreign:
            raise ValueError(
                "only decision-tree classification ensembles translate; got "
                f"member type(s) {foreign} (DecisionTreeClassifier / "
                "RandomForestClassifier / ExtraTreesClassifier are supported)"
            )
        expected = int(getattr(estimator, "n_features_in_", len(self.feature_names)))
        if expected != len(self.feature_names):
            raise ValueError(
                f"the estimator was fitted on {expected} feature(s) but the "
                f"codec names {len(self.feature_names)} ({self.feature_names}); "
                "the bands would not line up"
            )
        labels = np.asarray(estimator.classes_)
        return tuple(
            _tree_to_string(member.tree_, self.feature_names, labels)
            for member in members
        )
