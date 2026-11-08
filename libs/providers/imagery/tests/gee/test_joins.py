"""Tests for the composable `ee.Join` wrappers."""

from __future__ import annotations

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee import joins as joins_module  # noqa: E402
from earthlens.gee.joins import (  # noqa: E402
    InnerJoin,
    Join,
    SaveAllJoin,
    SaveBestJoin,
    SaveFirstJoin,
    SimpleJoin,
)


class _FakeJoinInstance:
    """An `ee.Join` whose `apply` records its keyword arguments."""

    def __init__(self, log: list) -> None:
        self.log = log

    def apply(self, **kwargs: object) -> tuple:
        self.log.append(kwargs)
        return ("joined", kwargs)


class _FakeJoinFactory:
    """Stands in for `ee.Join`, recording each factory call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.applies: list = []

    def _record(self, name: str, **kwargs: object) -> _FakeJoinInstance:
        self.calls.append((name, kwargs))
        return _FakeJoinInstance(self.applies)

    def inner(self, **kwargs: object) -> _FakeJoinInstance:
        return self._record("inner", **kwargs)

    def saveAll(self, **kwargs: object) -> _FakeJoinInstance:  # noqa: N802
        return self._record("saveAll", **kwargs)

    def saveBest(self, **kwargs: object) -> _FakeJoinInstance:  # noqa: N802
        return self._record("saveBest", **kwargs)

    def saveFirst(self, **kwargs: object) -> _FakeJoinInstance:  # noqa: N802
        return self._record("saveFirst", **kwargs)

    def simple(self, **kwargs: object) -> _FakeJoinInstance:
        return self._record("simple", **kwargs)


@pytest.fixture
def fake_join(monkeypatch):
    """Patch the module's `ee.Join` surface with a recorder."""
    factory = _FakeJoinFactory()
    monkeypatch.setattr(joins_module.ee, "Join", factory)
    return factory


class TestBase:
    """The frozen `Join` contract."""

    def test_the_base_refuses_to_apply(self):
        """The base class names the contract; applying it raises."""
        base = Join()
        with pytest.raises(NotImplementedError, match="_join"):
            base.apply("primary", "secondary", "condition")

    def test_it_is_frozen(self):
        """A wrapper's configuration cannot drift after construction."""
        join = SaveAllJoin(matches_key="m")
        with pytest.raises(ValidationError):
            join.matches_key = "other"

    def test_a_typo_is_refused(self):
        """An unknown keyword raises instead of silently no-op'ing."""
        with pytest.raises(ValidationError):
            SaveAllJoin(matchesKey="m")

    def test_apply_forwards_by_keyword(self, fake_join):
        """apply hands primary/secondary/condition to ee by keyword."""
        SimpleJoin().apply("p", "s", "cond")
        assert fake_join.applies == [
            {"primary": "p", "secondary": "s", "condition": "cond"}
        ]

    def test_apply_returns_the_joined_collection(self, fake_join):
        """The joined collection comes back to the caller untouched."""
        result = InnerJoin().apply("p", "s", "cond")
        assert result == (
            "joined",
            {"primary": "p", "secondary": "s", "condition": "cond"},
        )


class TestInnerJoin:
    """`InnerJoin` builds `ee.Join.inner`."""

    def test_defaults(self, fake_join):
        """The pair lands under ee's documented primary/secondary keys."""
        InnerJoin().apply("p", "s", "cond")
        assert fake_join.calls == [
            ("inner", {"primaryKey": "primary", "secondaryKey": "secondary"})
        ]

    def test_measure_key_is_passed_when_set(self, fake_join):
        """A measure key rides along only when asked for."""
        InnerJoin(primary_key="a", secondary_key="b", measure_key="d").apply(
            "p", "s", "cond"
        )
        assert fake_join.calls == [
            ("inner", {"primaryKey": "a", "secondaryKey": "b", "measureKey": "d"})
        ]


class TestSaveAllJoin:
    """`SaveAllJoin` builds `ee.Join.saveAll`."""

    def test_defaults(self, fake_join):
        """A bare join saves the matches list and nothing more."""
        SaveAllJoin().apply("p", "s", "cond")
        assert fake_join.calls == [("saveAll", {"matchesKey": "matches"})]

    def test_ordering_brings_ascending(self, fake_join):
        """ascending accompanies ordering, mirroring the gapfill call shape."""
        SaveAllJoin(
            matches_key="after", ordering="system:time_start", ascending=True
        ).apply("p", "s", "cond")
        assert fake_join.calls == [
            (
                "saveAll",
                {
                    "matchesKey": "after",
                    "ordering": "system:time_start",
                    "ascending": True,
                },
            )
        ]

    def test_measure_key_and_outer_are_passed_when_set(self, fake_join):
        """The optional measure/outer knobs reach ee only when set."""
        SaveAllJoin(matches_key="m", measure_key="d", outer=True).apply(
            "p", "s", "cond"
        )
        assert fake_join.calls == [
            ("saveAll", {"matchesKey": "m", "measureKey": "d", "outer": True})
        ]

    def test_descending_without_ordering_is_refused(self):
        """ascending=False has nothing to sort without ordering."""
        with pytest.raises(ValidationError, match="ordering"):
            SaveAllJoin(ascending=False)


class TestSaveBestJoin:
    """`SaveBestJoin` builds `ee.Join.saveBest`."""

    def test_both_keys_are_required(self):
        """ee.Join.saveBest has no defaults for its two keys."""
        with pytest.raises(ValidationError):
            SaveBestJoin(match_key="best")

    def test_kwargs(self, fake_join):
        """The match and measure keys reach ee under their wire names."""
        SaveBestJoin(match_key="best", measure_key="d").apply("p", "s", "cond")
        assert fake_join.calls == [
            ("saveBest", {"matchKey": "best", "measureKey": "d"})
        ]

    def test_outer_is_passed_when_set(self, fake_join):
        """outer=True keeps unmatched primary rows."""
        SaveBestJoin(match_key="best", measure_key="d", outer=True).apply(
            "p", "s", "cond"
        )
        assert fake_join.calls == [
            ("saveBest", {"matchKey": "best", "measureKey": "d", "outer": True})
        ]


class TestSaveFirstJoin:
    """`SaveFirstJoin` builds `ee.Join.saveFirst`."""

    def test_match_key_is_required(self):
        """ee.Join.saveFirst has no default match key."""
        with pytest.raises(ValidationError):
            SaveFirstJoin()

    def test_defaults(self, fake_join):
        """A bare join saves the first match and nothing more."""
        SaveFirstJoin(match_key="first").apply("p", "s", "cond")
        assert fake_join.calls == [("saveFirst", {"matchKey": "first"})]

    def test_full_configuration(self, fake_join):
        """Every knob reaches ee under its wire name."""
        SaveFirstJoin(
            match_key="first",
            ordering="system:time_start",
            ascending=False,
            measure_key="d",
            outer=True,
        ).apply("p", "s", "cond")
        assert fake_join.calls == [
            (
                "saveFirst",
                {
                    "matchKey": "first",
                    "ordering": "system:time_start",
                    "ascending": False,
                    "measureKey": "d",
                    "outer": True,
                },
            )
        ]

    def test_descending_without_ordering_is_refused(self):
        """ascending=False has nothing to sort without ordering."""
        with pytest.raises(ValidationError, match="ordering"):
            SaveFirstJoin(match_key="first", ascending=False)


class TestSimpleJoin:
    """`SimpleJoin` builds `ee.Join.simple`."""

    def test_no_configuration(self, fake_join):
        """The simple join takes no arguments at all."""
        SimpleJoin().apply("p", "s", "cond")
        assert fake_join.calls == [("simple", {})]
