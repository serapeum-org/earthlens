"""Which layer materialises a request's pixels, as strategy objects.

`_use_eedai` hand-rolled the `ee` / `eedai` / `auto` decision and `_api` then
branched again on `export_via`, with a `try/except` around the reader for the
refusal that only arrives after routing has committed. Routing, eligibility,
sizing and fallback were fused into two methods, so adding an engine meant a new
string in `_ENGINES` plus new branches in both.

Each engine is an object here, and the registry resolves the `engine=` string to
one. Adding an engine is a subclass plus a registry entry — which Task 1.13 then
does, and that is the only real proof this seam is the right shape.

**What moved and what did not.** The *decision* moved: eligibility, the forced
raise, the no-scenes skip, the sizing-failure fallback and the post-routing
refusal all live in the engines now. The *mechanics* — building the collection,
writing through the reader or `getDownloadURL`, queueing a batch task — stay on
the backend and are called back into, so this commit is behaviour-identical and
the characterisation suite stays green. Moving the mechanics is what Tasks 1.4
(`Sink`) and 1.7 (`WorkUnit`) do.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from loguru import logger

from earthlens.gee._eedai import import_earthengine_reader
from earthlens.gee.plans import EnginePlan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from earthlens.gee.backend import GEE
    from earthlens.gee.catalog import Dataset

__all__ = [
    "Engine",
    "EeEngine",
    "EedaiEngine",
    "AutoEngine",
    "ENGINES",
    "resolve_engine",
]

#: Marker prefix on a decline caused by the *data* rather than the request, so a
#: forced engine skips the bucket instead of failing the whole download.
NO_SCENES: str = "no scenes"


class Engine(ABC):
    """Decide whether this engine serves a request, and if so with what plan.

    Attributes:
        name: The registry key, and the value a user's `engine=` string resolves
            from.
    """

    name: ClassVar[str]

    @abstractmethod
    def plan(
        self,
        backend: GEE,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None = None,
        bucket_end: dt.datetime | None = None,
    ) -> tuple[bool, EnginePlan | None]:
        """Decide whether to serve this request through this engine.

        Args:
            backend: The `GEE` instance, for its config and the sizing helpers.
            var_info: The catalog entry being read.
            band_count: How many bands the read requests.
            bucket_start: The bucket's inclusive start, for a collection read.
            bucket_end: The bucket's exclusive end, for a collection read.

        Returns:
            `(use_this_engine, plan)`. A `False` carries `None`; a `True` always
            carries the plan the exporter reads its tiling off.
        """

    def __repr__(self) -> str:
        """Return the engine's class name, for log lines and plans."""
        return f"{type(self).__name__}()"


class EeEngine(Engine):
    """Earth Engine itself — `getDownloadURL` and the batch export sinks.

    Serves anything, because it is the engine that can run a computation graph.
    Its `plan` therefore never declines: there is nothing to size against, and a
    request this engine cannot handle fails at the export call with Earth
    Engine's own error rather than being pre-judged here.
    """

    name: ClassVar[str] = "ee"

    def plan(
        self,
        backend: GEE,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None = None,
        bucket_end: dt.datetime | None = None,
    ) -> tuple[bool, EnginePlan | None]:
        """Decline the *reader*, which is how the caller routes to Earth Engine.

        The return shape is the pre-Epic-1 one: `(False, None)` means "do not use
        the reader", which the backend reads as "use the Earth Engine path".

        Args:
            backend: Unused; present for the shared signature.
            var_info: Unused; present for the shared signature.
            band_count: Unused; present for the shared signature.
            bucket_start: Unused; present for the shared signature.
            bucket_end: Unused; present for the shared signature.

        Returns:
            `(False, None)`, always.
        """
        return False, None


class EedaiEngine(Engine):
    """The pyramids-eo EEDAI reader — stored pixels, no server-side compute.

    Forced with `engine="eedai"`, so a request it cannot serve raises rather than
    silently falling back: the user asked for this engine specifically. The one
    exception is a bucket that simply holds no scenes, which is a property of the
    data and not of the request, so it falls through instead of aborting a run
    whose earlier buckets wrote fine.
    """

    name: ClassVar[str] = "eedai"

    def plan(
        self,
        backend: GEE,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None = None,
        bucket_end: dt.datetime | None = None,
    ) -> tuple[bool, EnginePlan | None]:
        """Serve the request, or raise naming the dial that would change it.

        Args:
            backend: The `GEE` instance, for eligibility and sizing.
            var_info: The catalog entry being read.
            band_count: How many bands the read requests.
            bucket_start: The bucket's inclusive start, for a collection read.
            bucket_end: The bucket's exclusive end, for a collection read.

        Returns:
            `(True, plan)` when the reader serves it, or `(False, None)` when the
            bucket holds no scenes.

        Raises:
            ValueError: If the request is ineligible (it needs server-side
                compute, or targets a CRS the reader cannot size), or eligible
                but declined by the sizing verdict. The message names the reason
                and a matching remedy.
        """
        from earthlens.gee.backend import _eedai_remedy

        if not backend._eedai_eligible(var_info):
            raise ValueError(
                f"engine='eedai' cannot serve {var_info.id}: the EEDAI "
                "reader materialises pixels from an asset id, so it cannot "
                "run server-side compute (a reduced collection, cloud_mask "
                "or filters), and it reads only EPSG:4326 or a metre-based "
                f"projected CRS (got crs={backend.crs!r}). Use engine='auto' "
                "or engine='ee'."
            )
        plan = backend._eedai_verdict(var_info, band_count, bucket_start, bucket_end)
        if plan.can_serve:
            return True, plan
        if plan.reason.startswith(NO_SCENES):
            # Nothing to read here, but the request itself is fine, so let the
            # Earth Engine path write this bucket rather than aborting the run.
            logger.info(
                f"{var_info.id}: {plan.reason}; this bucket falls through "
                "to Earth Engine."
            )
            return False, None
        raise ValueError(
            f"engine='eedai' cannot serve {var_info.id}: {plan.reason}. "
            f"{_eedai_remedy(plan.reason)}"
        )

    def refusal_is_fatal(self) -> bool:
        """Whether a refusal arriving after routing should propagate.

        Returns:
            `True` — a forced engine's refusal is the answer, not a reason to
            try something else.
        """
        return True


class AutoEngine(Engine):
    """Prefer the reader when it can serve the request, else Earth Engine.

    This is where every fallback the old code spread across `_use_eedai` and
    `_api` now lives. The contract it keeps: the user asked for a download, not
    for a particular engine, so anything the reader declines — ineligibility, a
    sizing refusal, an empty bucket, a refusal that only surfaces mid-export —
    routes to Earth Engine rather than failing.
    """

    name: ClassVar[str] = "auto"

    def __init__(self) -> None:
        self._eedai = EedaiEngine()

    def plan(
        self,
        backend: GEE,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None = None,
        bucket_end: dt.datetime | None = None,
    ) -> tuple[bool, EnginePlan | None]:
        """Try the reader, falling back to Earth Engine on any decline.

        Args:
            backend: The `GEE` instance, for eligibility and sizing.
            var_info: The catalog entry being read.
            band_count: How many bands the read requests.
            bucket_start: The bucket's inclusive start, for a collection read.
            bucket_end: The bucket's exclusive end, for a collection read.

        Returns:
            `(True, plan)` when the reader serves it, else `(False, None)`.
        """
        # Read through the backend module's binding, not a direct import: that
        # attribute is the seam the test suite patches to simulate the extra
        # being absent, and a direct import here would bypass it.
        from earthlens.gee import backend as backend_module

        if not (backend._eedai_eligible(var_info) and backend_module.eedai_available()):
            return False, None
        try:
            plan = backend._eedai_verdict(
                var_info, band_count, bucket_start, bucket_end
            )
        except ValueError as exc:
            # Sizing refused the request — a non-finite projected envelope, say.
            # Under `auto` that is a reason to let Earth Engine serve it, not to
            # abort the download.
            logger.warning(
                f"the EEDAI reader could not size {var_info.id} ({exc}); "
                "falling back to Earth Engine."
            )
            return False, None
        if not plan.can_serve:
            logger.info(
                f"Serving {var_info.id} through Earth Engine rather than the EEDAI "
                f"reader: {plan.reason}."
            )
            return False, None
        return True, plan

    def refusal_is_fatal(self) -> bool:
        """Whether a refusal arriving after routing should propagate.

        Returns:
            `False` — a post-routing refusal is a request Earth Engine can still
            serve, so falling back keeps the contract that `auto` routes rather
            than fails.
        """
        return False


#: Every engine addressable by `engine=`.
ENGINES: dict[str, type[Engine]] = {
    cls.name: cls for cls in (AutoEngine, EeEngine, EedaiEngine)
}


def resolve_engine(spec: str | Engine) -> Engine:
    """Resolve an engine name, or pass an instance through.

    Args:
        spec: A registry name (`"auto"`, `"ee"`, `"eedai"`) or an `Engine`.

    Returns:
        The resolved engine.

    Raises:
        ValueError: If the name is not registered.
        TypeError: If `spec` is neither a string nor an `Engine`.

    Examples:
        - Resolve each shipped name:
            ```python
            >>> resolve_engine("eedai").name
            'eedai'

            ```
        - An unknown name names the alternatives:
            ```python
            >>> resolve_engine("telepathy")
            Traceback (most recent call last):
                ...
            ValueError: engine must be one of [...], got 'telepathy'

            ```
    """
    if isinstance(spec, Engine):
        return spec
    if not isinstance(spec, str):
        raise TypeError(
            f"engine must be a name or an Engine instance, got {type(spec).__name__}"
        )
    if spec not in ENGINES:
        raise ValueError(f"engine must be one of {sorted(ENGINES)}, got {spec!r}")
    return ENGINES[spec]()


def reader_refusal_types() -> tuple[type[BaseException], ...]:
    """Return the exception types a post-routing reader refusal arrives as.

    Resolved through the guarded import so the optional dependency stays
    optional, and so the types come from `pyramids_eo.errors` rather than the
    reader module, which does not re-export `ReaderError`.

    Returns:
        The exception types to catch around a reader export.
    """
    from earthlens.gee.backend import _reader_errors

    return _reader_errors(import_earthengine_reader())
