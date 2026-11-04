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
_stay_unguarded` pins that set so the triage cannot silently rot: a new
unguarded field anywhere else fails it.
"""

from __future__ import annotations

import importlib
import pkgutil
import typing

import pytest

pytest.importorskip("ee")

from pydantic import BaseModel, TypeAdapter, ValidationError  # noqa: E402

import earthlens.gee as _gee_pkg  # noqa: E402

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


def _numeric_fields() -> list[tuple[type[BaseModel], str, TypeAdapter, bool]]:
    """Every field where a `bool` is a live question, flagged guarded or not.

    A field qualifies only if feeding it a `bool` either coerces (the hazard,
    `guarded=False`) or is refused by `NoBool` (`guarded=True`). A field whose
    own type refuses a `bool` for its own reasons - a `datetime` accepts the
    integer `1` as a timestamp but rejects `True` natively - is out of scope and
    skipped, so the suite does not mistake that native refusal for a guard.
    """
    found: list[tuple[type[BaseModel], str, TypeAdapter, bool]] = []
    for model in _public_models():
        for name, info in model.model_fields.items():
            annotation = info.rebuild_annotation()
            if _mentions_bool_or_any(annotation):
                continue
            try:
                adapter: TypeAdapter = TypeAdapter(annotation)
                adapter.validate_python(1)
            except Exception:  # noqa: BLE001 - does not take a number, skip
                continue
            try:
                adapter.validate_python(True)
            except ValidationError as error:
                if "got the boolean" in str(error):
                    found.append((model, name, adapter, True))  # NoBool-guarded
                # else: the field's own type refused it - not in scope.
            else:
                found.append((model, name, adapter, False))  # coerced - a hazard
    return found


_FIELDS = _numeric_fields()
_GUARDED = [(m, f, a) for m, f, a, g in _FIELDS if g]
_GUARDED_IDS = [f"{m.__name__}.{f}" for m, f, _ in _GUARDED]


def test_the_sweep_found_the_fields_it_is_meant_to_guard():
    """A broken walk would pass every case below by finding nothing."""
    names = set(_GUARDED_IDS)
    assert {
        "VideoSink.fps",
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

    Fails if a new user-facing field starts coercing a `bool` (drift up), or if
    a machine-fed field is guarded without updating `_MACHINE_FED` (drift down).
    """
    unguarded = {f"{m.__name__}.{f}" for m, f, _, g in _FIELDS if not g}
    assert unguarded == set(_MACHINE_FED)
