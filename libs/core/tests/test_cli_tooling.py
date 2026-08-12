"""Tests for entry-point discovery of the provider CLI-tooling tables."""

from __future__ import annotations

from typing import Any

import pytest

from earthlens._cli_tooling import (
    CLI_ENTRY_POINT_GROUP,
    discover_cli_tooling,
    resolve_target,
)


class _FakeEntryPoint:
    """Stands in for an installed distribution's CLI-tooling entry point."""

    def __init__(self, name: str, table: dict[str, Any]) -> None:
        self.name = name
        # Real EntryPoints carry `value` (the "module:attr" target); the
        # duplicate-key warning names it, so the stub models it too.
        self.value = f"earthlens._{name}_cli:CLI_TOOLING"
        self._table = table

    def load(self) -> dict[str, Any]:
        return self._table


_SPEC_A = {"refresher": "earthlens.alpha.cli:refresher"}
_SPEC_B = {"validator": "earthlens.beta.cli:validator", "index_attr": "available"}


@pytest.mark.unit
class TestDiscovery:
    """Tests for `discover_cli_tooling`."""

    def test_group_name_matches_the_convention(self) -> None:
        """The tooling group sits beside the backend group under one prefix."""
        assert CLI_ENTRY_POINT_GROUP == "earthlens.cli"

    def test_merges_every_entry_point(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tables from separate distributions are merged into one registry."""
        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"a": _SPEC_A}),
                _FakeEntryPoint("beta", {"b": _SPEC_B}),
            ],
        )
        assert discover_cli_tooling() == {"a": _SPEC_A, "b": _SPEC_B}

    def test_later_entry_point_wins_on_a_duplicate_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id tooled twice resolves to the last table merged."""
        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"dup": _SPEC_A}),
                _FakeEntryPoint("beta", {"dup": _SPEC_B}),
            ],
        )
        assert discover_cli_tooling()["dup"] == _SPEC_B

    def test_duplicate_key_is_warned_about(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id claimed by two distributions logs a warning naming both."""
        from loguru import logger

        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"dup": _SPEC_A}),
                _FakeEntryPoint("beta", {"dup": _SPEC_B}),
            ],
        )
        messages: list[Any] = []
        sink = logger.add(lambda record: messages.append(record), level="WARNING")
        try:
            discover_cli_tooling()
        finally:
            logger.remove(sink)
        assert any("published by both" in str(message) for message in messages)

    def test_distinct_keys_are_not_warned_about(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Merging disjoint tables stays quiet."""
        from loguru import logger

        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"a": _SPEC_A}),
                _FakeEntryPoint("beta", {"b": _SPEC_B}),
            ],
        )
        messages: list[Any] = []
        sink = logger.add(lambda record: messages.append(record), level="WARNING")
        try:
            discover_cli_tooling()
        finally:
            logger.remove(sink)
        assert messages == []

    def test_no_entry_points_yields_an_empty_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no provider distribution installed, nothing is tooled."""
        monkeypatch.setattr("earthlens._cli_tooling.entry_points", lambda group: [])
        assert discover_cli_tooling() == {}

    def test_discovery_imports_no_provider_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Building the registry imports no provider package of its own."""
        import sys

        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [_FakeEntryPoint("alpha", {"a": _SPEC_A})],
        )
        before = {name for name in sys.modules if name.startswith("earthlens.")}
        discover_cli_tooling()
        after = {name for name in sys.modules if name.startswith("earthlens.")}
        assert after == before


@pytest.mark.unit
class TestResolveTarget:
    """Tests for the lazy `"module:attr"` resolver."""

    def test_resolves_a_module_attr_target(self) -> None:
        """A well-formed target imports the module and returns the attribute."""
        resolved = resolve_target("earthlens._cli_tooling:CLI_ENTRY_POINT_GROUP")
        assert resolved == "earthlens.cli"

    def test_resolves_a_dotted_attribute(self) -> None:
        """A dotted attr walks into a nested attribute of the module."""
        resolved = resolve_target(
            "earthlens._cli_tooling:discover_cli_tooling.__name__"
        )
        assert resolved == "discover_cli_tooling"

    @pytest.mark.parametrize("bad", ["no-colon", "earthlens._cli_tooling:", ":attr"])
    def test_a_malformed_target_is_rejected(self, bad: str) -> None:
        """A target missing the module or the attr half raises."""
        with pytest.raises(ValueError, match="module:attr"):
            resolve_target(bad)
