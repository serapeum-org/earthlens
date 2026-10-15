"""Arithmetic over named bands, compiled from a formula with no `eval`.

`BandExpression(formula="(b0 - b1) / (b0 + b1)")` is the general case the index
operations are presets of. The formula is parsed with `ast`, checked against an
allow-list, and evaluated by walking the tree - `eval` is never called, so a
formula cannot reach an attribute, a builtin, an import or a comprehension even
if a caller passes one in from outside the process.

What a formula may contain:

- band references, either `b0` / `b1` positionally or the raster's own band
  names (`Band_1`, or whatever the file carries);
- integer and float literals;
- `+`, `-`, `*`, `/`, `//`, `%`, `**`, unary `+`/`-`;
- comparisons and `&` / `|` over them, so a mask is expressible;
- the functions in `ALLOWED_FUNCTIONS`, called with exactly the number of
  arguments `FUNCTION_ARITY` gives.

Anything else is a `ValueError` naming the construct, at construction time
rather than when the operation runs.

**Why arity is checked and not just the name.** Every function here is a numpy
ufunc, and a ufunc's second positional argument is `out=` - an in-place write
target. `abs(b0, b1)` therefore means "take the absolute value of band 0 and
overwrite band 1 with it", so a formula could corrupt the array a later
subexpression reads: `abs(b0, b1) + b1` returned 2.0 where `abs(b0) + b1` is
1.2. Allowing the name without fixing the signature was not enough.

**Numerical hygiene.** Division by zero and domain errors produce non-finite
values, and those become the result's no-data rather than `inf` or `-inf`
travelling downstream. Warnings are suppressed while the formula runs, because
a library should not raise `RuntimeWarning`s out of a caller's arithmetic - and
a caller running under `-W error` would otherwise crash on `log(0)`. This
matches `NormalizedDifference`, which masks a zero denominator before dividing.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import field_validator, model_validator

from earthlens.gee.analysis._arrays import read_masked, result_like, validate_no_data
from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["ALLOWED_FUNCTIONS", "FUNCTION_ARITY", "BandExpression"]

#: The element-wise numpy functions a formula may call. Reductions are absent on
#: purpose: `np.mean` over a band would collapse the raster to a scalar and the
#: result could not go back on the grid. Read-only, so the no-escape promise
#: cannot be undone by a caller assigning a new entry.
ALLOWED_FUNCTIONS = MappingProxyType(
    {
        "abs": np.absolute,
        "clip": np.clip,
        "exp": np.exp,
        "log": np.log,
        "log10": np.log10,
        "maximum": np.maximum,
        "minimum": np.minimum,
        "sqrt": np.sqrt,
        "where": np.where,
    }
)

#: How many arguments each allowed function takes. Exact, not a minimum: the
#: extra positional slots of a numpy ufunc are `out=` and friends, which would
#: let a formula write into a band it is also reading.
FUNCTION_ARITY = MappingProxyType(
    {
        "abs": 1,
        "clip": 3,
        "exp": 1,
        "log": 1,
        "log10": 1,
        "maximum": 2,
        "minimum": 2,
        "sqrt": 1,
        "where": 3,
    }
)

_BINARY_OPERATORS = MappingProxyType(
    {
        ast.Add: np.add,
        ast.Sub: np.subtract,
        ast.Mult: np.multiply,
        ast.Div: np.divide,
        ast.FloorDiv: np.floor_divide,
        ast.Mod: np.mod,
        ast.Pow: np.power,
    }
)

_COMPARISONS = MappingProxyType(
    {
        ast.Eq: np.equal,
        ast.NotEq: np.not_equal,
        ast.Lt: np.less,
        ast.LtE: np.less_equal,
        ast.Gt: np.greater,
        ast.GtE: np.greater_equal,
    }
)

_BOOLEAN_OPERATORS = MappingProxyType(
    {
        ast.BitAnd: np.logical_and,
        ast.BitOr: np.logical_or,
        ast.BitXor: np.logical_xor,
    }
)


def _check_call(node: ast.Call) -> None:
    """Raise unless `node` calls an allowed function with the right arity.

    Args:
        node: The call node.

    Raises:
        ValueError: If the target is not a plain allowed name, if keywords or
            starred arguments are used, or if the argument count is wrong - a
            numpy ufunc's extra positional slot is `out=`, so an over-long call
            writes into a band instead of reading it.
    """
    if not isinstance(node.func, ast.Name):
        raise ValueError("only direct calls to the allowed functions are permitted")
    name = node.func.id
    if name not in ALLOWED_FUNCTIONS:
        raise ValueError(
            f"function {name!r} is not allowed; choose from {sorted(ALLOWED_FUNCTIONS)}"
        )
    if node.keywords:
        raise ValueError(f"{name}() takes positional arguments only in a formula")
    if any(isinstance(argument, ast.Starred) for argument in node.args):
        raise ValueError(f"{name}() cannot be called with a starred argument")
    expected = FUNCTION_ARITY[name]
    if len(node.args) != expected:
        raise ValueError(
            f"{name}() takes exactly {expected} argument(s) in a formula, got "
            f"{len(node.args)}; a numpy function's extra positional argument is "
            "an output buffer, which would overwrite a band being read"
        )
    for argument in node.args:
        _check(argument)


def _check(node: ast.AST) -> None:
    """Raise if `node` is not an allowed construct.

    Args:
        node: Any node of the parsed formula.

    Raises:
        ValueError: If the node is a construct formulas may not contain, a call
            to something outside `ALLOWED_FUNCTIONS`, or a call with the wrong
            number of arguments.
    """
    if isinstance(node, ast.Expression):
        _check(node.body)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(
                f"only numbers are allowed as literals, got {node.value!r}"
            )
        return
    if isinstance(node, ast.Name):
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise ValueError(f"unary {type(node.op).__name__} is not allowed")
        _check(node.operand)
        return
    if isinstance(node, ast.BinOp):
        operator = type(node.op)
        if operator not in _BINARY_OPERATORS and operator not in _BOOLEAN_OPERATORS:
            raise ValueError(f"operator {operator.__name__} is not allowed")
        _check(node.left)
        _check(node.right)
        return
    if isinstance(node, ast.Compare):
        for operator in node.ops:
            if type(operator) not in _COMPARISONS:
                raise ValueError(f"comparison {type(operator).__name__} is not allowed")
        _check(node.left)
        for comparator in node.comparators:
            _check(comparator)
        return
    if isinstance(node, ast.Call):
        _check_call(node)
        return
    raise ValueError(f"{type(node).__name__} is not allowed in a band expression")


@lru_cache(maxsize=256)
def _parse(formula: str) -> ast.Expression:
    """Parse and allow-list a formula, caching the result.

    Bounded, so a long-running process constructing operations in a loop cannot
    grow the cache without limit - the previous version was an unbounded
    `ClassVar` dict that reached 305 entries after 300 distinct formulas.

    Args:
        formula: The formula text.

    Returns:
        The parsed, allow-listed expression tree.

    Raises:
        ValueError: If the formula is blank, does not parse, or is not allowed.
    """
    if not formula.strip():
        raise ValueError("formula is empty")
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"formula does not parse: {exc.msg}") from exc
    _check(tree)
    return tree


def _evaluate(node: ast.AST, bands: dict[str, Any]) -> Any:
    """Compute `node` against the band arrays in `bands`.

    Args:
        node: A node that `_check` has already approved.
        bands: Band arrays by every name they answer to.

    Returns:
        The node's value: an array for anything touching a band, otherwise a
        number.

    Raises:
        KeyError: If the formula names a band the raster does not have.
    """
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, bands)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        try:
            return bands[node.id]
        except KeyError:
            raise KeyError(
                f"the formula refers to {node.id!r}, which is not a band of this "
                f"raster; available: {sorted(bands)}"
            ) from None
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, bands)
        return value if isinstance(node.op, ast.UAdd) else np.negative(value)
    if isinstance(node, ast.BinOp):
        operator = type(node.op)
        function = _BINARY_OPERATORS.get(operator) or _BOOLEAN_OPERATORS[operator]
        return function(_evaluate(node.left, bands), _evaluate(node.right, bands))
    if isinstance(node, ast.Compare):
        result = None
        left = _evaluate(node.left, bands)
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(comparator, bands)
            comparison = _COMPARISONS[type(operator)](left, right)
            result = (
                comparison if result is None else np.logical_and(result, comparison)
            )
            left = right
        return result
    # `_check` admits nothing else, and `_check_call` has already established
    # that the target is a plain allowed name with the right arity.
    call: ast.Call = node  # type: ignore[assignment]
    target: ast.Name = call.func  # type: ignore[assignment]
    return ALLOWED_FUNCTIONS[target.id](
        *(_evaluate(argument, bands) for argument in call.args)
    )


def _band_namespace(ds: Dataset) -> dict[str, Any]:
    """Map every name a formula may use to that band's masked values.

    Args:
        ds: The source raster.

    Returns:
        Band arrays under `b0`, `b1`, ... and under the raster's own band names.

    Raises:
        ValueError: If a band's own name collides with the positional
            namespace. A band called `b1` sitting at index 0 makes `b1`
            ambiguous, and resolving it silently either way reads the wrong
            band - which is what happened before this check: the positional
            entry won and a formula naming `b1` read band 1.
    """
    names = list(ds.band_names or [])
    positional = {f"b{index}" for index in range(ds.band_count)}
    collisions = sorted(positional.intersection(names))
    if collisions:
        raise ValueError(
            f"band name(s) {collisions} collide with the positional namespace "
            "(b0, b1, ...), so a formula using them would be ambiguous; address "
            "the bands positionally or rename them"
        )
    bands: dict[str, Any] = {}
    for index in range(ds.band_count):
        # Masked, so a formula never sees the sentinel as a value: without this
        # `b0 > 0` is false for a void only by accident of its sign.
        array = read_masked(ds, index)
        bands[f"b{index}"] = array
        if index < len(names):
            bands[names[index]] = array
    return bands


class BandExpression(RasterOperation):
    """Evaluate a formula over a raster's bands and write the result.

    Examples:
        - The NDVI formula written out, over two bands:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> stack = np.stack([
            ...     np.full((2, 2), 0.6, dtype="float32"),
            ...     np.full((2, 2), 0.2, dtype="float32"),
            ... ])
            >>> raster = Dataset.from_array(
            ...     arr=stack,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=np.nan,
            ... )
            >>> out = BandExpression(formula="(b0 - b1) / (b0 + b1)")(raster)
            >>> float(np.asarray(out.read_array())[0, 0])
            0.5

            ```
        - A formula reaching for anything but arithmetic is refused when the
          operation is built, not when it runs:
            ```python
            >>> BandExpression(formula="__import__('os').system('echo hi')")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...
            >>> BandExpression(formula="b0.min()")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
        - And so is a call with the wrong number of arguments, because a numpy
          function's extra positional slot is an output buffer:
            ```python
            >>> BandExpression(formula="abs(b0, b1)")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    formula: str
    dtype: str = "float32"
    no_data: float | int | None = None

    @field_validator("formula")
    @classmethod
    def _parseable_and_allowed(cls, value: str) -> str:
        """Parse the formula and reject anything outside the allow-list.

        Args:
            value: The formula text.

        Returns:
            The formula unchanged, once it is known to be safe.

        Raises:
            ValueError: If the formula does not parse, or contains a construct
                that is not allowed.
        """
        _parse(value)
        return value

    @field_validator("dtype")
    @classmethod
    def _known_dtype(cls, value: str) -> str:
        """Reject a dtype numpy cannot resolve, at construction.

        Args:
            value: The dtype name.

        Returns:
            The dtype unchanged when numpy knows it.

        Raises:
            ValueError: If numpy cannot resolve the name, which would otherwise
                surface only once a raster was passed in.
        """
        try:
            np.dtype(value)
        except TypeError as exc:
            raise ValueError(f"dtype={value!r} is not a numpy dtype") from exc
        return value

    @model_validator(mode="after")
    def _sentinel_fits_the_dtype(self) -> BandExpression:
        """Refuse a sentinel the output dtype cannot store, at construction.

        Returns:
            The operation, once its sentinel is representable.

        Raises:
            ValueError: If `no_data` cannot be stored in `dtype` - NaN in an
                integer band is cast to 0, which collides with a real value.
        """
        validate_no_data(self.no_data, self.dtype)
        return self

    @property
    def name(self) -> str:
        """The operation and its formula, so a chain says what it computed."""
        return f"BandExpression({self.formula})"

    def apply(self, ds: Dataset) -> Dataset:
        """Return a one-band raster holding the formula's result.

        Bands are addressable two ways: positionally as `b0`, `b1`, ... and by
        the raster's own band names, so a formula can read either
        `(b0 - b1)` or `(Band_1 - Band_2)`.

        Args:
            ds: The source raster.

        Returns:
            A new single-band raster on `ds`' grid. A non-finite result - from a
            zero denominator, from `log(0)`, or from a void in an input band -
            is the result's declared no-data rather than an `inf` passed on.

        Raises:
            KeyError: If the formula names a band the raster does not carry.
            ValueError: If a band name collides with the positional namespace,
                or the formula does not evaluate to one value per cell.
        """
        bands = _band_namespace(ds)
        # Suppressed, not ignored: a non-finite result becomes no-data below, so
        # the information is kept while the warning - which would crash a caller
        # running under `-W error` - is not raised out of their arithmetic.
        with np.errstate(all="ignore"):
            result = _evaluate(_parse(self.formula), bands)
        values = np.asarray(result, dtype="float64")
        if values.ndim == 0:
            # A formula of literals only (`1 + 1`) evaluates to a scalar;
            # broadcast it so the output is still a raster.
            values = np.full((ds.rows, ds.columns), float(values))
        if values.shape != (ds.rows, ds.columns):
            raise ValueError(
                f"formula {self.formula!r} produced an array of shape "
                f"{values.shape}, but this raster is {ds.rows}x{ds.columns}; a "
                "formula has to evaluate to one value per cell"
            )
        values = np.where(np.isfinite(values), values, np.nan)
        return result_like(ds, values, dtype=self.dtype, no_data=self.no_data)
