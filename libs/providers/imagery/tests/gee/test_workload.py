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
            "run-42",
            "a.b_c-d",  # every separator EE documents, between alphanumerics
            "A1-b.C",  # case is allowed
            "0" + "-" * 61 + "9",
        ],
    )
    def test_a_well_formed_tag_is_kept(self, tag):
        """Tags matching EE's documented rule construct and keep their string."""
        assert WorkloadTag(tag=tag).tag == tag

    @pytest.mark.parametrize(
        "tag",
        [
            "",  # below the 1-char floor
            "a" * 64,  # above the 63-char cap
            "-a",  # leading separator
            "a.",  # trailing separator
            "_a",
            "a-",
            "a b",  # whitespace is not a documented separator
            "a$b",
        ],
    )
    def test_an_illegal_tag_is_refused_at_construction(self, tag):
        """A tag Earth Engine would reject raises before any request is made."""
        with pytest.raises(ValidationError, match="1-63 characters"):
            WorkloadTag(tag=tag)

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
