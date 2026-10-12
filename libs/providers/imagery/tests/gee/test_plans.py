"""Tests for `EnginePlan`, the routing verdict promoted from a `NamedTuple`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee.plans import EedaiPlan, EnginePlan


class TestDeclined:
    """The refusal constructor, which 15 of the 18 call sites use."""

    def test_carries_the_reason_and_serves_nothing(self):
        """A refusal has no tiling and a zero unit count."""
        plan = EnginePlan.declined("no native resolution")
        assert plan.can_serve is False
        assert plan.reason == "no native resolution"
        assert plan.tile_size is None
        assert plan.tiles == 0

    def test_defaults_to_the_eedai_engine(self):
        """The only engine that produced plans before this promotion."""
        assert EnginePlan.declined("nope").engine == "eedai"

    def test_names_another_engine_when_given(self):
        """A second engine can say the verdict is its own."""
        assert EnginePlan.declined("nope", engine="ee").engine == "ee"


class TestServes:
    """The acceptance constructor."""

    def test_single_pass_has_no_tile_size(self):
        """One pass means one unit and no tiling."""
        plan = EnginePlan.serves()
        assert plan.can_serve is True
        assert plan.tiles == 1
        assert plan.tile_size is None
        assert plan.reason == ""

    def test_a_tiled_read_carries_both_counts(self):
        """The exporter reads the tiling off the plan rather than re-deriving it."""
        plan = EnginePlan.serves(tiles=4, tile_size=2048)
        assert (plan.tiles, plan.tile_size) == (4, 2048)

    def test_a_collection_plan_carries_scenes_without_a_tile_size(self):
        """For a composite `tiles` counts scenes, so `tile_size` stays `None`."""
        plan = EnginePlan.serves(tiles=142)
        assert plan.tiles == 142
        assert plan.tile_size is None


class TestValueSemantics:
    """What the promotion from a tuple actually buys."""

    def test_is_frozen(self):
        """A routing verdict cannot be edited after the decision was made."""
        plan = EnginePlan.serves(tiles=1)
        with pytest.raises(ValidationError):
            plan.tiles = 9

    def test_rejects_an_unknown_field(self):
        """A mistyped keyword fails instead of becoming a fifth tuple element."""
        with pytest.raises(ValidationError):
            EnginePlan(can_serve=True, tile_count=3)

    def test_rejects_a_negative_unit_count(self):
        """`tiles` is a count, so a negative value is a bug worth catching."""
        with pytest.raises(ValidationError):
            EnginePlan(can_serve=True, tiles=-1)

    def test_equal_plans_compare_equal(self):
        """Value semantics, so two identical verdicts are interchangeable."""
        assert EnginePlan.serves(tiles=2) == EnginePlan.serves(tiles=2)

    def test_fields_are_addressable_by_name(self):
        """What the characterisation suite pinned before the promotion."""
        plan = EnginePlan.serves(tiles=1)
        for field in ("engine", "can_serve", "tile_size", "tiles", "reason"):
            assert hasattr(plan, field), field

    def test_is_no_longer_positionally_constructible(self):
        """The tuple's positional form is gone, which is why the sites moved."""
        with pytest.raises(TypeError):
            EnginePlan(False, None, 0, "nope")


class TestSummary:
    """`SummarisedLeaf` gives the plan a readable one-line form."""

    def test_a_refusal_reads_as_engine_and_reason(self):
        """The two things worth knowing about a decline."""
        assert str(EnginePlan.declined("over the scene cap")) == (
            "EnginePlan(eedai, over the scene cap)"
        )

    def test_an_acceptance_reads_as_engine_and_counts(self):
        """A clear flag renders as its own name; a set one shows the count."""
        assert str(EnginePlan.serves(tiles=4, tile_size=2048)) == (
            "EnginePlan(eedai, can_serve, 4)"
        )

    def test_repr_still_carries_every_field(self):
        """`__repr__` stays pydantic's field-complete debugging contract."""
        text = repr(EnginePlan.serves(tiles=1))
        for field in ("engine", "can_serve", "tile_size", "tiles", "reason"):
            assert field in text


class TestBackCompatAlias:
    """`EedaiPlan` keeps resolving, for callers and `backend.__all__`."""

    def test_the_alias_is_the_promoted_model(self):
        """One class, two names — not a diverging subclass."""
        assert EedaiPlan is EnginePlan

    def test_the_backend_still_exports_the_old_name(self):
        """`backend.__all__` named it, so the name must stay importable."""
        from earthlens.gee import backend

        assert backend.EedaiPlan is EnginePlan
        assert "EedaiPlan" in backend.__all__
