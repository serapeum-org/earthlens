"""Tests for the `PropertyFilter` value object (Task 4.8)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee import PropertyFilter


class TestPropertyFilter:
    """Construction validates once; `to_ogr` hands the reader the checked string."""

    @pytest.mark.parametrize(
        "expression, expected",
        [
            ("CLOUDY_PIXEL_PERCENTAGE < 20", "CLOUDY_PIXEL_PERCENTAGE < 20"),
            ("  CLOUDY_PIXEL_PERCENTAGE < 20  ", "CLOUDY_PIXEL_PERCENTAGE < 20"),
            ("((A > 1) AND (B < 2)) OR C = 3", "((A > 1) AND (B < 2)) OR C = 3"),
            ("PRODUCT_ID = 'a;b'", "PRODUCT_ID = 'a;b'"),
            ("PRODUCT_ID = 'a--b'", "PRODUCT_ID = 'a--b'"),
            ("NAME = 'O''Brien'", "NAME = 'O''Brien'"),
        ],
    )
    def test_well_formed_filter_round_trips(self, expression, expected):
        """A legitimate expression validates and `to_ogr` returns it stripped."""
        assert PropertyFilter(expression=expression).to_ogr() == expected

    @pytest.mark.parametrize(
        "bad, match",
        [
            ("   ", "must not be blank"),
            ("NAME = 'abc", "unterminated quote"),
            ('NAME = "abc', "unterminated quote"),
            ("(CLOUD < 20", "unclosed"),
            ("CLOUD < 20; DROP", "single expression"),
            ("CLOUD < 20 -- rest", "single expression"),
            ("1=1) OR (1=1", "never opened"),
            (")(", "never opened"),
        ],
    )
    def test_malformed_filter_is_rejected(self, bad, match):
        """Each malformation raises at construction with its own message."""
        with pytest.raises(ValidationError, match=match):
            PropertyFilter(expression=bad)

    def test_non_string_is_rejected(self):
        """A non-string names the expected OGR shape rather than a type code."""
        with pytest.raises(ValidationError, match="OGR attribute-filter string"):
            PropertyFilter(expression=20)

    def test_is_frozen(self):
        """The value object is immutable."""
        pf = PropertyFilter(expression="A > 1")
        with pytest.raises(ValidationError):
            pf.expression = "B > 2"

    def test_rejects_unknown_fields(self):
        """`extra="forbid"` refuses a stray keyword."""
        with pytest.raises(ValidationError):
            PropertyFilter(expression="A > 1", extra_field=1)

    def test_equality_is_by_value(self):
        """Equality tracks the expression: same compares equal, different unequal."""
        one = PropertyFilter(expression="A > 1")
        same = PropertyFilter(expression="A > 1")
        different = PropertyFilter(expression="B > 2")
        assert one == same
        assert one != different
