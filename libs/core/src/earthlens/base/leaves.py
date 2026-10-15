"""Tiny shared base classes for per-variable / per-band catalog rows.

Only carries the slivers of behavior that genuinely repeat across
backends — most leaf metadata is domain-specific (optical bands carry
`wavelength` / `scale` / `offset`; CDS variables carry
`cds_variable` / `cds_pressure_level`; CHIRPS variables carry
`description` / `units`).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

#: Collection types rendered as a count (`12 bands`) rather than dumped in full.
_SIZED = (dict, list, tuple, set, frozenset)

#: Plurals whose singular is not the word minus a trailing `s`. Latent today —
#: no adopter declares such a field — but `render_fragment` is public, so a
#: future backend declaring `aliases` should not read `1 aliase`.
_IRREGULAR_SINGULARS = {
    "aliases": "alias",
    "analyses": "analysis",
    "axes": "axis",
    "indices": "index",
    "series": "series",
    "species": "species",
}


def _singular(field: str, count: int) -> str:
    """Return the label for a count, singularised when the count is one.

    Args:
        field: The declared field name, normally a plural.
        count: How many items the collection holds.

    Returns:
        str: `field` unchanged unless `count` is 1 and a singular is known.
    """
    if count != 1:
        return field
    if field in _IRREGULAR_SINGULARS:
        return _IRREGULAR_SINGULARS[field]
    # `status`, `analysis`, `class` merely end in `s`; they are already singular.
    if field.endswith(("ss", "us", "is")):
        return field
    return field[:-1] if field.endswith("s") else field


#: Longest single fragment. The longest value a shipped row declares is a
#: 335-character description, and asset ids reach 100; past this width a
#: fragment stops informing and starts crowding out the ones after it.
MAX_FRAGMENT = 60

#: Longest joined summary, so a row with many fragments still reads as one
#: line. The longest shipped summary sits at 189 characters — this cap plus
#: the class name and brackets.
MAX_SUMMARY = 180


#: Fraction of a clipped prose fragment kept as the head. Prose front-loads its
#: meaning, so a title reads better with most of its opening intact — but a
#: tail still has to survive, or two long titles sharing an opening collapse
#: onto the same summary.
_PROSE_HEAD_SHARE = 0.75


def _reads_as_prose(text: str) -> bool:
    """Whether `text` is a sentence rather than an identifier.

    Args:
        text: The fragment being clipped.

    Returns:
        bool: `True` when the text contains a space. Catalog ids and paths
        never do, and a title that happens to carry `/` or `:` is still a
        title — `"Antarctic Ocean - Sea Ice, CFOSAT/SSMI interpolated"` reads
        as prose despite the slash.
    """
    return " " in text


def _clip(text: str, limit: int) -> str:
    """Shorten `text` to `limit` characters, marking that it was cut.

    Always keeps both ends, because what distinguishes two long values is
    often only their tail — `.../weathernext_2_0_0` from
    `..._mean`, or two dataset titles differing in a trailing qualifier.
    Dropping the tail outright collapses such rows onto one summary, which is
    worse than clipping them at all.

    How much of each end survives depends on what the text is. An
    **identifier** is split evenly: a path carries as much meaning at its end
    as its start. **Prose** keeps `_PROSE_HEAD_SHARE` of the budget as head,
    since a title front-loads its meaning and reads as a truncation rather
    than as damage — while still retaining enough tail to tell two similar
    titles apart.

    Args:
        text: The text to shorten.
        limit: Maximum length of the result, including the ellipsis.

    Returns:
        str: `text` unchanged when it fits, else its head and tail joined by
        `...`.
    """
    if len(text) <= limit:
        return text
    keep = limit - 3
    share = _PROSE_HEAD_SHARE if _reads_as_prose(text) else 0.5
    head = max(1, round(keep * share))
    tail = keep - head
    if tail <= 0:
        return text[:keep].rstrip() + "..."
    return text[:head].rstrip() + "..." + text[len(text) - tail :].lstrip()


def render_fragment(value: Any, field: str) -> str:
    """Render one field value for a one-line summary, or `""` to omit it.

    Collapses runs of whitespace, clips to `MAX_FRAGMENT` characters (cutting
    the middle, so both ends survive), and renders a non-empty collection as a
    count labelled with the field name. A `summary_parts` override composing
    its own fragment should call this so it gets the same treatment as a
    declared field.

    Args:
        value: The attribute's value. `None`, an empty string and an empty
            collection all render as `""`; `0`, `0.0` and `False` are real
            values and render as themselves.
        field: The attribute's name, used to label a collection's count.

    Returns:
        str: The rendered fragment, or `""` when the value carries nothing
        worth showing (`None`, an empty string, or an empty collection).

    Examples:
        - A scalar renders as its stripped text:
            ```python
            >>> render_fragment("  K  ", "units")
            'K'

            ```
        - A collection renders as a count labelled with the field:
            ```python
            >>> render_fragment({"b1": 1, "b2": 2}, "bands")
            '2 bands'

            ```
        - Nothing worth showing renders as the empty string:
            ```python
            >>> [render_fragment(v, "units") for v in (None, "", [])]
            ['', '', '']

            ```
        - Zero is a real value, so it survives:
            ```python
            >>> render_fragment(0, "count")
            '0'

            ```
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # A flag reads as its own name when set; when clear it has nothing to
        # say, and `False` beside four other fragments only takes up room.
        return field if value else ""
    if isinstance(value, _SIZED):
        if not value:
            return ""
        count = len(value)
        return f"{count} {_singular(field, count)}"
    # Collapse runs of whitespace so an embedded newline cannot split the
    # summary across lines; no shipped row does this today, but the summary
    # promises to be one line and nothing else enforces it.
    return _clip(" ".join(str(value).split()), MAX_FRAGMENT)


def render_measure(value: float | int | None, unit: str = "m") -> str:
    """Render a bare numeric measure with its unit, or `""` when unknown.

    A nominal resolution stored as a plain number renders as a lone figure
    with nothing saying what it measured. Five row classes needed the same
    two lines to fix that, so it lives here once.

    Args:
        value: The measure, or `None` / `0` when the row does not carry one.
        unit: The unit to append. Defaults to metres.

    Returns:
        str: `"<n> <unit>"` with a trailing zero trimmed, or `""`.

    Examples:
        - A whole number drops its trailing zero:
            ```python
            >>> render_measure(30.0)
            '30 m'

            ```
        - A fractional value keeps its precision, and the unit is free:
            ```python
            >>> render_measure(1113.2), render_measure(3.75, "arc-second")
            ('1113.2 m', '3.75 arc-second')

            ```
        - An absent measure contributes nothing:
            ```python
            >>> render_measure(None)
            ''

            ```
    """
    if not value:
        return ""
    return f"{value:g} {unit}"


class SummarisedLeaf(BaseModel):
    """Catalog row that prints as one readable line instead of a field dump.

    A catalog row is normally read for three things — what it is called,
    what units it is in, and how big or how recent it is. Pydantic's default
    `__str__` answers none of them quickly: it prints every field, including
    the `None`s.

    `None`, empty strings and empty collections are skipped, so a sparse row
    stays short; a non-empty collection renders as a count (`12 bands`).
    Fragments are capped at `MAX_FRAGMENT` characters and the joined summary
    at `MAX_SUMMARY`, both marked with an ellipsis when cut. A subclass
    needing a different shape overrides `summary_parts`.

    Only `__str__` is defined. `__repr__` is deliberately left as pydantic's
    field-complete form: that is the debugging contract, and doctests and log
    lines can depend on its exact text.

    Examples:
        - Declare the fields worth showing, in order, then print a row:
            ```python
            >>> class Dataset(SummarisedLeaf):
            ...     _summary_fields = ("id", "title", "bands")
            ...     id: str
            ...     title: str | None = None
            ...     bands: dict[str, int] = {}
            >>> print(Dataset(id="A/B", title="A title", bands={"b1": 1}))
            Dataset(A/B, A title, 1 band)

            ```
        - A sparse row stays short instead of printing `None` placeholders:
            ```python
            >>> class Dataset(SummarisedLeaf):
            ...     _summary_fields = ("id", "title")
            ...     id: str
            ...     title: str | None = None
            >>> print(Dataset(id="A/B"))
            Dataset(A/B)

            ```
        - `__repr__` still carries every field, so debugging is unaffected:
            ```python
            >>> class Dataset(SummarisedLeaf):
            ...     _summary_fields = ("id",)
            ...     id: str
            ...     title: str | None = None
            >>> row = Dataset(id="A/B")
            >>> str(row)
            'Dataset(A/B)'
            >>> repr(row)
            "Dataset(id='A/B', title=None)"

            ```

    See Also:
        FluxableLeaf: Adds a `flux` / `state` marker for variable rows.
    """

    #: Field names to include in `__str__`, in order. Empty means no summary
    #: beyond the class name, which is the honest answer for a row that has
    #: not declared one. A subclass declaration **replaces** its parent's
    #: rather than extending it — to keep a parent's fragments, override
    #: `summary_parts` and call `super()`, as `FluxableLeaf` does.
    _summary_fields: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a `_summary_fields` spelling pydantic would silently swallow.

        A leading underscore plus a bare annotation — `_summary_fields:
        tuple[str, ...] = (...)` — makes pydantic treat the declaration as a
        private attribute and drop it from the class namespace. Lookup then
        falls through to this base's empty default and the row prints as
        `ClassName()` with no error, no lint warning and no type error. That
        is the one failure this class cannot detect at render time, so it is
        caught at class-creation time instead.

        Args:
            **kwargs: Class-construction keywords, forwarded to the base.

        Raises:
            TypeError: If `_summary_fields` was captured as a private
                attribute, i.e. annotated without `ClassVar`.
        """
        super().__pydantic_init_subclass__(**kwargs)
        near_miss = [
            name
            for name in (*vars(cls), *cls.__private_attributes__)
            if name.startswith("_summary") and name != "_summary_fields"
        ]
        if near_miss:
            raise TypeError(
                f"{cls.__name__} declares {near_miss[0]!r}; the summary is "
                "read from _summary_fields, so this would be ignored and the "
                f"row would print as '{cls.__name__}()'."
            )
        if "_summary_fields" in cls.__private_attributes__:
            raise TypeError(
                f"{cls.__name__}._summary_fields is annotated without "
                "ClassVar, so pydantic captured it as a private attribute and "
                "the summary would silently degrade to "
                f"'{cls.__name__}()'. Declare it bare "
                '(`_summary_fields = ("id", ...)`) or as '
                "`ClassVar[tuple[str, ...]]`."
            )

    def summary_parts(self) -> list[str]:
        """Return the rendered fragments that make up the one-line summary.

        Override to prepend a composed fragment (an `a -> b` identity, say)
        or to append a derived one; call `super().summary_parts()` so a base
        class's contribution is kept.

        Returns:
            list[str]: One fragment per declared field that had a value.

        Examples:
            - Only the populated fields produce a fragment:
                ```python
                >>> class Dataset(SummarisedLeaf):
                ...     _summary_fields = ("id", "title", "provider")
                ...     id: str
                ...     title: str | None = None
                ...     provider: str | None = None
                >>> Dataset(id="A/B", provider="esa").summary_parts()
                ['A/B', 'esa']

                ```
            - Prepend a composed fragment by calling `super()`:
                ```python
                >>> class Variable(SummarisedLeaf):
                ...     _summary_fields = ("units",)
                ...     name: str
                ...     nc_name: str
                ...     units: str
                ...     def summary_parts(self) -> list[str]:
                ...         pair = f"{self.name} -> {self.nc_name}"
                ...         return [pair, *super().summary_parts()]
                >>> print(Variable(name="2m_temperature", nc_name="t2m", units="K"))
                Variable(2m_temperature -> t2m, K)

                ```
        """
        parts = []
        for field in self._summary_fields:
            rendered = render_fragment(getattr(self, field, None), field)
            if rendered:
                parts.append(rendered)
        return parts

    def __str__(self) -> str:
        """Return `ClassName(fragment, fragment, ...)` for `print(row)`.

        Returns:
            str: The one-line summary. The row's own text is reproduced as
            itself, so a title carrying `—` or `≥` keeps it — the same choice
            `earthlens.base.AbstractCatalog.__str__` makes, since
            escaping the text into ASCII sequences is what actually makes a
            summary unreadable. A console on a narrow codepage (`cp1252`) can
            therefore still raise `UnicodeEncodeError` on `print(row)`; that
            is a property of the console, not of this summary.

        Examples:
            - The class names itself, then lists its fragments:
                ```python
                >>> class Band(SummarisedLeaf):
                ...     _summary_fields = ("id", "units")
                ...     id: str
                ...     units: str | None = None
                >>> str(Band(id="B1", units="K"))
                'Band(B1, K)'

                ```
            - A row with nothing to show still names itself honestly:
                ```python
                >>> class Band(SummarisedLeaf):
                ...     id: str = "B1"
                >>> str(Band())
                'Band()'

                ```
        """
        parts = self.summary_parts()
        kept: list[str] = []
        used = 0
        for part in parts:
            extra = len(part) + (2 if kept else 0)
            if used + extra > MAX_SUMMARY:
                break
            kept.append(part)
            used += extra
        body = ", ".join(kept)
        if len(kept) < len(parts):
            # Cut on a fragment boundary rather than mid-word: splicing a
            # comma-separated list is what makes a clipped summary unreadable.
            body = f"{body}, ..." if body else "..."
        return f"{type(self).__name__}({body})"


class FluxableLeaf(SummarisedLeaf):
    """Catalog row that flags whether its quantity accumulates over time.

    Both ECMWF `earthlens.ecmwf.Variable` and CHIRPS
    `earthlens.chc.Variable` carry an identical `types` field
    plus `is_flux` property — flux quantities (precipitation,
    evapotranspiration, radiation) are accumulated per timestep on
    the server side, so monthly aggregation has to multiply by the
    number of days in the month. State / instantaneous values
    (temperature, pressure) don't need that scaling.

    GEE `earthlens.gee.Band` does NOT inherit from this — its
    raster bands don't carry flux semantics (cloud-screened optical
    reflectance, NDVI, etc.).

    Attributes:
        types: `"flux"` for accumulated quantities, `None` (the
            default) for state / instantaneous values. Concrete
            subclasses may narrow to a `Literal` if they enumerate
            other values.

    Examples:
        - A flux row is marked as such in its summary:
            ```python
            >>> class Variable(FluxableLeaf):
            ...     _summary_fields = ("units",)
            ...     units: str
            >>> print(Variable(units="mm", types="flux"))
            Variable(mm, flux)

            ```
        - Anything that is not exactly `"flux"` reads as state:
            ```python
            >>> class Variable(FluxableLeaf):
            ...     _summary_fields = ("units",)
            ...     units: str
            >>> row = Variable(units="K")
            >>> row.is_flux
            False
            >>> print(row)
            Variable(K, state)

            ```

    See Also:
        SummarisedLeaf: The one-line `__str__` this builds on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    types: str | None = None

    @property
    def is_flux(self) -> bool:
        """`True` when `types == "flux"`; drives monthly accumulation scaling.

        Returns:
            bool: Whether the quantity accumulates over the timestep.

        Examples:
            - An accumulated quantity is a flux:
                ```python
                >>> FluxableLeaf(types="flux").is_flux
                True

                ```
            - An instantaneous quantity, and the default, are not:
                ```python
                >>> [FluxableLeaf(types=t).is_flux for t in ("state", None)]
                [False, False]

                ```
        """
        return self.types == "flux"

    def summary_parts(self) -> list[str]:
        """Append the `flux` / `state` marker to the declared fields.

        `is_flux` is a property rather than a model field, so nothing
        field-driven picks it up — it has to be added explicitly, and it is
        the single most load-bearing fact about a variable row.

        Returns:
            list[str]: The declared fragments, then `"flux"` or `"state"`.

        Examples:
            - The marker is appended, so declared fields keep their order:
                ```python
                >>> class Variable(FluxableLeaf):
                ...     _summary_fields = ("units",)
                ...     units: str
                >>> Variable(units="mm", types="flux").summary_parts()
                ['mm', 'flux']

                ```
            - A row with no declared fields still reports its kind:
                ```python
                >>> FluxableLeaf().summary_parts()
                ['state']

                ```
        """
        return [*super().summary_parts(), "flux" if self.is_flux else "state"]
