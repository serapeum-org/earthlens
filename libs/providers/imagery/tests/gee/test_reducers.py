"""Tests for the `Reducer` strategy objects and the `REDUCERS` registry."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee.reducers import (
    REDUCERS,
    Max,
    Mean,
    Median,
    Min,
    Mode,
    Mosaic,
    Percentile,
    Reducer,
    Sum,
    resolve_reducer,
)

CONVENIENCE = {
    "mean": Mean,
    "median": Median,
    "min": Min,
    "max": Max,
    "mode": Mode,
    "mosaic": Mosaic,
    "sum": Sum,
}


class _FakeCollection:
    """Records which reducer method the backend dispatched to."""

    def __init__(self, band_names=("B4", "B3")):
        self.called: list[str] = []
        self.reduced_with = None
        self._band_names = list(band_names)

    def first(self):
        """Return self, so `first().bandNames()` works."""
        return self

    def bandNames(self):  # noqa: N802 - mirrors the Earth Engine method name
        """Return the collection's band names."""
        return self._band_names

    def reduce(self, reducer):
        """Record the generic reduce and return a renameable stand-in."""
        self.reduced_with = reducer
        return _FakeReduced(self)

    def __getattr__(self, item):
        def _call():
            self.called.append(item)
            return f"image-{item}"

        return _call


class _FakeReduced:
    """The image `reduce` returns, tracking the rename back to original bands."""

    def __init__(self, collection):
        self.collection = collection
        self.renamed_to = None

    def rename(self, names):
        """Record the rename target and return self."""
        self.renamed_to = names
        return self


class TestRegistry:
    """The registry's contents and what it deliberately omits."""

    def test_the_collection_vocabulary_is_the_seven(self):
        """The band-name-preserving reducers, which `GEE(reducer=)` accepts.

        The registry itself is wider: it also holds the region-only reducers,
        so that one vocabulary covers both the collection and the sampling
        path. Which of them can composite is `collection_reducers()`.
        """
        from earthlens.gee.reducers import collection_reducers

        assert set(collection_reducers()) == set(CONVENIENCE)
        assert set(CONVENIENCE) <= set(REDUCERS)

    @pytest.mark.parametrize(("name", "cls"), CONVENIENCE.items())
    def test_each_name_maps_to_its_class(self, name, cls):
        """Each registry key resolves to the matching reducer class."""
        assert REDUCERS[name] is cls
        assert cls.name == name

    def test_percentile_is_not_in_the_registry(self):
        """It needs an argument, so a bare name could not build it."""
        assert "percentile" not in REDUCERS

    def test_every_reducer_is_a_reducer(self):
        """Each registered class implements the ABC."""
        for cls in REDUCERS.values():
            assert issubclass(cls, Reducer)

    def test_the_base_class_cannot_be_instantiated(self):
        """`__call__` is abstract, so a bare `Reducer` is not usable."""
        with pytest.raises(TypeError):
            Reducer()


class TestConvenienceDispatch:
    """The seven original reducers keep their band-name-preserving semantics."""

    @pytest.mark.parametrize(("name", "cls"), CONVENIENCE.items())
    def test_dispatches_to_the_matching_collection_method(self, name, cls):
        """`Mean()(collection)` calls `collection.mean()`, not `reduce`."""
        collection = _FakeCollection()
        assert cls()(collection) == f"image-{name}"
        assert collection.called == [name]
        assert collection.reduced_with is None, "must not use the renaming form"

    def test_instances_are_callable(self):
        """A reducer satisfies the `collection -> image` shape directly."""
        assert callable(Mean())

    def test_repr_names_the_class(self):
        """A reducer reads cleanly in a log line or a plan."""
        assert repr(Median()) == "Median()"


@pytest.fixture
def fake_ee_reducer(monkeypatch):
    """Patch the reducers module's `ee` binding, as `fake_ee` does for the backend."""
    from types import SimpleNamespace

    from earthlens.gee import reducers as reducers_module

    fake = SimpleNamespace(
        Reducer=SimpleNamespace(percentile=lambda qs: f"percentile-reducer-{qs}")
    )
    monkeypatch.setattr(reducers_module, "ee", fake)
    return fake


class TestPercentile:
    """The parameterised reducer the string-dispatch mechanism could not express."""

    def test_reduces_then_renames_back_to_the_original_bands(self, fake_ee_reducer):
        """`reduce` suffixes band names, so they are restored."""
        collection = _FakeCollection(band_names=("B4", "B3"))
        out = Percentile(q=90)(collection)
        assert collection.reduced_with is not None, "should use the generic reduce"
        assert out.renamed_to == ["B4", "B3"]

    def test_is_frozen(self):
        """The config is a value object, so `q` cannot be mutated after build."""
        reducer = Percentile(q=90)
        with pytest.raises(ValidationError):
            reducer.q = 10

    def test_rejects_an_extra_field(self):
        """`extra="forbid"` catches a mistyped keyword at construction."""
        with pytest.raises(ValidationError):
            Percentile(q=90, quantile=0.9)

    @pytest.mark.parametrize("q", [-1, 0, 101, 1000])
    def test_rejects_out_of_range(self, q):
        """A percentile outside `0 < q <= 100` is rejected at construction.

        `q=0` is refused too: the client-side `reduce_time(op="percentile")`
        path rejects it, so accepting it would defer the failure to mid-run.
        """
        with pytest.raises(ValidationError):
            Percentile(q=q)

    @pytest.mark.parametrize("q", [0.5, 50, 100])
    def test_accepts_the_valid_range(self, q):
        """The smallest positive percentile and the inclusive 100 are valid."""
        assert Percentile(q=q).q == float(q)

    def test_repr_includes_the_percentile(self):
        """Two percentile reducers must not read identically."""
        assert repr(Percentile(q=90)) == "Percentile(q=90.0)"

    def test_the_requested_percentile_reaches_earth_engine(self, fake_ee_reducer):
        """`q` must arrive in the reducer, not just be stored on the object."""
        collection = _FakeCollection()
        Percentile(q=90)(collection)
        assert collection.reduced_with == "percentile-reducer-[90.0]"

    def test_two_equal_percentiles_are_interchangeable(self):
        """Frozen value semantics, so one can be cached or put in a set."""
        from_int = Percentile(q=90)
        from_float = Percentile(q=90.0)
        assert from_int == from_float
        assert len({from_int, from_float}) == 1

    def test_a_different_percentile_is_not_equal(self):
        """`q` is what distinguishes two instances."""
        assert Percentile(q=90) != Percentile(q=95)


class TestResolveReducer:
    """Name, parameterised spec, or instance — one entry point."""

    @pytest.mark.parametrize(("name", "cls"), CONVENIENCE.items())
    def test_resolves_a_bare_name(self, name, cls):
        """A registry name builds an instance of its class."""
        assert isinstance(resolve_reducer(name), cls)

    def test_resolves_a_parameterised_percentile(self):
        """`percentile:90` builds `Percentile(q=90)`."""
        reducer = resolve_reducer("percentile:90")
        assert isinstance(reducer, Percentile)
        assert reducer.q == 90.0

    def test_resolves_a_fractional_percentile(self):
        """A non-integer percentile is accepted."""
        assert resolve_reducer("percentile:97.5").q == 97.5

    def test_passes_an_instance_through(self):
        """An already-built reducer is returned unchanged, not rebuilt."""
        reducer = Percentile(q=95)
        assert resolve_reducer(reducer) is reducer

    def test_percentile_without_an_argument_explains_the_spelling(self):
        """A bare `percentile` cannot be built, and the error says how to."""
        with pytest.raises(ValueError, match="percentile:<q>"):
            resolve_reducer("percentile")

    def test_percentile_with_a_non_numeric_argument_is_rejected(self):
        """The argument must parse as a number."""
        with pytest.raises(ValueError, match="must be a number"):
            resolve_reducer("percentile:high")

    def test_an_out_of_range_spec_is_rejected_at_resolution(self):
        """Validation happens when the spec resolves, before any export runs."""
        with pytest.raises(ValidationError):
            resolve_reducer("percentile:120")

    def test_an_unknown_name_lists_the_alternatives(self):
        """The error names what is available, including the percentile spelling."""
        with pytest.raises(ValueError, match="unsupported reducer 'average'"):
            resolve_reducer("average")

    def test_an_argument_on_a_convenience_reducer_is_rejected(self):
        """`mean:5` is a mistake, not a silently ignored argument."""
        with pytest.raises(ValueError, match="takes no argument"):
            resolve_reducer("mean:5")

    def test_a_non_string_non_reducer_is_a_type_error(self):
        """Passing a stray object fails loudly rather than being coerced."""
        with pytest.raises(TypeError, match="name or a Reducer"):
            resolve_reducer(90)

    def test_a_trailing_separator_explains_the_spelling(self):
        """`percentile:` carries no argument, so it fails like the bare name."""
        with pytest.raises(ValueError, match="percentile:<q>"):
            resolve_reducer("percentile:")

    def test_a_second_separator_is_reported_as_a_bad_number(self):
        """The whole tail is the argument, so it has to parse as one number."""
        with pytest.raises(ValueError, match="'90:5'"):
            resolve_reducer("percentile:90:5")

    def test_each_resolution_builds_a_fresh_instance(self):
        """The registry holds classes, so no state can leak between requests."""
        assert resolve_reducer("mean") is not resolve_reducer("mean")


class TestPercentilePrintsAsItself:
    """A `BaseModel` prints as its fields, which said nothing about which reducer."""

    def test_str_matches_repr(self):
        """`print(reducer)` showed `q=90.0` with no class name at all."""
        reducer = Percentile(q=90)
        assert str(reducer) == repr(reducer) == "Percentile(q=90.0)"

    def test_the_plain_reducers_are_unaffected(self):
        """They are not pydantic models, so they already read correctly."""
        assert str(Median()) == "Median()"


class TestBackCompatShim:
    """`_helpers.reduce_collection` keeps working for the seven original names."""

    @pytest.mark.parametrize("name", CONVENIENCE)
    def test_shim_dispatches_like_before(self, name):
        """The old helper still calls the collection's convenience method."""
        from earthlens.gee._helpers import reduce_collection

        collection = _FakeCollection()
        assert reduce_collection(collection, name) == f"image-{name}"
        assert collection.called == [name]

    def test_shim_rejects_an_unknown_name(self):
        """The old error contract is preserved."""
        from earthlens.gee._helpers import reduce_collection

        collection = _FakeCollection()
        with pytest.raises(ValueError, match="unsupported reducer"):
            reduce_collection(collection, "average")

    def test_shim_now_also_accepts_a_percentile_spec(self, fake_ee_reducer):
        """Delegating to the registry widens the shim at no cost."""
        from earthlens.gee._helpers import reduce_collection

        collection = _FakeCollection()
        out = reduce_collection(collection, "percentile:90")
        assert out.renamed_to == ["B4", "B3"]

    def test_the_derived_name_set_matches_the_registry(self):
        """`_COLLECTION_REDUCERS` is a view on the registry, not a second copy.

        The *collection-capable* view specifically: a region reducer has no
        band-name-preserving collection method, so including it here would
        offer the EEDAI reader a composite it cannot make.
        """
        from earthlens.gee import _helpers
        from earthlens.gee.reducers import collection_reducers

        assert set(_helpers._COLLECTION_REDUCERS) == set(collection_reducers())

    def test_the_sampling_whitelist_is_a_view_on_the_registry(self):
        """Sampling reads the same registry, filtered by capability.

        Not the whole registry: `mosaic` is a collection method and
        `ee.Reducer.mosaic` does not exist, so a sampler refuses
        `reducer="mosaic"` rather than reaching an `AttributeError`.
        """
        from earthlens.gee.reducers import sampling_reducers

        whitelist = sampling_reducers()
        assert {"first", "stdDev", "variance", "count"} <= set(whitelist)
        assert "mosaic" not in whitelist
