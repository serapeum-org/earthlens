"""Unit tests for the IUCN v4 REST shim's response-shape helpers."""

from __future__ import annotations

import pytest

from earthlens.iucn._rest import _category, _flatten_label


@pytest.mark.iucn
class TestFlattenLabel:
    """`_flatten_label` flattens the v4 `{description, code}` wrapper."""

    def test_passthrough_string(self):
        """A bare string is returned unchanged."""
        assert _flatten_label("Decreasing") == "Decreasing"

    def test_none_stays_none(self):
        """`None` stays `None`."""
        assert _flatten_label(None) is None

    def test_english_description_preferred(self):
        """A `{description: {en: ...}, code: ...}` wrapper returns the English label."""
        wrapper = {"description": {"en": "Decreasing"}, "code": "1"}
        assert _flatten_label(wrapper) == "Decreasing"

    def test_string_description_fallback(self):
        """A `{description: <string>}` wrapper returns the string."""
        assert _flatten_label({"description": "Stable"}) == "Stable"

    def test_code_fallback_when_no_description(self):
        """A wrapper with no description but a code returns the code as a string."""
        assert _flatten_label({"code": "1"}) == "1"

    def test_empty_dict_is_none(self):
        """A dict with neither description nor code yields `None`."""
        assert _flatten_label({}) is None

    def test_list_of_strings_joined(self):
        """A list of strings is `'; '`-joined so the data is not silently dropped."""
        assert _flatten_label(["A2c", "B1ab"]) == "A2c; B1ab"

    def test_list_of_wrappers_flattened(self):
        """A list of `{description, code}` wrappers flattens each then joins."""
        wrappers = [
            {"description": {"en": "Decreasing"}, "code": "1"},
            {"description": {"en": "Stable"}, "code": "2"},
        ]
        assert _flatten_label(wrappers) == "Decreasing; Stable"

    def test_empty_list_is_none(self):
        """An empty list yields `None`, not an empty string."""
        assert _flatten_label([]) is None

    def test_unknown_type_falls_through_to_none(self):
        """A value that is none of (None, str, list, dict) returns `None`."""
        assert _flatten_label(42) is None
        assert _flatten_label(3.14) is None

    def test_empty_english_falls_back_to_code(self):
        """A wrapper with `description.en = ""` falls through to the `code` field."""
        wrapper = {"description": {"en": ""}, "code": "1"}
        assert _flatten_label(wrapper) == "1"

    def test_empty_string_description_falls_back_to_code(self):
        """A wrapper with `description = ""` (empty string) falls through to `code`."""
        wrapper = {"description": "", "code": "VU"}
        assert _flatten_label(wrapper) == "VU"


@pytest.mark.iucn
class TestThrottleThreadSafety:
    """`_throttle` serializes concurrent callers via a module-level lock."""

    def test_throttle_acquires_the_module_lock(self, monkeypatch, fake_iucn):
        """`_throttle` enters and exits `_THROTTLE_LOCK` on every call.

        Asserting on `time.sleep` calls is not a real proof of thread safety
        in Python: the read-update of `_CALLED_ONCE` is short enough that
        the GIL almost always serializes it even without a lock. So we
        instead test the strict contract the M2 fix introduced — every
        `_throttle()` invocation must acquire and release the shared lock.

        Verified by replacing `_THROTTLE_LOCK` with an instrumented mock
        whose `__enter__`/`__exit__` count their invocations: a future
        contributor who removes the `with _THROTTLE_LOCK:` block leaves
        `enters == 0` and the test fails loudly.
        """
        from earthlens.iucn import _rest

        enters = 0
        exits = 0

        class _CountingLock:
            def __enter__(self_inner):
                nonlocal enters
                enters += 1
                return self_inner

            def __exit__(self_inner, *exc):
                nonlocal exits
                exits += 1
                return False

        monkeypatch.setattr(_rest, "_THROTTLE_LOCK", _CountingLock())
        _rest._throttle()
        _rest._throttle()
        assert enters == 2, (
            f"_throttle did not acquire _THROTTLE_LOCK; got {enters} enters. "
            "A future contributor likely removed the `with _THROTTLE_LOCK:` "
            "block — restore it to keep the throttle thread-safe."
        )
        assert exits == 2, f"lock left in acquired state; got {exits} exits"

    def test_throttle_under_concurrency_returns_cleanly(self, fake_iucn):
        """Two threads calling `_throttle` concurrently both return.

        The Python GIL plus the lock makes the read-modify-write of the
        shared state safe; this test is the smoke check that the lock does
        not deadlock or starve a thread under contention.
        """
        import threading

        from earthlens.iucn._rest import _throttle

        completed: list[int] = []
        barrier = threading.Barrier(2)

        def worker(index: int) -> None:
            barrier.wait()
            _throttle()
            completed.append(index)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert sorted(completed) == [0, 1], (
            f"a thread did not complete; got {completed!r}"
        )


@pytest.mark.iucn
class TestCategory:
    """`_category` reads the flat vs nested `red_list_category` shapes."""

    def test_nested_detail_shape(self):
        """The detail body's nested `red_list_category.code` is read."""
        assert _category({"red_list_category": {"code": "VU"}}) == "VU"

    def test_flat_summary_shape(self):
        """The summary list's flat `red_list_category_code` is read."""
        assert _category({"red_list_category_code": "EN"}) == "EN"

    def test_neither_present(self):
        """A missing category yields `None`."""
        assert _category({}) is None
