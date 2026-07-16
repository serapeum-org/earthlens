"""Tests for entry-point discovery of the provider backend tables."""

from __future__ import annotations

from typing import Any

import pytest

from earthlens._backends import ENTRY_POINT_GROUP, discover_backends
from earthlens.earthlens import EarthLens

#: Every thematic provider distribution installed in the dev workspace.
THEMES = ["atmosphere", "ocean", "imagery", "land", "hazards"]


class _FakeEntryPoint:
    """Stands in for an installed distribution's backend-table entry point."""

    def __init__(self, name: str, table: dict[str, Any]) -> None:
        self.name = name
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
                assert key not in seen, f"{key} published by {seen.get(key)} and {theme}"
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
        modules = {spec[0] for spec in table.values()}
        for module in modules:
            assert module.startswith("earthlens.")


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
