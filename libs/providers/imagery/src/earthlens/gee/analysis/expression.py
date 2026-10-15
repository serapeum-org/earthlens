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
- the functions in `ALLOWED_FUNCTIONS`, which are numpy element-wise ones
  chosen for raster work.

Anything else is a `ValueError` naming the construct, at construction time
rather than when the operation runs.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np
from pydantic import field_validator, model_validator

from earthlens.gee.analysis._arrays import read_masked, result_like, validate_no_data
from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["ALLOWED_FUNCTIONS", "BandExpression"]

#: The element-wise numpy functions a formula may call. Reductions are absent on
#: purpose: `np.mean` over a band would collapse the raster to a scalar and the
#: result could not go back on the grid.
ALLOWED_FUNCTIONS: dict[str, Any] = {
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

_BINARY_OPERATORS: dict[type[ast.operator], Any] = {
    ast.Add: np.add,
    ast.Sub: np.subtract,
    ast.Mult: np.multiply,
    ast.Div: np.divide,
    ast.FloorDiv: np.floor_divide,
    ast.Mod: np.mod,
    ast.Pow: np.power,
}

_COMPARISONS: dict[type[ast.cmpop], Any] = {
    ast.Eq: np.equal,
    ast.NotEq: np.not_equal,
    ast.Lt: np.less,
    ast.LtE: np.less_equal,
    ast.Gt: np.greater,
    ast.GtE: np.greater_equal,
}

_BOOLEAN_OPERATORS: dict[type[ast.operator], Any] = {
    ast.BitAnd: np.logical_and,
    ast.BitOr: np.logical_or,
    ast.BitXor: np.logical_xor,
}


def _check(node: ast.AST) -> None:
    """Raise if `node` is not an allowed construct.

    Args:
        node: Any node of the parsed formula.

    Raises:
        ValueError: If the node is a construct formulas may not contain, or a
            call to something outside `ALLOWED_FUNCTIONS`.
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
        if not isinstance(node.func, ast.Name):
            raise ValueError("only direct calls to the allowed functions are permitted")
        if node.func.id not in ALLOWED_FUNCTIONS:
            raise ValueError(
                f"function {node.func.id!r} is not allowed; "
                f"choose from {sorted(ALLOWED_FUNCTIONS)}"
            )
        if node.keywords:
            raise ValueError(
                f"{node.func.id}() takes positional arguments only in a formula"
            )
        for argument in node.args:
            _check(argument)
        return
    raise ValueError(f"{type(node).__name__} is not allowed in a band expression")


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
    # `_check` admits nothing else, so this is a call.
    call = ast.cast(ast.Call, node) if hasattr(ast, "cast") else node
    function = ALLOWED_FUNCTIONS[call.func.id]  # type: ignore[union-attr]
    return function(*(_evaluate(argument, bands) for argument in call.args))  # type: ignore[union-attr]


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
    """

    formula: str
    dtype: str = "float32"
    no_data: float | int | None = None

    #: Parsed formulas, keyed by text. Parsing is cheap but the validator runs
    #: on every construction, and an operation is often rebuilt in a loop.
    _CACHE: ClassVar[dict[str, ast.Expression]] = {}

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
        if not value.strip():
            raise ValueError("formula is empty")
        try:
            tree = ast.parse(value, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"formula does not parse: {exc.msg}") from exc
        _check(tree)
        cls._CACHE[value] = tree
        return value

    @model_validator(mode="after")
    def _sentinel_fits_the_dtype(self) -> BandExpression:
        """Refuse a sentinel the output dtype cannot store, at construction.

        Returns:
            The operation, once its sentinel is representable.

        Raises:
            ValueError: If `no_data` cannot be stored in `dtype` - NaN in an
                integer band is cast to 0, which collides with a real class, so
                it has to fail here rather than silently at apply time.
        """
        validate_no_data(self.no_data, self.dtype)
        return self

    def _tree(self) -> ast.Expression:
        """Return the parsed formula, parsing again if the cache was cleared."""
        tree = type(self)._CACHE.get(self.formula)
        if tree is None:
            tree = ast.parse(self.formula, mode="eval")
            _check(tree)
            type(self)._CACHE[self.formula] = tree
        return tree

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
            A new single-band raster on `ds`' grid.

        Raises:
            KeyError: If the formula names a band the raster does not carry.
        """
        bands: dict[str, Any] = {}
        names = list(ds.band_names or [])
        for index in range(ds.band_count):
            # Masked, so a formula never sees the sentinel as a value: without
            # this `b0 > 0` is false for a void only by accident of its sign.
            array = read_masked(ds, index)
            bands[f"b{index}"] = array
            if index < len(names):
                bands[names[index]] = array
        result = _evaluate(self._tree(), bands)
        # A formula of literals only (`1 + 1`) evaluates to a scalar; broadcast
        # it so the output is still a raster rather than failing on write.
        if np.isscalar(result) or np.asarray(result).ndim == 0:
            result = np.full((ds.rows, ds.columns), result)
        return result_like(ds, result, dtype=self.dtype, no_data=self.no_data)
