"""Tests for entry-point discovery of the provider CLI-tooling tables."""

from __future__ import annotations

from typing import Any

import pytest

from earthlens._cli_tooling import (
    CLI_ENTRY_POINT_GROUP,
    config_table,
    discover_cli_tooling,
    dispatch_table,
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


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Reset the memoized discovery around each test that repoints entry_points.

    `discover_cli_tooling` is `lru_cache`d, so a real-entry-points result cached
    by import (or a previous test) would leak into a test that monkeypatches
    `entry_points`. Clear before and after so every test sees its own view.
    """
    discover_cli_tooling.cache_clear()
    yield
    discover_cli_tooling.cache_clear()


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


@pytest.mark.unit
class TestDispatchAndConfigTables:
    """Tests for the `dispatch_table` / `config_table` / `_thunk` projection."""

    def test_dispatch_table_projects_only_the_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only providers publishing the role appear in the table."""
        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"a": _SPEC_A}),  # refresher only
                _FakeEntryPoint("beta", {"b": _SPEC_B}),  # validator + index_attr
            ],
        )
        assert set(dispatch_table("refresher")) == {"a"}
        assert set(dispatch_table("validator")) == {"b"}

    def test_dispatch_table_thunks_resolve_lazily(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Building the table imports no handler module; the thunk resolves on call."""
        import sys

        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [
                _FakeEntryPoint("x", {"x": {"refresher": "earthlens.nope.cli:go"}})
            ],
        )
        table = dispatch_table("refresher")  # must not import earthlens.nope
        assert "earthlens.nope.cli" not in sys.modules, "handler module not imported"
        with pytest.raises(ModuleNotFoundError):
            table["x"]()  # resolution is deferred to the first call

    def test_dispatch_table_thunk_forwards_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolved thunk forwards its arguments to the real handler."""
        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [
                _FakeEntryPoint(
                    "x", {"x": {"writer": "earthlens.base.naming:safe_filename"}}
                )
            ],
        )
        assert dispatch_table("writer")["x"]("a/b:c") == "a_b_c"

    def test_config_table_passes_values_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config role's literal value is returned verbatim, not imported."""
        monkeypatch.setattr(
            "earthlens._cli_tooling.entry_points",
            lambda group: [
                _FakeEntryPoint("alpha", {"a": _SPEC_A}),  # no index_attr
                _FakeEntryPoint("beta", {"b": _SPEC_B}),  # index_attr: available
            ],
        )
        assert config_table("index_attr") == {"b": "available"}
