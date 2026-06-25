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


@pytest.mark.iucn
class TestParseRetryAfter:
    """`_parse_retry_after` accepts seconds and HTTP-date forms."""

    def test_seconds_integer(self):
        """An integer number of seconds parses to a float."""
        from earthlens.iucn._rest import _parse_retry_after

        assert _parse_retry_after("7") == 7.0

    def test_none_and_empty(self):
        """`None` or an empty string yields `None`."""
        from earthlens.iucn._rest import _parse_retry_after

        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None

    def test_http_date_far_future(self):
        """An HTTP-date in the distant future yields a positive `delta` in seconds."""
        from earthlens.iucn._rest import _parse_retry_after

        wait = _parse_retry_after("Fri, 31 Dec 2099 23:59:59 GMT")
        assert wait is not None and wait > 365 * 24 * 3600

    def test_http_date_past_clamps_to_zero(self):
        """An HTTP-date already in the past yields `0.0`, not a negative wait."""
        from earthlens.iucn._rest import _parse_retry_after

        assert _parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT") == 0.0

    def test_malformed_value_is_none(self):
        """An unparseable value yields `None` so the caller can fall back."""
        from earthlens.iucn._rest import _parse_retry_after

        assert _parse_retry_after("definitely not a date") is None


@pytest.mark.iucn
class TestThrottleThreadSafety:
    """`_throttle` serializes concurrent callers via a module-level lock."""

    def test_concurrent_calls_serialise(self, fake_iucn):
        """Two threads calling `_throttle` race through the lock cleanly.

        Without the lock both threads could read `_CALLED_ONCE = False`,
        skip the wait, and update `_LAST_CALL_MONOTONIC` non-monotonically.
        With the lock the second thread always observes the first's update
        and the resulting state reflects two calls (call count 2).
        """
        import threading

        from earthlens.iucn._rest import _throttle

        counts = {"calls": 0}
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            _throttle()
            counts["calls"] += 1

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert counts["calls"] == 2


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
