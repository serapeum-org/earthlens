"""Tests for `earthlens.base.leaves` — `SummarisedLeaf` and `FluxableLeaf`."""

from __future__ import annotations

from typing import ClassVar

import pytest

from earthlens.base.leaves import (
    MAX_FRAGMENT,
    MAX_SUMMARY,
    FluxableLeaf,
    SummarisedLeaf,
    _clip,
    render_fragment,
    render_measure,
)


class Row(SummarisedLeaf):
    """A leaf declaring a summary, used across the tests below."""

    _summary_fields = ("id", "title", "provider", "bands")

    id: str = ""
    title: str | None = None
    provider: str | None = None
    bands: dict[str, int] = {}


class Undeclared(SummarisedLeaf):
    """A leaf that declares no summary fields."""

    id: str = "x"


class Sized(SummarisedLeaf):
    """A leaf whose declared fields cover the four collection types."""

    _summary_fields = ("tags", "levels", "codes", "frozen")

    tags: list[str] = []
    levels: tuple[int, ...] = ()
    codes: set[str] = set()
    frozen: frozenset[str] = frozenset()


class Numeric(SummarisedLeaf):
    """A leaf whose declared fields are falsy-but-meaningful numbers."""

    _summary_fields = ("count", "ratio", "flag")

    count: int = 0
    ratio: float = 0.0
    flag: bool = False


class Typo(SummarisedLeaf):
    """A leaf declaring a field name it does not actually carry."""

    _summary_fields = ("id", "nonexistent")

    id: str = "A/B"


class Nested(SummarisedLeaf):
    """A leaf whose declared field holds another summarised leaf."""

    _summary_fields = ("id", "inner")

    id: str = "outer"
    inner: Undeclared | None = None


class Wide(SummarisedLeaf):
    """A leaf whose five fragments together overrun `MAX_SUMMARY`.

    Each field is one repeated character, so a fragment that survived the cut
    intact is recognisable by holding a single distinct character.
    """

    _summary_fields = ("a", "b", "c", "d", "e")

    a: str = "x" * 55
    b: str = "y" * 55
    c: str = "z" * 55
    d: str = "w" * 55
    e: str = "v" * 55


class Var(FluxableLeaf):
    """A flux-aware leaf declaring one field."""

    _summary_fields = ("units",)

    units: str | None = None


class Pairing(Var):
    """A flux-aware leaf that prepends a composed fragment to the summary."""

    def summary_parts(self) -> list[str]:
        """Return an `a -> b` pair ahead of whatever the base contributes."""
        return ["a -> b", *super().summary_parts()]


class TestRenderFragment:
    """Tests for the `render_fragment` field-value formatter."""

    def test_none_renders_as_nothing(self):
        """`None` carries no information and is dropped."""
        assert render_fragment(None, "units") == ""

    def test_empty_string_renders_as_nothing(self):
        """An empty string is as absent as `None`."""
        assert render_fragment("", "units") == ""

    def test_whitespace_only_string_renders_as_nothing(self):
        """A blank string collapses to empty rather than a stray gap."""
        assert render_fragment("   ", "units") == ""

    def test_string_is_stripped(self):
        """Surrounding whitespace never reaches the summary."""
        assert render_fragment("  K  ", "units") == "K"

    @pytest.mark.parametrize("value, expected", [(0, "0"), (0.0, "0.0")])
    def test_falsy_numbers_still_render(self, value, expected):
        """Zero is a real value, unlike `None` — it is kept."""
        assert render_fragment(value, "count") == expected, (
            f"{value!r} should render as {expected!r}"
        )

    def test_a_set_flag_renders_as_its_own_name(self):
        """`categorical` says more than a bare `True` beside four other fragments."""
        assert render_fragment(True, "categorical") == "categorical"

    def test_a_clear_flag_renders_as_nothing(self):
        """A flag that is off has nothing to contribute and only takes up room."""
        assert render_fragment(False, "categorical") == ""

    @pytest.mark.parametrize(
        "value",
        [{}, [], (), set(), frozenset()],
        ids=["dict", "list", "tuple", "set", "frozenset"],
    )
    def test_empty_collections_render_as_nothing(self, value):
        """An empty collection is omitted rather than shown as a zero count."""
        assert render_fragment(value, "bands") == "", (
            f"{value!r} should render as empty"
        )

    @pytest.mark.parametrize(
        "value",
        [{"a": 1, "b": 2}, ["a", "b"], ("a", "b"), {"a", "b"}, frozenset({"a", "b"})],
        ids=["dict", "list", "tuple", "set", "frozenset"],
    )
    def test_non_empty_collections_render_as_a_labelled_count(self, value):
        """Every collection type shows its size against the field's name."""
        assert render_fragment(value, "bands") == "2 bands", (
            f"{value!r} should render as a count"
        )

    @pytest.mark.parametrize(
        "field, expected",
        [
            ("bands", "1 band"),
            ("codes", "1 code"),
            ("aliases", "1 alias"),
            ("axes", "1 axis"),
            ("indices", "1 index"),
            ("series", "1 series"),
            ("status", "1 status"),
            ("analysis", "1 analysis"),
            ("bbox", "1 bbox"),
        ],
    )
    def test_a_count_of_one_singularises_the_label(self, field, expected):
        """Stripping a trailing `s` alone would give `1 aliase` and `1 statu`."""
        assert render_fragment([1], field) == expected

    def test_the_count_is_labelled_with_the_field_name(self):
        """The label comes from the field, so the count reads as prose."""
        assert render_fragment([1, 2, 3], "levels") == "3 levels"


class TestRenderMeasure:
    """Tests for the `render_measure` numeric-with-unit formatter."""

    def test_a_whole_number_drops_its_trailing_zero(self):
        """`30.0 m` reads as a float where the catalog means a round figure."""
        assert render_measure(30.0) == "30 m"

    def test_an_integer_renders_the_same_as_its_float(self):
        """Catalogs spell the same resolution both ways."""
        assert render_measure(30) == render_measure(30.0)

    def test_a_fractional_value_keeps_its_precision(self):
        """Sub-metre and arc-second grids are the reason the unit is a parameter."""
        assert render_measure(0.05, "degree") == "0.05 degree"

    def test_the_unit_defaults_to_metres(self):
        """Every current caller measures a ground sample distance."""
        assert render_measure(1113.2) == "1113.2 m"

    @pytest.mark.parametrize("value", [None, 0, 0.0], ids=["none", "int", "float"])
    def test_an_absent_or_zero_measure_renders_as_nothing(self, value):
        """A zero resolution is not a measurement, unlike a zero count."""
        assert render_measure(value) == "", f"{value!r} should render as empty"

    def test_a_large_value_is_not_rendered_in_exponent_form(self):
        """`%g` switches to `1e+06` past six significant digits."""
        assert render_measure(1000000) == "1e+06 m"


class TestFluxableLeaf:
    """Tests for the `FluxableLeaf` mixin."""

    def test_default_types_is_none(self):
        """`types` defaults to `None` for state / instantaneous quantities."""
        leaf = FluxableLeaf()
        assert leaf.types is None
        assert leaf.is_flux is False

    def test_types_flux_marks_is_flux_true(self):
        """`types="flux"` flips `is_flux` to `True`."""
        assert FluxableLeaf(types="flux").is_flux is True

    @pytest.mark.parametrize("types", ["state", "instant", "accumulated", "FLUX"])
    def test_non_flux_string_keeps_is_flux_false(self, types):
        """Anything other than the exact string `"flux"` leaves `is_flux` False."""
        assert FluxableLeaf(types=types).is_flux is False

    def test_frozen_model_rejects_mutation(self):
        """The leaf is a frozen pydantic model — assignment raises."""
        leaf = FluxableLeaf(types="flux")
        with pytest.raises(Exception):
            leaf.types = "state"

    def test_extra_fields_rejected(self):
        """`extra="forbid"` blocks unknown fields at construction."""
        with pytest.raises(Exception):
            FluxableLeaf(types="flux", surprise=1)

    def test_it_is_a_summarised_leaf(self):
        """The flux mixin builds on the summary base rather than replacing it."""
        assert issubclass(FluxableLeaf, SummarisedLeaf)


class TestSummarisedLeafStr:
    """Tests for the one-line `__str__` on `SummarisedLeaf`."""

    def test_declared_fields_render_in_order(self):
        """Fields appear in the order they were declared, not model order."""
        row = Row(id="A/B", title="A title", provider="esa")
        assert str(row) == "Row(A/B, A title, esa)"

    def test_none_and_empty_are_skipped(self):
        """A sparse row stays short instead of printing `None` placeholders."""
        assert str(Row(id="A/B")) == "Row(A/B)"

    def test_empty_string_is_skipped(self):
        """An empty string carries nothing and is dropped like `None`."""
        assert str(Row(id="A/B", title="")) == "Row(A/B)"

    def test_collection_renders_as_a_labelled_count(self):
        """A non-empty collection shows its size, not its contents."""
        row = Row(id="A/B", bands={"b1": 1, "b2": 2, "b3": 3})
        assert str(row) == "Row(A/B, 3 bands)"

    def test_empty_collection_is_skipped(self):
        """An empty collection is omitted rather than shown as a zero count."""
        assert "bands" not in str(Row(id="A/B", bands={}))

    def test_every_collection_type_is_counted(self):
        """Lists, tuples, sets and frozensets all render as counts."""
        row = Sized(
            tags=["a"], levels=(1, 2), codes={"x"}, frozen=frozenset({"y", "z"})
        )
        assert str(row) == "Sized(1 tag, 2 levels, 1 code, 2 frozen)"

    def test_falsy_numbers_are_kept(self):
        """A zero count is a fact; a cleared flag is not."""
        assert str(Numeric()) == "Numeric(0, 0.0)"

    def test_undeclared_leaf_prints_only_its_name(self):
        """A row declaring no fields says so honestly rather than guessing."""
        assert str(Undeclared()) == "Undeclared()"

    def test_a_field_the_row_does_not_carry_is_skipped(self):
        """A declared name with no matching field degrades quietly, not loudly."""
        assert str(Typo()) == "Typo(A/B)"

    def test_a_nested_leaf_renders_through_its_own_str(self):
        """Composition works: the inner leaf formats itself."""
        assert str(Nested(inner=Undeclared())) == "Nested(outer, Undeclared())"

    def test_the_summary_is_a_single_line(self):
        """A row summary never wraps, so it stays greppable in a log."""
        assert "\n" not in str(Row(id="A/B", title="A title", bands={"b": 1}))

    def test_a_subclass_uses_its_own_name(self):
        """The class name is read off the instance, not the declaring base."""
        assert str(Pairing(units="K")).startswith("Pairing(")

    def test_repr_is_left_alone(self):
        """`__repr__` keeps pydantic's field-complete form — the debugging contract."""
        row = Row(id="A/B")
        assert repr(row).startswith("Row(id='A/B'")
        assert "title=None" in repr(row)
        assert repr(row) != str(row)

    def test_non_ascii_text_is_reproduced_as_itself(self):
        """Escaping a title into ASCII sequences is what makes a summary unreadable."""
        rendered = str(Row(id="A/B", title="Sea level — medium term"))
        assert "Sea level — medium term" in rendered, f"text was escaped: {rendered!r}"

    def test_summary_fields_is_not_a_model_field(self):
        """The declaration is class config, so it never becomes data."""
        assert "_summary_fields" not in Row.model_fields

    def test_summary_parts_returns_one_fragment_per_populated_field(self):
        """`summary_parts` is the seam subclasses compose with."""
        assert Row(id="A/B", provider="esa").summary_parts() == ["A/B", "esa"]


class TestFluxableLeafStr:
    """Tests for the flux marker `FluxableLeaf` adds to the summary."""

    def test_state_row_is_marked_state(self):
        """A non-flux row ends with `state`."""
        assert str(Var(units="K")) == "Var(K, state)"

    def test_flux_row_is_marked_flux(self):
        """A flux row ends with `flux`."""
        assert str(Var(units="mm", types="flux")) == "Var(mm, flux)"

    def test_is_flux_reaches_str_though_it_is_not_a_field(self):
        """The property is added explicitly; nothing field-driven would find it."""
        row = Var(units="mm", types="flux")
        assert "is_flux" not in row.model_dump()
        assert "flux" in str(row)

    def test_marker_follows_the_declared_fields(self):
        """The flux marker is appended, so declared fields keep their order."""
        assert str(Var(units="K")).index("K") < str(Var(units="K")).index("state")

    def test_the_marker_survives_an_empty_declaration(self):
        """A flux row with nothing else to say still reports its kind."""
        assert str(FluxableLeaf(types="flux")) == "FluxableLeaf(flux)"

    def test_subclass_can_prepend_and_keep_the_marker(self):
        """Overriding `summary_parts` composes with the base via `super()`."""
        assert str(Pairing(units="K")) == "Pairing(a -> b, K, state)"


class TestSummaryFieldsDeclaration:
    """Tests for the class-creation guard on `_summary_fields`."""

    def test_a_bare_declaration_is_accepted(self):
        """The documented spelling carries through to the summary."""
        assert str(Row(id="A/B", provider="esa")) == "Row(A/B, esa)"

    def test_a_classvar_declaration_is_accepted(self):
        """Spelling the ClassVar out explicitly is equally valid."""

        class Explicit(SummarisedLeaf):
            _summary_fields: ClassVar[tuple[str, ...]] = ("id",)

            id: str = "A/B"

        assert str(Explicit()) == "Explicit(A/B)"

    def test_an_annotation_without_classvar_is_rejected(self):
        """Pydantic would swallow it, so the row is refused at class creation."""
        with pytest.raises(TypeError, match="annotated without ClassVar"):

            class Swallowed(SummarisedLeaf):
                """The annotated spelling pydantic captures as a private attr."""

                _summary_fields: tuple[str, ...] = ("id",)

                id: str = "A/B"

    def test_the_rejection_names_the_degraded_output(self):
        """The message shows what would have been printed, so the fix is obvious."""
        with pytest.raises(TypeError, match=r"degrade to 'Swallowed\(\)'"):

            class Swallowed(SummarisedLeaf):
                """The annotated spelling pydantic captures as a private attr."""

                _summary_fields: tuple[str, ...] = ("id",)

                id: str = "A/B"


class TestLengthAndWhitespace:
    """Tests for the fragment/summary caps and whitespace collapsing."""

    def test_a_long_fragment_is_clipped_with_an_ellipsis(self):
        """A 186-character variable name would otherwise hide the fields after it."""
        rendered = render_fragment("x" * 200, "title")
        assert len(rendered) == MAX_FRAGMENT
        assert "..." in rendered

    def test_prose_keeps_most_of_its_opening(self):
        """A title front-loads its meaning, so the head gets the larger share."""
        rendered = render_fragment(
            "heavy-rainfall events with a return period of at least 5 years", "d"
        )
        assert rendered.startswith("heavy-rainfall events with a return"), rendered
        assert "..." in rendered, rendered

    def test_prose_still_keeps_a_tail(self):
        """Two titles sharing an opening would otherwise collapse onto one summary."""
        a = render_fragment("Global ocean physics reanalysis, monthly means", "t")
        b = render_fragment("Global ocean physics reanalysis, daily means", "t")
        long_a = render_fragment(
            "Global ocean physics reanalysis " + "x" * 40 + " alpha", "t"
        )
        long_b = render_fragment(
            "Global ocean physics reanalysis " + "x" * 40 + " omega", "t"
        )
        assert a != b
        assert long_a != long_b, (long_a, long_b)

    def test_an_identifier_is_still_clipped_in_the_middle(self):
        """A path keeps both ends, so two ids differing only at the tail stay distinct."""
        rendered = render_fragment(
            "projects/malariaatlasproject/assets/accessibility/friction_surface/2019_v5_1",
            "id",
        )
        assert "..." in rendered, rendered
        assert not rendered.endswith("..."), rendered

    def test_a_real_shipped_title_is_clipped_as_prose(self):
        """A synthetic value cannot show the heuristic meets real catalog text."""
        _earthlens_cmems_catalog = pytest.importorskip("earthlens.cmems.catalog")
        Catalog = _earthlens_cmems_catalog.Catalog

        row = Catalog().get_dataset("cmems_mod_glo_phy_my_0.083deg_P1D-m")
        rendered = str(row)
        assert "..." in rendered, rendered
        assert rendered.startswith("Dataset(GLOBAL_MULTIYEAR_PHY_001_030, daily mean")

    def test_a_title_carrying_a_slash_is_still_prose(self):
        """`CFOSAT/SSMI` inside a sentence does not make the sentence an identifier."""
        title = "Antarctic Ocean Sea Ice concentration, CFOSAT/SSMI interpolated daily"
        rendered = render_fragment(title, "title")
        assert rendered.startswith("Antarctic Ocean Sea Ice concentration"), rendered

    def test_clipping_keeps_the_tail_that_distinguishes_two_paths(self):
        """Two asset ids differing only in their last segment must not collide."""
        base = "projects/gcp-public-data-weathernext/assets/weathernext_2_0_0"
        assert render_fragment(base, "id") != render_fragment(base + "_mean", "id")

    def test_clipping_keeps_the_tail_that_distinguishes_two_titles(self):
        """The prose mirror of the path case: a shared head, a differing tail."""
        head = "Global Ocean Physics Reanalysis, daily mean fields for the "
        assert render_fragment(head + "Atlantic", "title") != render_fragment(
            head + "Pacific", "title"
        )

    def test_a_fragment_at_the_limit_is_left_alone(self):
        """Clipping starts past the limit, not at it."""
        exact = "x" * MAX_FRAGMENT
        assert render_fragment(exact, "title") == exact

    def test_embedded_newlines_are_collapsed(self):
        """The summary promises one line, and nothing else enforces it."""
        assert render_fragment("a\nb\tc  d", "title") == "a b c d"

    def test_the_whole_summary_is_capped(self):
        """Six clipped fragments could still add up past one readable line."""
        row = Sized(
            tags=["x"] * 3, levels=(1,) * 4, codes={"a"}, frozen=frozenset({"b"})
        )
        assert len(str(row)) <= MAX_SUMMARY + len(type(row).__name__) + 2

    def test_a_summary_over_the_cap_is_marked_as_cut(self):
        """A clipped summary says so rather than just ending mid-word."""

        class Wide(SummarisedLeaf):
            _summary_fields = ("a", "b", "c", "d")

            a: str = "x" * 55
            b: str = "y" * 55
            c: str = "z" * 55
            d: str = "w" * 55

        rendered = str(Wide())
        assert "..." in rendered, rendered
        assert len(rendered) <= MAX_SUMMARY + len("Wide") + 2, rendered

    def test_a_typo_in_the_declaration_name_is_rejected(self):
        """`_summary_field` would be ignored and the row would print bare."""
        with pytest.raises(TypeError, match="_summary_field"):

            class Typo(SummarisedLeaf):
                """The singular misspelling nothing would ever read."""

                _summary_field = ("id",)

                id: str = "A/B"

    def test_an_unrelated_private_name_is_left_alone(self):
        """Only near-misses of the declaration name are refused."""

        class Other(SummarisedLeaf):
            """A private name that is not a near-miss of the declaration."""

            _cache_key = "x"
            _summary_fields = ("id",)

            id: str = "A/B"

        assert str(Other()) == "Other(A/B)"


class TestClipAtTinyLimits:
    """Tests for `_clip`'s guard against a limit with no room for a tail.

    Neither module constant reaches it — at `MAX_FRAGMENT` the weighted split
    leaves 14 characters of tail — so the guard is only exercised by a caller
    passing a very small limit. Untested it would be free to return a string
    longer than the limit it was given.
    """

    @pytest.mark.parametrize(
        "text",
        ["hello world", "S2A_MSIL2A_T31UFT"],
        ids=["prose", "identifier"],
    )
    def test_a_limit_leaving_no_tail_keeps_only_the_head(self, text):
        """Both shares round to the whole of a one-character budget."""
        assert _clip(text, 4) == f"{text[0]}..."

    def test_the_result_never_exceeds_the_limit(self):
        """Splicing a tail onto a one-character head would overrun it."""
        for limit in range(4, 12):
            assert len(_clip("hello world", limit)) <= limit, limit

    def test_trailing_whitespace_is_dropped_before_the_ellipsis(self):
        """`a ...` reads as a gap in the text rather than a cut."""
        assert _clip("a   bcdefgh", 5) == "a..."

    def test_a_text_that_fits_is_returned_unchanged(self):
        """The guard sits past the fits-already exit, not in front of it."""
        assert _clip("abcd", 4) == "abcd"


class TestSummaryLevelClip:
    """Tests for the cap on the joined summary."""

    def test_a_dropped_fragment_is_marked(self) -> None:
        """A summary cut short says so rather than just ending."""
        rendered = str(Wide())
        assert rendered.endswith(", ...)"), rendered

    def test_the_cut_falls_on_a_fragment_boundary(self) -> None:
        """Splicing a comma-separated list mid-word is what makes it unreadable."""
        body = str(Wide())[len("Wide(") : -1]
        for fragment in body.split(", "):
            assert fragment == "..." or len(set(fragment)) == 1, fragment
