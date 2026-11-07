"""The verdict an engine returns when asked whether it can serve a request.

A plan answers two questions at once — *will this engine take the read*, and *how*
— and carries the second half so the exporter never re-derives it. A second
derivation is free to disagree with the one the routing decision was made on, and
that disagreement is invisible until the pixels are wrong.

This was an `EedaiPlan` `NamedTuple` built positionally at eighteen call sites. It
is now a frozen model, which buys three things the tuple could not: the fields are
validated, a mistyped keyword is rejected instead of silently becoming a fifth
tuple element, and an `engine` field lets a plan say which engine produced it —
needed as soon as there is more than one.

Two named constructors cover every shape the backend actually builds:
`EnginePlan.declined` for a refusal with a reason, and
`EnginePlan.serves` for an acceptance with its unit count.

```python
>>> print(EnginePlan.declined("no native resolution", engine="eedai"))
EnginePlan(eedai, no native resolution)
>>> plan = EnginePlan.serves(tiles=4, tile_size=2048, engine="eedai")
>>> plan.can_serve, plan.tiles, plan.tile_size
(True, 4, 2048)

```
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import ConfigDict, Field, model_validator

from earthlens.base.leaves import SummarisedLeaf

__all__ = ["EnginePlan", "EedaiPlan", "RequestPreview"]


class EnginePlan(SummarisedLeaf):
    """How an engine should serve one request, or why it should not.

    Attributes:
        engine: Which engine produced this verdict (`"ee"`, `"eedai"`, …). A
            plan is only meaningful next to the engine that made it, and with
            more than one engine the field stops being implied.
        can_serve: Whether the engine takes this read at all.
        tile_size: Output pixels per tile side when the read is streamed, or
            `None` for a single pass (and whenever `can_serve` is `False`).
        tiles: How many units the read is made of, which depends on its kind.
            For a streamed single-image read it is the number of tiles the window
            is cut into (`1` for a single pass); for a collection composite it is
            the number of scenes the reader will fetch and reduce. Carried here so
            the exporter never re-derives it. It is only a tile count when
            `tile_size` is set, which a collection plan never does.
        reason: Why the engine declined; empty when it did not.

    Examples:
        - A refusal carries the reason and nothing else:
            ```python
            >>> plan = EnginePlan.declined("1,200 scenes, over the cap")
            >>> plan.can_serve
            False
            >>> plan.tiles
            0

            ```
        - It is frozen, so a routing verdict cannot be edited after the fact:
            ```python
            >>> plan = EnginePlan.serves(tiles=1)
            >>> plan.tiles = 9
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for EnginePlan
            ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    _summary_fields: ClassVar[tuple[str, ...]] = (
        "engine",
        "can_serve",
        "tiles",
        "reason",
    )

    engine: str = "eedai"
    can_serve: bool
    # Built from an Earth Engine compute plan, not a hand-typed constructor, so
    # these numeric fields are left coercing a bool rather than guarded with
    # NoBool: a bool here is an EE-response surprise to catch upstream (#1355).
    tile_size: int | None = None
    tiles: int = Field(default=1, ge=0)
    reason: str = ""

    @model_validator(mode="after")
    def _reject_a_self_contradictory_plan(self) -> EnginePlan:
        """Refuse the states a plan has no meaning in.

        The named constructors cannot produce these, but the model is public and
        `ExportConfig` already validates across its fields, so the invariant
        belongs on the type rather than in the habits of its callers.

        Returns:
            The validated plan.

        Raises:
            ValueError: If a refusal carries no reason (the remedy text would
                read `cannot serve X: .`), if a servable plan claims no units of
                work, or if `tile_size` is set but not positive.
        """
        if not self.can_serve and not (self.reason or "").strip():
            raise ValueError("a declined plan must carry a reason")
        if self.can_serve and self.tiles < 1:
            raise ValueError(
                f"a servable plan must have at least one unit of work, got "
                f"tiles={self.tiles}"
            )
        if self.tile_size is not None and self.tile_size < 1:
            raise ValueError(
                f"tile_size must be a positive number of pixels, got {self.tile_size}"
            )
        return self

    def summary_parts(self) -> list[str]:
        """Drop the unit count from a refusal's summary.

        A declined plan always carries `tiles=0`, and a zero that renders as
        `0` beside the reason is noise rather than information.

        Returns:
            One fragment per populated field, minus `tiles` when declining.
        """
        parts = super().summary_parts()
        if not self.can_serve:
            return [part for part in parts if part != "0"]
        return parts

    @classmethod
    def declined(cls, reason: str, *, engine: str = "eedai") -> EnginePlan:
        """Build a refusal.

        Args:
            reason: Why the engine will not serve this request. It reaches the
                user verbatim in the forced-engine error and the fallback log
                line, so it should name the dial that would change the answer.
                Prefix it with `earthlens.gee.engines.NO_SCENES` when the
                refusal is about the data rather than the request, which is what
                lets a forced engine skip the bucket instead of failing.
            engine: Which engine refused. Defaults to `"eedai"`, the engine that
                was the only one declining when this type was introduced.

        Returns:
            A plan with `can_serve=False`, no tiling, and `tiles=0`.
        """
        return cls(
            engine=engine, can_serve=False, tile_size=None, tiles=0, reason=reason
        )

    @classmethod
    def serves(
        cls, *, tiles: int = 1, tile_size: int | None = None, engine: str = "eedai"
    ) -> EnginePlan:
        """Build an acceptance.

        Args:
            tiles: How many units the read is made of — tiles for a streamed
                single image, scenes for a collection composite. Defaults to
                `1`, the single-pass case.
            tile_size: Output pixels per tile side for a streamed read, or `None`
                for a single pass.
            engine: Which engine accepted. Defaults to `"eedai"`.

        Returns:
            A plan with `can_serve=True` and an empty reason.
        """
        return cls(
            engine=engine, can_serve=True, tile_size=tile_size, tiles=tiles, reason=""
        )


#: The pre-Epic-1 name. Kept so `backend.py`'s `__all__` and any caller importing
#: it keep working; new code should use `EnginePlan`, which says which engine the
#: verdict came from.
EedaiPlan = EnginePlan


class RequestPreview(SummarisedLeaf):
    """A cheap, frozen pre-flight summary of what a GEE read would cost.

    Returned by `GEE.preview()`: which engine would serve the read, how many
    scenes and output pixels it involves (the EEDAI reader's estimate, when the
    reader is the engine), how many tiles it is streamed in, and whether it is
    served in a single pass or falls back to Earth Engine with a reason. The
    budget thresholds behind `fits` stay private — this reports *whether* a
    request fits and *why not*, never the thresholds themselves, since they are
    current behaviour, not a contract.

    Attributes:
        engine: The engine that would serve the read (`"ee"`, `"eedai"`, ...).
        fits: Whether the preferred engine serves it without falling back —
            `True` for an Earth-Engine-served read (composited server-side).
        tiles: How many tiles the read is streamed in (`1` for a single pass);
            meaningful for the EEDAI reader, `1` otherwise.
        scene_count: Scenes in the window (the EEDAI estimate), or `None` when
            the reader is not the engine and no per-scene estimate is made.
        max_width: Output width in pixels (EEDAI estimate), or `None`.
        max_height: Output height in pixels (EEDAI estimate), or `None`.
        total_size_bytes: Estimated materialised size (EEDAI estimate), or `None`.
        reason: Why the preferred engine declined / fell back, or `None`.

    Examples:
        - A server-side Earth Engine read fits in one pass and has no estimate:
            ```python
            >>> from earthlens.gee.plans import RequestPreview
            >>> p = RequestPreview(engine="ee", fits=True)
            >>> (p.engine, p.fits, p.tiles, p.scene_count)
            ('ee', True, 1, None)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    engine: str
    fits: bool
    tiles: int = 1
    scene_count: int | None = None
    max_width: int | None = None
    max_height: int | None = None
    total_size_bytes: int | None = None
    reason: str | None = None
