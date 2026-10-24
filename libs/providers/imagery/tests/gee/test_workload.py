"""Tests for the nest-safe `WorkloadTag` context manager."""

from __future__ import annotations

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee import workload as workload_module  # noqa: E402
from earthlens.gee.workload import WorkloadTag  # noqa: E402


class _FakeData:
    """Client-side workload-tag state recording every get/set call."""

    def __init__(self, tag: str = "") -> None:
        self.tag: object = tag
        self.calls: list[tuple[str, object]] = []

    def getWorkloadTag(self) -> object:  # noqa: N802 - EE method name
        self.calls.append(("get", self.tag))
        return self.tag

    def setWorkloadTag(self, tag: object) -> None:  # noqa: N802 - EE method name
        self.calls.append(("set", tag))
        self.tag = tag


@pytest.fixture
def fake_data(monkeypatch):
    """Patch `ee.data`'s workload-tag functions with a recording fake."""
    fake = _FakeData()
    monkeypatch.setattr(workload_module.ee.data, "getWorkloadTag", fake.getWorkloadTag)
    monkeypatch.setattr(workload_module.ee.data, "setWorkloadTag", fake.setWorkloadTag)
    return fake


class TestWorkloadTagValidation:
    """The tag string is validated at construction, not mid-download."""

    @pytest.mark.parametrize(
        "tag",
        [
            "a",  # 1 char, the lower boundary
            "a" * 63,  # 63 chars, the upper boundary
            "a1",
            "run-42",
            "a-b",
            "a_b",
            "a_b-c",  # both separators EE accepts, between alphanumerics
            "0" + "-" * 61 + "9",
        ],
    )
    def test_a_well_formed_tag_is_kept(self, tag):
        """Tags the SDK accepts construct and keep their string."""
        assert WorkloadTag(tag=tag).tag == tag

    @pytest.mark.parametrize(
        "tag",
        [
            "",  # below the 1-char floor
            "a" * 64,  # above the 63-char cap
            "-a",  # leading separator
            "a-",  # trailing separator
            "_a",
            "a_",
            "a b",  # whitespace is not a separator
            "a$b",
            "A1",  # uppercase: the docstring allows it, the SDK does not
            "a.b",  # a dot: likewise documented but refused
            "A1-b.C",
            # `$` also matches just before a trailing newline, while the SDK's
            # own `fullmatch` does not, so neither may this validator.
            "ab\n",
            "ab\n\n",
            "a\nb",
        ],
    )
    def test_an_illegal_tag_is_refused_at_construction(self, tag):
        """A tag Earth Engine would reject raises before any request is made."""
        with pytest.raises(ValidationError, match="1-63 characters"):
            WorkloadTag(tag=tag)

    @pytest.mark.parametrize(
        "tag", ["A1", "a.b", "a" * 64, "-a", "a-", "a b", "ab\n", "ab\n\n"]
    )
    def test_the_rule_matches_the_sdk(self, tag):
        """Each tag refused at construction is one `ee.data` also refuses.

        The SDK's docstring documents a looser rule than its enforcement
        (`ee/_state.py` requires lowercase and forbids dots), so the validator
        is pinned against the enforcement rather than against the prose.
        """
        with pytest.raises(ValueError):
            workload_module.ee.data.setWorkloadTag(tag)

    @pytest.mark.parametrize("tag", ["a", "a1", "a-b", "a_b", "a" * 63])
    def test_every_accepted_tag_is_one_the_sdk_takes(self, tag):
        """A tag that constructs is one `ee.data.setWorkloadTag` accepts."""
        WorkloadTag(tag=tag)
        workload_module.ee.data.setWorkloadTag(tag)
        assert workload_module.ee.data.getWorkloadTag() == tag
        workload_module.ee.data.resetWorkloadTag()

    def test_the_empty_tag_is_the_sdks_no_tag_sentinel(self):
        """`''` means "no tag" to the SDK, so a scope over it is refused here."""
        workload_module.ee.data.setWorkloadTag("")
        assert workload_module.ee.data.getWorkloadTag() == ""
        with pytest.raises(ValidationError, match="1-63 characters"):
            WorkloadTag(tag="")

    def test_it_is_frozen(self):
        """The tag cannot drift after construction."""
        scope = WorkloadTag(tag="run-42")
        with pytest.raises(ValidationError):
            scope.tag = "other"

    def test_a_typo_is_refused(self):
        """An unknown keyword raises instead of silently no-op'ing."""
        with pytest.raises(ValidationError):
            WorkloadTag(tag="run-42", nested=True)


class TestWorkloadTagScope:
    """Entering sets the tag; exiting restores the saved one."""

    def test_entering_sets_and_exiting_restores(self, fake_data):
        """The tag is set on enter and the previous value is back on exit."""
        with WorkloadTag(tag="run-42") as scope:
            assert scope.tag == "run-42"
            assert fake_data.tag == "run-42"
        assert fake_data.tag == ""
        assert fake_data.calls == [("get", ""), ("set", "run-42"), ("set", "")]

    def test_enter_returns_the_scope_object(self, fake_data):
        """The `with ... as` target is the WorkloadTag itself."""
        scope = WorkloadTag(tag="run-42")
        with scope as entered:
            assert entered is scope

    def test_nesting_restores_the_enclosing_tag(self, fake_data):
        """An inner scope hands back to the outer tag, not the default one.

        This is exactly what `ee.data.workloadTagContext` gets wrong: its exit
        calls `resetWorkloadTag()`, which falls back to the default tag and so
        wipes the outer scope's tag.
        """
        with WorkloadTag(tag="outer-run"):
            with WorkloadTag(tag="inner-run"):
                assert fake_data.tag == "inner-run"
            assert fake_data.tag == "outer-run"
        assert fake_data.tag == ""

    def test_an_exception_still_restores(self, fake_data):
        """The saved tag comes back even when the body raises."""
        fake_data.tag = "pre-existing"
        with pytest.raises(RuntimeError, match="boom"):
            with WorkloadTag(tag="run-42"):
                raise RuntimeError("boom")
        assert fake_data.tag == "pre-existing"

    def test_the_exception_is_not_swallowed(self, fake_data):
        """__exit__ restores but never suppresses the exception."""
        scope = WorkloadTag(tag="run-42")
        scope.__enter__()
        assert scope.__exit__(RuntimeError, RuntimeError("boom"), None) is None

    def test_the_same_object_can_be_reentered(self, fake_data):
        """Re-entering one object nests via a stack rather than clobbering."""
        scope = WorkloadTag(tag="run-42")
        fake_data.tag = "pre-existing"
        with scope:
            with scope:
                assert fake_data.tag == "run-42"
            assert fake_data.tag == "run-42"
        assert fake_data.tag == "pre-existing"

    def test_a_non_string_saved_tag_is_restored_verbatim(self, fake_data):
        """Whatever `getWorkloadTag` answered is what `setWorkloadTag` gets back."""
        fake_data.tag = 1234
        with WorkloadTag(tag="run-42"):
            assert fake_data.tag == "run-42"
        assert fake_data.tag == 1234

    def test_an_unbalanced_exit_is_named(self, fake_data):
        """`__exit__` without a matching `__enter__` says so, not `IndexError`."""
        scope = WorkloadTag(tag="run-42")
        with pytest.raises(RuntimeError, match="was not entered"):
            scope.__exit__(None, None, None)

    def test_an_unbalanced_exit_leaves_the_tag_alone(self, fake_data):
        """The refusal happens before any `setWorkloadTag`, so state is intact."""
        fake_data.tag = "pre-existing"
        with pytest.raises(RuntimeError):
            WorkloadTag(tag="run-42").__exit__(None, None, None)
        assert fake_data.tag == "pre-existing"
        assert fake_data.calls == []

    def test_a_double_exit_is_named(self, fake_data):
        """One extra exit after a balanced scope is the same mistake."""
        scope = WorkloadTag(tag="run-42")
        with scope:
            pass
        with pytest.raises(RuntimeError, match="was not entered"):
            scope.__exit__(None, None, None)
