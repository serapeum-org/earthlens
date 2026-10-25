"""No numeric field on the server-side value objects may accept a `bool`.

`bool` subclasses `int`, so an unguarded `int` / `float` field takes `True`
and stores `1`. This walks the models reflectively rather than listing
fields, so a field added later is covered without anyone remembering to add
a case.
"""

from __future__ import annotations

import typing

import pytest

pytest.importorskip("ee")

from pydantic import BaseModel, TypeAdapter, ValidationError  # noqa: E402

from earthlens.gee import (  # noqa: E402
    assets,
    cloud_masks,
    filters,
    reducers,
    video_sink,
)

#: The modules this branch adds or extends. The rest of the package has the
#: same latent shape on many more fields; widening this list is a separate,
#: much larger change than the value objects added here.
_MODULES = (assets, cloud_masks, filters, reducers, video_sink)


def _mentions_bool_or_any(annotation: object) -> bool:
    """Say whether `bool` or `Any` appears anywhere in the annotation tree."""
    return annotation in (bool, typing.Any) or any(
        _mentions_bool_or_any(argument) for argument in typing.get_args(annotation)
    )


def _numeric_fields() -> list[tuple[str, type[BaseModel], str, TypeAdapter]]:
    """Every public model field that accepts a plain `1`, with its adapter."""
    found: list[tuple[str, type[BaseModel], str, TypeAdapter]] = []
    for module in _MODULES:
        for attribute in vars(module).values():
            if (
                not isinstance(attribute, type)
                or not issubclass(attribute, BaseModel)
                or attribute.__module__ != module.__name__
                or attribute.__name__.startswith("_")
            ):
                continue
            for name, info in attribute.model_fields.items():
                annotation = info.rebuild_annotation()
                if _mentions_bool_or_any(annotation):
                    continue
                try:
                    adapter: TypeAdapter = TypeAdapter(annotation)
                    adapter.validate_python(1)
                except Exception:  # noqa: BLE001 - not a numeric field, skip
                    continue
                found.append((module.__name__, attribute, name, adapter))
    return found


_FIELDS = _numeric_fields()
_IDS = [f"{model.__name__}.{field}" for _, model, field, _ in _FIELDS]


def test_the_sweep_found_the_fields_it_is_meant_to_guard():
    """A broken walk would pass every case below by finding nothing."""
    names = set(_IDS)
    assert {
        "VideoSink.fps",
        "VideoSink.dimensions",
        "VideoSink.max_frames",
        "ByQuality.value",
        "Sentinel2Cdi.threshold",
        "BandFactor.scale",
        "Sentinel2Composite.threshold",
    } <= names
    assert len(_FIELDS) >= 20


@pytest.mark.parametrize(("module", "model", "field", "adapter"), _FIELDS, ids=_IDS)
@pytest.mark.parametrize("given", [True, False], ids=["True", "False"])
def test_a_numeric_field_refuses_a_bool(module, model, field, adapter, given):
    """`True` is not the number 1, so it is refused rather than coerced."""
    with pytest.raises(ValidationError, match="got the boolean"):
        adapter.validate_python(given)


@pytest.mark.parametrize(("module", "model", "field", "adapter"), _FIELDS, ids=_IDS)
def test_a_numeric_field_still_takes_a_number(module, model, field, adapter):
    """The guard rejects `bool` only; a plain `1` still validates."""
    assert adapter.validate_python(1) is not None
