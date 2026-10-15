"""Tests for the band-expression compiler and what it refuses."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis.expression import ALLOWED_FUNCTIONS, BandExpression

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _two_band(first: float = 0.6, second: float = 0.2) -> Dataset:
    """Build a two-band raster with constant bands."""
    stack = np.stack(
        [
            np.full((2, 2), first, dtype="float32"),
            np.full((2, 2), second, dtype="float32"),
        ]
    )
    return Dataset.from_array(arr=stack, geo_ref=GEO, no_data_value=np.nan)


def _first(ds: Dataset) -> float:
    """Read the top-left cell of a raster's first band."""
    return float(np.asarray(ds.read_array(), dtype="float64")[0, 0])


#: One working call per allowed function, so the allow-list cannot grow an
#: entry that does not actually compile.
SAMPLE_CALLS = {
    "abs": "abs(b0 - b1)",
    "clip": "clip(b0, 0, 1)",
    "exp": "exp(b0)",
    "log": "log(b0)",
    "log10": "log10(b0)",
    "maximum": "maximum(b0, b1)",
    "minimum": "minimum(b0, b1)",
    "sqrt": "sqrt(b0)",
    "where": "where(b0 > 0.5, b0, b1)",
}

#: Formulas that must not compile, with the construct each one smuggles in.
REFUSED = [
    pytest.param("__import__('os').system('x')", id="import"),
    pytest.param("b0.min()", id="attribute-call"),
    pytest.param("open('f')", id="builtin"),
    pytest.param("[b0 for _ in range(3)]", id="comprehension"),
    pytest.param("lambda: 1", id="lambda"),
    pytest.param("b0 if b0 else b1", id="conditional"),
    pytest.param("b0 and b1", id="boolop"),
    pytest.param("'text'", id="string-literal"),
    pytest.param("~b0", id="unary-invert"),
    pytest.param("clip(b0, a_min=0, a_max=1)", id="keyword-argument"),
    pytest.param("b0 +", id="syntax-error"),
    pytest.param("   ", id="blank"),
]


class TestWhatCompiles:
    """Formulas that are allowed, and what they compute."""

    def test_arithmetic_over_positional_band_names(self):
        """`b0`/`b1` address bands by index, which is the request's order."""
        result = BandExpression(formula="(b0 - b1) / (b0 + b1)").apply(_two_band())
        assert _first(result) == pytest.approx(0.5)

    def test_arithmetic_over_the_rasters_own_band_names(self):
        """A formula may use the names the raster carries."""
        result = BandExpression(formula="(Band_1 - Band_2) / (Band_1 + Band_2)").apply(
            _two_band()
        )
        assert _first(result) == pytest.approx(0.5)

    def test_an_allowed_function_runs(self):
        """The allow-list functions are element-wise numpy ones."""
        result = BandExpression(formula="sqrt(b0)").apply(_two_band(first=0.25))
        assert _first(result) == pytest.approx(0.5)

    def test_a_comparison_yields_a_mask(self):
        """Comparisons make a threshold mask expressible as a formula."""
        result = BandExpression(formula="b0 > 0.5").apply(_two_band())
        assert _first(result) == pytest.approx(1.0)

    def test_masks_combine_with_bitwise_operators(self):
        """`&` and `|` compose masks, which is why they are allowed."""
        result = BandExpression(formula="(b0 > 0.5) & (b1 < 0.5)").apply(_two_band())
        assert _first(result) == pytest.approx(1.0)

    def test_a_formula_of_literals_still_produces_a_raster(self):
        """A constant expression is broadcast rather than failing on write."""
        result = BandExpression(formula="1 + 1").apply(_two_band())
        assert _first(result) == pytest.approx(2.0)
        assert result.rows == 2

    def test_the_output_dtype_is_configurable(self):
        """A mask is better stored as an integer than a float."""
        result = BandExpression(formula="b0 > 0.5", dtype="uint8").apply(_two_band())
        assert np.asarray(result.read_array()).dtype == np.uint8

    def test_the_output_keeps_the_source_grid(self):
        """An expression result is georeferenced like its input."""
        source = _two_band()
        result = BandExpression(formula="b0 * 2").apply(source)
        assert (result.rows, result.columns, result.epsg) == (
            source.rows,
            source.columns,
            source.epsg,
        )

    def test_the_name_carries_the_formula(self):
        """A chain's name should say which expression ran."""
        assert BandExpression(formula="b0 * 2").name == "BandExpression(b0 * 2)"

    def test_every_advertised_function_is_callable_in_a_formula(self):
        """The allow-list and what actually compiles must not drift apart."""
        unused = set(ALLOWED_FUNCTIONS) - set(SAMPLE_CALLS)
        assert unused == set(), f"these allowed functions have no sample call: {unused}"
        for formula in SAMPLE_CALLS.values():
            assert (
                _first(BandExpression(formula=formula).apply(_two_band())) is not None
            )


class TestWhatIsRefused:
    """The allow-list, checked at construction rather than at run time."""

    @pytest.mark.parametrize("formula", REFUSED)
    def test_a_disallowed_formula_fails_at_construction(self, formula):
        """Nothing outside arithmetic compiles, so `eval` is never needed."""
        with pytest.raises(ValidationError):
            BandExpression(formula=formula)

    def test_the_refusal_names_the_construct(self):
        """A rejected formula says what was wrong with it."""
        with pytest.raises(ValidationError, match="ListComp is not allowed"):
            BandExpression(formula="[b0 for _ in range(2)]")

    def test_an_unknown_function_lists_the_allowed_ones(self):
        """The error is actionable rather than just a refusal."""
        with pytest.raises(ValidationError, match="choose from"):
            BandExpression(formula="median(b0)")

    def test_a_missing_band_is_named_when_the_formula_runs(self):
        """A formula cannot know the raster's band count until it is applied."""
        with pytest.raises(KeyError, match=r"'b7'.*not a band"):
            BandExpression(formula="b7 * 2").apply(_two_band())

    def test_no_formula_can_reach_a_builtin(self):
        """The evaluator walks the tree itself, so builtins are unreachable."""
        with pytest.raises(ValidationError):
            BandExpression(formula="len(b0)")
