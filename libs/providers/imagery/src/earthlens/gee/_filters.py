"""The validated OGR attribute filter as a value object.

`GEE(property_filter=...)` narrows a collection's scenes on the EEDAI path by an
OGR attribute-filter string (e.g. `"CLOUDY_PIXEL_PERCENTAGE < 20"`). The reader
splices it into a larger filter as `f"{time_filter} AND ({property_filter})"`
without escaping, so a fragment that closes the wrapper early would escape it and
neutralise the time/space clauses. The backend used to carry the raw string and
re-run a procedural scan at construction, then re-interpolate the same string at
every use site.

`PropertyFilter` is that string as a frozen value object: it runs the scan once
in a `model_validator`, so a malformed filter is refused at construction, and it
hands the reader the validated form through `to_ogr`. "Validated" becomes a type
guarantee carried to the reader rather than a check that happened somewhere else.

This is a structural check, not a parser, and **not a security boundary**: build
the filter in code, never from untrusted input.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

__all__ = ["PropertyFilter"]


def _consume_quoted(text: str, index: int, quote: str) -> tuple[int, str | None]:
    """Advance past one character inside a quoted literal.

    Args:
        text: The whole filter.
        index: The character being consumed.
        quote: The quote character that opened the literal.

    Returns:
        The next index, and the quote still in effect (`None` once it closed).
    """
    if text[index] != quote:
        return index + 1, quote
    # A doubled quote is an escaped one, not the end of the literal.
    if text[index + 1 : index + 2] == quote:
        return index + 2, quote
    return index + 1, None


def _scan_property_filter(text: str) -> tuple[int, str | None]:
    """Walk a filter, raising on structure that would escape the reader's wrapper.

    Args:
        text: The stripped filter.

    Returns:
        The final parenthesis depth, and the quote left open (`None` when every
        literal was closed).

    Raises:
        ValueError: It closes a parenthesis it never opened, or carries a bare
            statement separator or SQL comment outside a literal.
    """
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        if quote is not None:
            index, quote = _consume_quoted(text, index, quote)
            continue
        char = text[index]
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == ";" or text.startswith("--", index):
            raise ValueError(
                "property_filter must be a single expression; remove the "
                f"statement separator or comment from {text!r}"
            )
        if depth < 0:
            raise ValueError(
                "property_filter closes a parenthesis it never opened, which "
                f"would escape the filter it is combined with: {text!r}"
            )
        index += 1
    return depth, quote


class PropertyFilter(BaseModel):
    """A structurally validated OGR attribute filter for the EEDAI scene path.

    Construction runs the same scan the backend's free `_validate_property_filter`
    used to, raising the same `ValueError`s for the same malformations (a
    non-string, a blank string, unbalanced parentheses, an unterminated quote, or
    a bare statement separator / SQL comment). A quoted literal is skipped while
    scanning, so a `;` or `--` inside a string value is data, not a second
    statement. This is a structural check, not a security boundary.

    Attributes:
        expression: The OGR attribute-filter string, validated and carried
            verbatim (its surrounding whitespace is stripped by `to_ogr`).

    Examples:
        - A well-formed filter round-trips through `to_ogr`:
            ```python
            >>> PropertyFilter(expression="CLOUDY_PIXEL_PERCENTAGE < 20").to_ogr()
            'CLOUDY_PIXEL_PERCENTAGE < 20'

            ```
        - A fragment that closes the reader's wrapper early is refused:
            ```python
            >>> PropertyFilter(expression="1=1) OR (1=1")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for PropertyFilter
            ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    expression: str

    @field_validator("expression", mode="before")
    @classmethod
    def _require_string(cls, value: object) -> object:
        """Reject a non-string before pydantic's own type message hides the fix.

        Args:
            value: The raw `expression` input.

        Returns:
            The value unchanged when it is a string.

        Raises:
            ValueError: If it is not a string, naming the expected shape.
        """
        if not isinstance(value, str):
            raise ValueError(
                "property_filter must be an OGR attribute-filter string "
                f"(e.g. 'CLOUDY_PIXEL_PERCENTAGE < 20'), got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _scan(self) -> PropertyFilter:
        """Run the structural scan, refusing a filter that would escape the wrapper.

        Returns:
            The validated instance.

        Raises:
            ValueError: If the filter is blank, leaves a parenthesis unclosed,
                has an unterminated quote, closes a parenthesis it never opened,
                or carries a bare statement separator or SQL comment.
        """
        text = self.expression.strip()
        if not text:
            raise ValueError("property_filter must not be blank; pass None instead.")
        depth, quote = _scan_property_filter(text)
        if quote is not None:
            raise ValueError(
                f"property_filter has an unterminated quote: {self.expression!r}"
            )
        if depth:
            raise ValueError(
                "property_filter leaves "
                f"{depth} parenthesis/es unclosed: {self.expression!r}"
            )
        return self

    def to_ogr(self) -> str:
        """Return the validated filter string the reader wraps.

        Returns:
            The expression with surrounding whitespace stripped.
        """
        return self.expression.strip()
