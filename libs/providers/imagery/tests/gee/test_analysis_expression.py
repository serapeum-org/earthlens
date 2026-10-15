"""Tests for the band-expression compiler and what it refuses."""

from __future__ import annotations

import warnings

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis.expression import (
    ALLOWED_FUNCTIONS,
    FUNCTION_ARITY,
    BandExpression,
    _parse,
)

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


def _signed_two_band() -> Dataset:
    """A raster whose first band is negative, so `abs` changes the answer."""
    return _two_band(first=-1.0, second=0.2)


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
    pytest.param("abs(b0, b1)", id="arity-too-many"),
    pytest.param("clip(b0, 0)", id="arity-too-few"),
    pytest.param("where(b0)", id="arity-one-of-three"),
    pytest.param("maximum(b0, b1, b1)", id="arity-ufunc-out-slot"),
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
        result = BandExpression(formula="b0 > 0.5", dtype="uint8", no_data=255).apply(
            _two_band()
        )
        assert np.asarray(result.read_array()).dtype == np.uint8

    def test_an_integer_dtype_needs_an_explicit_sentinel(self):
        """NaN cast to an integer band is 0, which collides with a real value."""
        with pytest.raises(ValidationError, match="no_data must be given"):
            BandExpression(formula="b0 > 0.5", dtype="uint8")

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


class TestTheHardenedEdges:
    """Findings from the round-1 review, each pinned by the behaviour it names."""

    def test_an_extra_positional_argument_cannot_overwrite_a_band(self):
        """A numpy ufunc's second slot is `out=`, so arity has to be exact.

        Before the arity check, `abs(b0, b1) + b1` returned 2.0 - the call had
        written the absolute value of band 0 into band 1, which the `+ b1` then
        read back.
        """
        source = _signed_two_band()
        assert _first(
            BandExpression(formula="abs(b0) + b1").apply(source)
        ) == pytest.approx(1.2, rel=1e-5)
        with pytest.raises(ValidationError, match="takes exactly 1 argument"):
            BandExpression(formula="abs(b0, b1) + b1")

    @pytest.mark.parametrize(
        "formula", ["1 / 0", "log(0)", "sqrt(0 - 1)"], ids=["divide", "log", "domain"]
    )
    def test_a_non_finite_result_becomes_no_data_without_warning(self, formula):
        """`inf` must not travel downstream, and no warning may escape.

        A caller running under `-W error` would otherwise crash on `log(0)`,
        and an `inf` in a raster poisons every statistic computed from it.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = BandExpression(formula=formula).apply(_two_band())
        assert np.isnan(_first(result))

    def test_the_allow_list_cannot_be_extended_at_runtime(self):
        """The no-escape promise would be worthless if a caller could add to it."""
        with pytest.raises(TypeError):
            ALLOWED_FUNCTIONS["system"] = print  # type: ignore[index]

    def test_every_allowed_function_has_a_declared_arity(self):
        """A new function without an arity would be callable with any signature."""
        assert set(ALLOWED_FUNCTIONS) == set(FUNCTION_ARITY)

    def test_the_parse_cache_is_bounded(self):
        """An unbounded cache grew by one entry per distinct formula, forever."""
        for offset in range(400):
            BandExpression(formula=f"b0 + {offset}")
        assert _parse.cache_info().currsize <= 256

    def test_an_unknown_dtype_is_refused_at_construction(self):
        """A typo in the dtype should not wait for a raster to surface."""
        with pytest.raises(ValidationError, match="not a numpy dtype"):
            BandExpression(formula="b0", dtype="flaot32")

    def test_a_band_name_colliding_with_the_positional_namespace_is_refused(self):
        """A band called `b1` at index 0 made `b1` silently read band 1."""
        source = _two_band()
        source.band_names = ["b1", "other"]
        with pytest.raises(ValueError, match="collide with the positional namespace"):
            BandExpression(formula="b1").apply(source)
