"""Live check of Earth Engine's `decisionTreeEnsemble` tie-break.

Gated behind `-m e2e`. `TreeEnsemble` documents a majority vote with ties
broken to the smallest label and says that is Earth Engine's own rule; Earth
Engine documents the vote but not the tie-break, so only a live evaluation
settles it. Needs `GEE_SERVICE_ACCOUNT` / `GEE_SERVICE_KEY` and skips cleanly
without them. The request is a single one-point `classify` over
hand-written tree strings — no imagery, no assets, no queue.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("ee")

from earthlens.gee.ml import TreeEnsemble, _parse_tree  # noqa: E402

pytestmark = [pytest.mark.e2e]

_SERVICE_ACCOUNT = os.environ.get("GEE_SERVICE_ACCOUNT")
_SERVICE_KEY = os.environ.get("GEE_SERVICE_KEY")

_skip_without_creds = pytest.mark.skipif(
    not (_SERVICE_ACCOUNT and _SERVICE_KEY),
    reason="GEE_SERVICE_ACCOUNT / GEE_SERVICE_KEY not set",
)


def _constant_tree(label: int) -> str:
    """A tree whose every leaf predicts `label`, so its vote is unconditional."""
    return (
        "1) root 4 9999 9999 (0.5)\n"
        f"  2) B1 <= 0.500000 2 0.0000 {label} *\n"
        f"  3) B1 > 0.500000 2 0.0000 {label} *\n"
    )


def _initialise():
    """Authenticate the live `ee` session from the environment."""
    import ee

    from earthlens.gee.auth import EarthEngineAuth

    EarthEngineAuth.initialize(
        service_account=_SERVICE_ACCOUNT, service_key=_SERVICE_KEY
    )
    return ee


def _ee_vote(labels: list[int], value: float) -> float:
    """Classify one point server-side and read the label back."""
    ee = _initialise()
    classifier = ee.Classifier.decisionTreeEnsemble(
        ee.List([_constant_tree(label) for label in labels])
    )
    point = ee.Feature(ee.Geometry.Point([31.25, 29.95]), {"B1": value})
    classified = ee.FeatureCollection([point]).classify(classifier)
    return float(classified.first().get("classification").getInfo())


def _local_vote(labels: list[int], value: float) -> float:
    """The same ensemble through earthlens's own predictor."""
    ensemble = TreeEnsemble(
        feature_names=("B1",),
        trees=tuple(_parse_tree(_constant_tree(label)) for label in labels),
    )
    return float(ensemble.predict([[value]])[0])


@_skip_without_creds
@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ([1, 2], 1.0),
        ([2, 1], 1.0),
        ([3, 9], 3.0),
        ([9, 3], 3.0),
        ([5, 5, 8, 8], 5.0),
        ([0, 4], 0.0),
        ([1, 2, 3], 1.0),
    ],
    ids=["1v2", "2v1", "3v9", "9v3", "55v88", "0v4", "three-way"],
)
def test_earth_engine_breaks_a_tie_to_the_smallest_label(labels, expected):
    """A tied ensemble returns the smallest tied label, server-side."""
    assert _ee_vote(labels, 0.2) == expected


@_skip_without_creds
@pytest.mark.parametrize(
    "labels",
    [[1, 2], [2, 1], [3, 9], [5, 5, 8, 8], [0, 4], [7, 7, 9]],
    ids=["1v2", "2v1", "3v9", "55v88", "0v4", "majority"],
)
def test_the_local_predictor_agrees_with_earth_engine(labels):
    """`TreeEnsemble.predict` reproduces what Earth Engine returns."""
    assert _local_vote(labels, 0.2) == _ee_vote(labels, 0.2)


@_skip_without_creds
def test_the_tie_break_does_not_depend_on_tree_order():
    """Swapping the members leaves the answer unchanged, so it is by label."""
    assert _ee_vote([9, 3], 0.2) == _ee_vote([3, 9], 0.2)
