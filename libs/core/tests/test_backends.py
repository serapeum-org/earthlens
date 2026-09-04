"""Tests for entry-point discovery of the provider backend tables."""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from earthlens._backends import (
    ENTRY_POINT_GROUP,
    KEY_TOPIC_SEPARATOR,
    RESERVED_TOPICS,
    discover_backends,
    topic_claimants,
)
from earthlens.earthlens import EarthLens

#: Every thematic provider distribution installed in the dev workspace.
THEMES = ["atmosphere", "ocean", "imagery", "land", "hazards"]

#: The workspace root (this file lives at libs/core/tests/test_backends.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _theme_shipped_segments(theme: str) -> set[str]:
    """Top-level `earthlens.<seg>` packages a theme distribution ships.

    Reads the `[tool.setuptools.packages.find] include` globs from the
    theme's `pyproject.toml` and reduces each `earthlens.<seg>*` glob to
    its `<seg>`, giving the exact set of top-level packages that
    distribution puts on the wheel.

    Args:
        theme: A provider theme name (e.g. `"ocean"`).

    Returns:
        The set of top-level segment names (e.g. `{"_ocean", "argo",
        "cmems", ...}`) the distribution's find globs cover.
    """
    pyproject = _REPO_ROOT / "libs" / "providers" / theme / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    includes = data["tool"]["setuptools"]["packages"]["find"]["include"]
    return {glob.removeprefix("earthlens.").rstrip("*") for glob in includes}


class _FakeEntryPoint:
    """Stands in for an installed distribution's backend-table entry point."""

    def __init__(self, name: str, table: dict[str, Any]) -> None:
        self.name = name
        # Real EntryPoints carry `value` (the "module:attr" target); the
        # duplicate-key warning names it, so the stub models it too.
        self.value = f"earthlens._{name}:BACKENDS"
        self._table = table

    def load(self) -> dict[str, Any]:
        return self._table


_SPEC_A = ("earthlens.alpha", "Alpha", "alpha", {})
_SPEC_B = ("earthlens.beta", "Beta", "beta", {"endpoint": "b"})


@pytest.mark.unit
class TestDiscovery:
    """Tests for `discover_backends`."""

    @pytest.mark.parametrize("theme", THEMES)
    def test_every_theme_publishes_its_table(self, theme: str) -> None:
        """Each provider distribution registers one entry point."""
        from importlib.metadata import entry_points

        values = [ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)]
        assert f"earthlens._{theme}:BACKENDS" in values

    def test_facade_registry_is_built_from_discovery(self) -> None:
        """The facade exposes exactly the discovered keys."""
        assert set(EarthLens.DataSources) == set(discover_backends())

    def test_discovery_merges_every_theme(self) -> None:
        """Keys from all five distributions land in one registry."""
        found = discover_backends()
        for key in ("chirps", "argo", "gee", "dem", "osm"):
            assert key in found

    def test_merges_every_entry_point(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tables from separate distributions are merged into one registry."""
        monkeypatch.setattr(
            "earthlens._backends.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"a": _SPEC_A}),
                _FakeEntryPoint("beta", {"b": _SPEC_B}),
            ],
        )
        assert discover_backends() == {"a": _SPEC_A, "b": _SPEC_B}

    def test_later_entry_point_wins_on_a_duplicate_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key published twice resolves to the last table merged."""
        monkeypatch.setattr(
            "earthlens._backends.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"dup": _SPEC_A}),
                _FakeEntryPoint("beta", {"dup": _SPEC_B}),
            ],
        )
        assert discover_backends()["dup"] == _SPEC_B

    def test_duplicate_key_is_warned_about(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A key claimed by two distributions logs a warning naming both."""
        from loguru import logger

        monkeypatch.setattr(
            "earthlens._backends.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"dup": _SPEC_A}),
                _FakeEntryPoint("beta", {"dup": _SPEC_B}),
            ],
        )
        messages: list[str] = []
        sink = logger.add(lambda record: messages.append(record), level="WARNING")
        try:
            discover_backends()
        finally:
            logger.remove(sink)
        assert any("published by both" in message for message in messages)

    def test_bare_reserved_key_is_warned_about(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider registering a bare reserved topic word warns at discovery."""
        from loguru import logger

        monkeypatch.setattr(
            "earthlens._backends.entry_points",
            lambda group: [_FakeEntryPoint("plugin", {"precipitation": _SPEC_A})],
        )
        messages: list[str] = []
        sink = logger.add(lambda record: messages.append(record), level="WARNING")
        try:
            discover_backends()
        finally:
            logger.remove(sink)
        assert any("reserved generic topic word" in message for message in messages)

    def test_distinct_keys_are_not_warned_about(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Merging disjoint tables stays quiet."""
        from loguru import logger

        monkeypatch.setattr(
            "earthlens._backends.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"a": _SPEC_A}),
                _FakeEntryPoint("beta", {"b": _SPEC_B}),
            ],
        )
        messages: list[str] = []
        sink = logger.add(lambda record: messages.append(record), level="WARNING")
        try:
            discover_backends()
        finally:
            logger.remove(sink)
        assert messages == []

    def test_no_entry_points_yields_an_empty_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no provider distribution installed, nothing is registered."""
        monkeypatch.setattr("earthlens._backends.entry_points", lambda group: [])
        assert discover_backends() == {}


@pytest.mark.unit
class TestThemeTables:
    """Tests for the per-distribution tables the entry points resolve to."""

    def test_themes_do_not_overlap(self) -> None:
        """A data-source key is published by exactly one distribution."""
        import importlib

        seen: dict[str, str] = {}
        for theme in THEMES:
            table = importlib.import_module(f"earthlens._{theme}").BACKENDS
            for key in table:
                assert key not in seen, (
                    f"{key} published by {seen.get(key)} and {theme}"
                )
                seen[key] = theme

    def test_themes_account_for_every_registered_key(self) -> None:
        """The union of the theme tables is the whole registry."""
        import importlib

        union: dict[str, Any] = {}
        for theme in THEMES:
            union.update(importlib.import_module(f"earthlens._{theme}").BACKENDS)
        assert union == discover_backends()

    @pytest.mark.parametrize("theme", THEMES)
    def test_a_theme_only_names_its_own_backends(self, theme: str) -> None:
        """A distribution's table never points into another distribution."""
        import importlib

        table = importlib.import_module(f"earthlens._{theme}").BACKENDS
        shipped = _theme_shipped_segments(theme)
        for key, spec in table.items():
            module = spec[0]
            assert module.startswith("earthlens."), (
                f"{theme}:{key} names {module!r}, not under the earthlens namespace"
            )
            top_level = module.split(".")[1]
            assert top_level in shipped, (
                f"{theme} table key {key!r} points at earthlens.{top_level}, which "
                f"earthlens-{theme} does not ship (it ships {sorted(shipped)})"
            )


@pytest.mark.unit
class TestTableShape:
    """Tests for the invariants every row in the registry must hold."""

    def test_every_spec_is_a_four_tuple(self) -> None:
        """Each row carries module, class, extras hint and default kwargs."""
        assert all(len(spec) == 4 for spec in discover_backends().values())

    def test_module_and_class_are_strings(self) -> None:
        """Rows name their backend lazily, so no import happens on discovery."""
        assert all(
            isinstance(module, str) and isinstance(class_name, str)
            for module, class_name, _, _ in discover_backends().values()
        )

    def test_default_kwargs_are_dicts(self) -> None:
        """Pre-bound constructor arguments are always a mapping."""
        assert all(isinstance(spec[3], dict) for spec in discover_backends().values())

    def test_discovery_imports_no_backend_module(self) -> None:
        """Building the registry imports no backend package of its own."""
        import sys

        before = {name for name in sys.modules if name.startswith("earthlens.")}
        discover_backends()
        after = {name for name in sys.modules if name.startswith("earthlens.")}
        assert after == before


@pytest.mark.unit
class TestPreBoundKwargs:
    """Tests for the alias keys that pre-bind constructor arguments."""

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("cdse", {"endpoint": "cdse"}),
            ("planetary-computer", {"endpoint": "planetary-computer"}),
            ("landsat", {"endpoint": "usgs-landsat"}),
            ("nsrdb", {"product": "nsrdb-psm3"}),
            ("wind-toolkit", {"product": "wtk"}),
        ],
    )
    def test_alias_survives_discovery(self, key: str, expected: dict) -> None:
        """An entry point carries the table, so pre-bound kwargs survive."""
        assert EarthLens.DataSources.default_kwargs(key) == expected


@pytest.mark.unit
class TestReservedTopics:
    """`RESERVED_TOPICS` and the `topic_claimants` helper (C1)."""

    def test_topic_claimants_returns_sorted_qualified_keys(self) -> None:
        """`topic_claimants` returns the sorted `source:topic` keys for a topic."""
        keys = ["b:elevation", "a:elevation", "dem", "x:solar-pv"]
        assert topic_claimants(keys, "elevation") == ["a:elevation", "b:elevation"]

    def test_topic_claimants_empty_when_unclaimed(self) -> None:
        """A topic no key qualifies yields an empty list."""
        assert topic_claimants(["chc", "cmems"], "precipitation") == []

    def test_topic_claimants_matches_the_whole_topic_not_a_suffix(self) -> None:
        """A hyphenated topic is one word, so its tail is not a topic of its own."""
        # The docstring's own example: `foo:sea-surface-temperature` serves
        # `sea-surface-temperature` and never `temperature`. An `endswith` here
        # would pass both existing cases, so it takes a key whose topic ends in
        # the searched word to hold the exact-match rule in place.
        keys = ["foo:sea-surface-temperature", "bar:temperature"]
        assert topic_claimants(keys, "temperature") == ["bar:temperature"]

    def test_topic_claimants_splits_on_the_first_separator_only(self) -> None:
        """A key's topic is everything after the first separator, colons and all."""
        # `split(sep)[1]` reads the same as `split(sep, 1)[1]` for every key in
        # the registry today, so nothing else would notice maxsplit going missing.
        keys = ["a:sea:surface", "b:sea"]
        assert topic_claimants(keys, "sea:surface") == ["a:sea:surface"]
        assert topic_claimants(keys, "sea") == ["b:sea"]

    def test_key_topic_separator_is_the_grammar_colon(self) -> None:
        """The exported separator is the colon the facade-key grammar is written in."""
        assert KEY_TOPIC_SEPARATOR == ":"

    def test_reserved_topics_covers_the_migrated_words(self) -> None:
        """Every generic word migrated to `source:topic` is a reserved topic."""
        migrated = {
            "elevation",
            "insar",
            "bare-earth-dem",
            "human-settlement",
            "climate-projections",
            "teleconnections",
            "european-flood-hazard",
            "sea-level-forecast",
            "coastal-forecast",
            "twl-forecast",
            "solar-pv",
        }
        assert migrated <= RESERVED_TOPICS


def _provider_tables() -> list[tuple[str, dict[str, object]]]:
    """Return `(theme, BACKENDS)` for every provider distribution."""
    return [
        (theme, importlib.import_module(f"earthlens._{theme}").BACKENDS)
        for theme in THEMES
    ]


def _duplicate_keys(tables: list[tuple[str, dict[str, object]]]) -> list[str]:
    """Return the sorted keys registered by more than one provider table."""
    seen: dict[str, str] = {}
    dups: list[str] = []
    for theme, table in tables:
        for key in table:
            if key in seen:
                dups.append(key)
            seen[key] = theme
    return sorted(dups)


def _bare_reserved(keys: Iterable[str]) -> list[str]:
    """Return the sorted bare keys that are reserved topic words."""
    return sorted(key for key in keys if ":" not in key and key in RESERVED_TOPICS)


def _dangling_topics(keys: Iterable[str]) -> list[str]:
    """Return the sorted `source:topic` keys whose topic is not reserved."""
    return sorted(
        key
        for key in keys
        if ":" in key and key.split(":", 1)[1] not in RESERVED_TOPICS
    )


@pytest.mark.unit
class TestRegistrationGuards:
    """The merged registry obeys the source/topic key invariants (C3)."""

    def test_no_key_is_registered_twice(self) -> None:
        """No facade key is registered by more than one provider table."""
        tables = _provider_tables()
        assert _duplicate_keys(tables) == []
        assert len(discover_backends()) == sum(len(table) for _theme, table in tables)

    def test_no_bare_reserved_word_is_registered(self) -> None:
        """A reserved topic word is never registered as a bare facade key."""
        assert _bare_reserved(discover_backends()) == []

    def test_no_qualified_key_names_an_unreserved_topic(self) -> None:
        """Every `source:topic` key's topic is a reserved word."""
        assert _dangling_topics(discover_backends()) == []

    def test_duplicate_detection_catches_a_clash(self) -> None:
        """`_duplicate_keys` flags a key that two tables share."""
        tables = [("a", {"x": 1, "y": 1}), ("b", {"x": 2})]
        assert _duplicate_keys(tables) == ["x"]

    def test_bare_reserved_detection_catches_a_violation(self) -> None:
        """`_bare_reserved` flags a reserved word registered bare."""
        assert _bare_reserved(["dem", "elevation", "dem:elevation"]) == ["elevation"]

    def test_dangling_topic_detection_catches_a_violation(self) -> None:
        """`_dangling_topics` flags a qualified key naming an unreserved topic."""
        assert _dangling_topics(["dem:elevation", "foo:not-a-topic"]) == [
            "foo:not-a-topic"
        ]
