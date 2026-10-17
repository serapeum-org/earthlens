"""Tests for the `RasterOperation` contract and `OperationChain` composition."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis import OperationChain, RasterOperation

#: A small georeferenced raster is enough for the contract tests: what matters
#: is that an operation returns a Dataset and leaves the source alone, not the
#: pixel values.
GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _raster(values: np.ndarray | None = None) -> Dataset:
    """Build a one-band float raster, by default a 4x4 ramp."""
    if values is None:
        values = np.arange(16, dtype="float32").reshape(4, 4)
    return Dataset.from_array(arr=values, geo_ref=GEO, no_data_value=np.nan)


class _Identity(RasterOperation):
    """Returns its input unchanged, so the plumbing is the only thing tested."""

    def apply(self, ds: Dataset) -> Dataset:
        """Return `ds` as given."""
        return ds


class _AddOne(RasterOperation):
    """Adds a constant to the first band, to prove a chain folds in order."""

    amount: float = 1.0

    def apply(self, ds: Dataset) -> Dataset:
        """Return a new raster with `amount` added to every cell."""
        return _raster(np.asarray(ds.read_array(), dtype="float32") + self.amount)


class _Record(RasterOperation):
    """Appends its tag to a list, so run order is observable without rasters."""

    tag: str

    def apply(self, ds):  # noqa: ANN001, ANN201 - takes a list, not a Dataset
        """Return `ds` with this operation's tag appended."""
        return [*ds, self.tag]


class TestTheContract:
    """What every operation has to provide."""

    def test_apply_returns_a_dataset(self):
        """An operation's output is a raster, not an array."""
        assert isinstance(_Identity().apply(_raster()), Dataset)

    def test_calling_the_instance_is_the_same_as_apply(self):
        """`__call__` is an alias, so an op reads as a function at the call site."""
        source = _raster()
        assert _Identity()(source) is _Identity().apply(source)

    def test_name_defaults_to_the_class_name(self):
        """The default label is the class, which is what chain names join."""
        assert _AddOne().name == "_AddOne"

    def test_the_config_is_frozen(self):
        """One instance can be reused across rasters without defensive copying."""
        operation = _AddOne(amount=2.0)
        with pytest.raises(ValidationError, match="frozen"):
            operation.amount = 3.0

    def test_an_unknown_field_is_rejected_at_construction(self):
        """A mistyped field fails immediately rather than being ignored."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            _AddOne(amuont=2.0)

    def test_the_abc_cannot_be_instantiated(self):
        """`apply` is abstract, so the base class is not a usable operation."""
        with pytest.raises(TypeError, match="abstract"):
            RasterOperation()

    def test_applying_does_not_mutate_the_source(self):
        """The caller's raster stays usable after an operation runs."""
        source = _raster()
        before = np.asarray(source.read_array(), dtype="float32").copy()
        _AddOne(amount=5.0).apply(source)
        after = np.asarray(source.read_array(), dtype="float32")
        assert np.array_equal(before, after)


class TestComposition:
    """How `>>` and `OperationChain` build a run out of operations."""

    def test_composing_two_operations_yields_a_chain(self):
        """`a >> b` is itself an operation, so it goes anywhere one does."""
        chain = _Identity() >> _Identity()
        assert isinstance(chain, OperationChain)
        assert isinstance(chain, RasterOperation)

    def test_the_chain_runs_in_left_to_right_order(self):
        """The leftmost operation sees the input first."""
        chain = _Record(tag="first") >> _Record(tag="second")
        assert chain.apply([]) == ["first", "second"]

    def test_composition_flattens_rather_than_nesting(self):
        """Grouping does not change the run, so `name` stays readable."""
        left = (_Identity() >> _Identity()) >> _Identity()
        right = _Identity() >> (_Identity() >> _Identity())
        assert len(left) == len(right) == 3

    def test_of_accepts_operations_positionally(self):
        """A list built elsewhere becomes a chain without `>>`."""
        chain = OperationChain.of(_Record(tag="a"), _Record(tag="b"))
        assert chain.apply([]) == ["a", "b"]

    def test_of_flattens_a_chain_it_is_given(self):
        """Passing a chain extends it instead of embedding it."""
        inner = _Identity() >> _Identity()
        assert len(OperationChain.of(inner, _Identity())) == 3

    def test_an_empty_chain_is_the_identity(self):
        """`OperationChain.of()` is usable as a no-op rather than an error."""
        source = _raster()
        assert OperationChain.of().apply(source) is source

    def test_the_name_joins_the_members(self):
        """A chain reports what it will run, in order."""
        assert (_AddOne() >> _Identity()).name == "_AddOne >> _Identity"

    def test_the_chain_iterates_its_operations(self):
        """Inspecting a chain yields the operations, not pydantic fields."""
        chain = _Record(tag="x") >> _Record(tag="y")
        assert [operation.tag for operation in chain.operations] == ["x", "y"]

    def test_composing_with_a_non_operation_is_refused(self):
        """`>>` names both sides, rather than failing later inside `apply`."""
        with pytest.raises(TypeError, match="needs a RasterOperation"):
            _Identity() >> "not an operation"

    def test_the_chain_is_frozen_like_any_operation(self):
        """A chain inherits the value-object guarantees it is built from."""
        chain = _Identity() >> _Identity()
        with pytest.raises(ValidationError, match="frozen"):
            chain.operations = ()

    def test_a_chain_of_real_operations_accumulates(self):
        """Two adds compose arithmetically, which is the fold doing its job."""
        chain = _AddOne(amount=2.0) >> _AddOne(amount=3.0)
        result = chain.apply(_raster(np.zeros((4, 4), dtype="float32")))
        assert float(np.asarray(result.read_array(), dtype="float32").max()) == 5.0


class TestTheModelProtocol:
    """A chain is a pydantic model, and must keep behaving like one."""

    def test_dict_of_a_chain_is_its_fields_not_its_operations(self):
        """An `__iter__` override made `dict(chain)` look like data.

        Yielding the operations meant `dict()` took each one and unpacked it
        through *its* inherited pydantic `__iter__`, producing
        `{('nir', 0): ('red', 1), ...}` - a mapping of tuples to tuples that
        raised nothing and meant nothing.
        """
        chain = _Record(tag="x") >> _Record(tag="y")
        as_dict = dict(chain)
        assert set(as_dict) == {"operations"}
        assert [operation.tag for operation in as_dict["operations"]] == ["x", "y"]

    def test_the_operations_are_reached_through_the_field(self):
        """`chain.operations` is the documented way to iterate a chain."""
        chain = _Record(tag="x") >> _Record(tag="y")
        assert len(chain) == 2
        assert tuple(operation.tag for operation in chain.operations) == ("x", "y")
