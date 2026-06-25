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
