"""No user-facing numeric field on the GEE value objects may accept a `bool`.

`bool` subclasses `int`, so an unguarded `int` / `float` (and any field pydantic
feeds an `int` into, such as a `timedelta` of seconds) takes `True` and stores
`1`. `NoBool` refuses the boolean up front. This walks every model in the
`earthlens.gee` package reflectively rather than listing fields, so a field
added later is covered without anyone remembering to add a case.

A small set of models is fed from machine input - the YAML catalog, an Earth
Engine response - rather than a hand-typed constructor. There a `bool` is a
schema surprise to catch upstream, not a user typo to refuse at the boundary,
so those fields are deliberately left coercing. `test_only_machine_fed_fields
_stay_unguarded` pins that set so the triage cannot silently rot: any new
unguarded *scalar* numeric field - one that accepts the integer `0` or `1` -
fails it. Numeric *elements* of tuples and numeric dict *keys* accept no scalar
`0`/`1`, so they fall outside this sweep by construction; that wider surface is
guarded field-by-field (e.g. `StyleHints` already does) rather than pinned here.
"""

from __future__ import annotations

import importlib
import pkgutil
import typing

import pytest

pytest.importorskip("ee")

from pydantic import BaseModel, TypeAdapter, ValidationError  # noqa: E402

import earthlens.gee as _gee_pkg  # noqa: E402
from earthlens.gee._helpers import NoBool  # noqa: E402

#: Fields built from machine input, deliberately left coercing a `bool`. Keyed
#: `Model.field`. `Band` / `Cadence` / `Dataset` are parsed from
#: `gee_data_catalog.yaml`; `TaskInfo` is built from an Earth Engine task
#: response; `EnginePlan` from an Earth Engine compute plan. Guarding them would
#: turn accepted parse input into an exception - a separate decision than
#: guarding the constructor surface (#1355).
_MACHINE_FED = frozenset(
    {
        "Band.scale",
        "Band.offset",
        "Band.wavelength",
        "Band.min",
        "Band.max",
        "Cadence.interval",
        "Dataset.spatial_resolution",
        "TaskInfo.attempt",
        "TaskInfo.priority",
        "EnginePlan.tile_size",
        "EnginePlan.tiles",
    }
)


def _mentions_bool_or_any(annotation: object) -> bool:
    """Say whether `bool` or `Any` appears anywhere in the annotation tree."""
    return annotation in (bool, typing.Any) or any(
        _mentions_bool_or_any(argument) for argument in typing.get_args(annotation)
    )


def _public_models() -> list[type[BaseModel]]:
    """Every public pydantic model defined across the `earthlens.gee` package."""
    models: dict[str, type[BaseModel]] = {}
    for info in pkgutil.walk_packages(_gee_pkg.__path__, _gee_pkg.__name__ + "."):
        module = importlib.import_module(info.name)
        for attribute in vars(module).values():
            if (
                isinstance(attribute, type)
                and issubclass(attribute, BaseModel)
                and attribute.__module__ == module.__name__
                and not attribute.__name__.startswith("_")
            ):
                models[f"{attribute.__module__}.{attribute.__name__}"] = attribute
    return list(models.values())


#: The plain integers a `bool` collapses to: `True` is `1`, `False` is `0`. A
#: field that accepts neither cannot silently take a `bool`, so it is not a
#: numeric field this sweep needs to see - probing both (not just `1`) means a
#: field whose domain excludes `1` but admits `0` is still examined.
_INT_PROBES = (1, 0)


def _accepts(adapter: TypeAdapter, value: object) -> bool:
    """Say whether `adapter` validates `value` without raising."""
    try:
        adapter.validate_python(value)
    except Exception:  # noqa: BLE001 - any failure means "does not accept"
        return False
    return True


def _classify(adapter: TypeAdapter) -> bool | None:
    """Classify one field adapter against the `bool`-coercion hazard.

    Returns `True` when a `bool` is refused by `NoBool`, `False` when a `bool`
    coerces silently (the hazard), or `None` when the field accepts no plain
    integer - so no `bool` could slip into it - or refuses a `bool` for its own
    type's reasons (a `datetime` takes the integer `1` as a timestamp but
    rejects `True` natively, and must not be mistaken for a guard).
    """
    if not any(_accepts(adapter, probe) for probe in _INT_PROBES):
        return None
    for value in (True, False):
        try:
            adapter.validate_python(value)
        except ValidationError as error:
            if "got the boolean" in str(error):
                return True  # refused by NoBool
            # refused by the field's own type/range for this value; try the other
        else:
            return False  # coerced silently - a hazard
    return None


def _numeric_fields() -> list[tuple[type[BaseModel], str, TypeAdapter, bool]]:
    """Every field a `bool` could reach, flagged guarded (`True`) or not.

    A field is included when it accepts at least one of `0` / `1` as a plain
    integer - the two values a `bool` collapses to - and either coerces a `bool`
    (`guarded=False`) or refuses it via `NoBool` (`guarded=True`). Fields that
    accept no integer, or that refuse a `bool` for their own type's reasons, are
    skipped (see `_classify`).
    """
    found: list[tuple[type[BaseModel], str, TypeAdapter, bool]] = []
    for model in _public_models():
        for name, info in model.model_fields.items():
            annotation = info.rebuild_annotation()
            if _mentions_bool_or_any(annotation):
                continue
            try:
                adapter: TypeAdapter = TypeAdapter(annotation)
            except Exception:  # noqa: BLE001 - unbuildable schema, not numeric
                continue
            guarded = _classify(adapter)
            if guarded is not None:
                found.append((model, name, adapter, guarded))
    return found


_FIELDS = _numeric_fields()
_GUARDED = [(m, f, a) for m, f, a, g in _FIELDS if g]
_GUARDED_IDS = [f"{m.__name__}.{f}" for m, f, _ in _GUARDED]


def test_the_sweep_found_the_fields_it_is_meant_to_guard():
    """A broken walk would pass every case below by finding nothing."""
    names = set(_GUARDED_IDS)
    assert {
        "VideoSink.fps",
        "VideoSink.dimensions",
        "VideoSink.max_frames",
        "LandTrendr.maxSegments",
        "OutputGrid.rows",
        "Vectorize.scale_m",
        "ExportConfig.http_timeout",
        "ClosestImage.tolerance",
        "GapFill.max_gap",
        "StratifiedSampler.n_per_class",
    } <= names
    assert len(_GUARDED) >= 100


@pytest.mark.parametrize(("model", "field", "adapter"), _GUARDED, ids=_GUARDED_IDS)
@pytest.mark.parametrize("given", [True, False], ids=["True", "False"])
def test_a_numeric_field_refuses_a_bool(model, field, adapter, given):
    """`True` is not the number 1, so it is refused rather than coerced."""
    with pytest.raises(ValidationError, match="got the boolean"):
        adapter.validate_python(given)


@pytest.mark.parametrize(("model", "field", "adapter"), _GUARDED, ids=_GUARDED_IDS)
def test_a_numeric_field_still_takes_a_number(model, field, adapter):
    """The guard rejects `bool` only; a plain `1` still validates."""
    assert adapter.validate_python(1) is not None


def test_only_machine_fed_fields_stay_unguarded():
    """The unguarded population is exactly the recorded machine-fed set.

    Fails if a new *scalar* numeric field (one accepting the integer `0` or `1`)
    starts coercing a `bool` (drift up), or if a machine-fed field is guarded
    without updating `_MACHINE_FED` (drift down). Numeric elements of tuples and
    numeric dict keys accept no scalar `0`/`1`, so they fall outside this sweep
    by construction - a wider surface guarded per field, not pinned here.
    """
    unguarded = {f"{m.__name__}.{f}" for m, f, _, g in _FIELDS if not g}
    assert unguarded == set(_MACHINE_FED)


def test_classify_sees_a_hazard_whose_domain_excludes_one_but_admits_zero():
    """A field rejecting `1` but accepting `0` still coerces `False`; the sweep catches it.

    This is the blind spot of probing `1` alone: `Field(le=0)` refuses `1`, so a
    `1`-only probe would skip the field entirely, yet an unguarded such field
    still silently turns `False` into `0`. Probing `0` as well keeps it visible.
    """
    from pydantic import Field

    hazard = TypeAdapter(typing.Annotated[int, Field(le=0)])
    assert _classify(hazard) is False, "a False->0 coercion must register as a hazard"
    guarded = TypeAdapter(typing.Annotated[int, Field(le=0), NoBool])
    assert _classify(guarded) is True, "NoBool must register as guarded even when le=0"


def test_stratified_sampler_refuses_a_bool_count_inside_the_mapping():
    """A per-class count of `True` is the #1355 trap one level down; refuse it too."""
    from earthlens.gee.sampling.random import StratifiedSampler

    with pytest.raises(ValidationError, match="got the boolean"):
        StratifiedSampler(scale_m=30, class_band="landcover", n_per_class={1: True})
    with pytest.raises(ValidationError, match="got the boolean"):
        StratifiedSampler(scale_m=30, class_band="landcover", n_per_class={True: 100})
