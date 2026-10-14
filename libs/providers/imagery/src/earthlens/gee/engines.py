"""Which layer materialises a request's pixels, as strategy objects.

`_use_eedai` hand-rolled the `ee` / `eedai` / `auto` decision and `_api` then
branched again on `export_via`, with a `try/except` around the reader for the
refusal that only arrives after routing has committed. Routing, eligibility,
sizing and fallback were fused into two methods, so adding an engine meant a new
string in `_ENGINES` plus new branches in both.

Each engine is an object here, and the registry resolves the `engine=` string to
one. Adding an engine is a subclass plus a registry entry — which
:class:`~earthlens.gee.pixels.PixelsEngine` does, registering itself on import
with no edit to the routing or export methods.

**What moved and what did not.** The *decision* moved: eligibility, the forced
raise, the no-scenes skip, the sizing-failure fallback and the post-routing
refusal all live in the engines now. The *mechanics* — building the collection,
writing through the reader or `getDownloadURL`, queueing a batch task — stay on
the backend and are called back into, so routing an engine changes no observable
behaviour. :class:`~earthlens.gee.sinks.Sink` and
:class:`~earthlens.gee.work.WorkUnit` likewise carry the destination and the
payload as types without having taken the writing itself off the backend.
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
    "selectable_engines",
    "Engine",
    "EeEngine",
    "EedaiEngine",
    "AutoEngine",
    "ENGINES",
    "resolve_engine",
]

#: Marker prefix on a decline caused by the *data* rather than the request, so a
#: forced engine skips the bucket instead of failing the whole download. A plan
#: claims the skip by starting its `reason` with this text, which is matched by
#: prefix rather than equality so the reason can still name the dataset and
#: window it found empty.
NO_SCENES: str = "no scenes"


class Engine(ABC):
    """Decide whether this engine serves a request, and if so with what plan.

    Only :meth:`plan` is required. An engine whose plan can accept a read also
    declares `refusal_is_fatal()`, which the backend consults when the read it
    routed is refused after the fact — fatal under a forced engine, a fallback
    under `auto`. It is only ever asked of an engine that accepted, which is why
    :class:`EeEngine`, whose plan never does, does not define it.

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

    #: Whether this engine can actually fetch pixels, and may therefore be
    #: selected with `engine=`. An engine registers itself to prove the seam and
    #: to keep its sizing testable before its transport exists; until then it
    #: stays out of the selectable vocabulary rather than being offered as a
    #: choice that fails later.
    implemented: ClassVar[bool] = True

    def endpoint(self, high_volume: bool) -> str | None:
        """Return the Earth Engine host to initialise against, or `None`.

        On the ABC so the backend can ask whichever engine it resolved, instead
        of testing for one engine by name before a global `ee.Initialize`. The
        default answers `None` — initialise exactly as before — and only an
        engine that documents an alternative host overrides it.

        Args:
            high_volume: Whether the caller opted into the high-volume endpoint.

        Returns:
            `None`: this engine has one host, so there is nothing to choose.
        """
        return None

    def refusal_is_fatal(self) -> bool:
        """Whether a refusal arriving *after* routing committed should propagate.

        On the ABC with a safe default rather than left to each subclass: the
        backend calls this on whichever engine it routed to, so an engine that
        accepts a read and forgets to define it would raise `AttributeError`
        part-way through an export instead of falling back.

        Returns:
            `False` — treat a late refusal as a fallback, which is the
            conservative answer because it still produces the pixels.
        """
        return False

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
                compute, targets a CRS the reader cannot size, or pins a
                `crs_transform` the reader cannot honour), or eligible but
                declined by the sizing verdict. The message names the reason
                and a matching remedy.
            ImportError: If sizing a collection read needs the reader and the
                `[eedai]` extra is not installed.
            AuthenticationError: If a collection read's scene discovery cannot
                build the reader's credential. Forced means the key is the
                answer, so this propagates rather than falling back.
        """
        from earthlens.gee.backend import _eedai_remedy

        if not backend._eedai_eligible(var_info):
            # The pinned grid is named separately because it is the one
            # ineligibility a caller can misread as a CRS problem: the CRS is
            # fine, the reader just sizes its own grid and takes no transform.
            pinned = (
                " It also pins crs_transform, which the reader cannot honour: "
                "it sizes its own metre grid from scale."
                if backend._engine_config.crs_transform is not None
                else ""
            )
            raise ValueError(
                f"engine='eedai' cannot serve {var_info.id}: the EEDAI "
                "reader materialises pixels from an asset id, so it cannot "
                "run server-side compute (a reduced collection, cloud_mask "
                "or filters), and it reads only EPSG:4326 or a metre-based "
                f"projected CRS (got crs={backend.crs!r}).{pinned} Use "
                "engine='auto' or engine='ee'."
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
            Every other outcome — the extra not installed, an ineligible
            request, a sizing failure, a credential the reader cannot build, an
            empty bucket — is logged and routed to Earth Engine rather than
            raised, because the caller asked for a download and not for this
            engine.
        """
        # Read `eedai_available` through the backend module's binding rather
        # than importing it here: that attribute is the single seam through
        # which the optional extra's presence is resolved, and a direct import
        # would bypass it.
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


#: Every engine addressable by `engine=`. Mutable on purpose: an engine in
#: another module registers itself here on import — as
#: :class:`~earthlens.gee.pixels.PixelsEngine` does — so the accepted names are
#: derived from this one dict rather than restated anywhere.
ENGINES: dict[str, type[Engine]] = {
    cls.name: cls for cls in (AutoEngine, EeEngine, EedaiEngine)
}


def selectable_engines() -> frozenset[str]:
    """Return the engine names `engine=` accepts.

    `ENGINES` holds every registered engine; this holds the ones that can
    actually fetch pixels. They agree today, and the distinction exists so an
    engine can be registered - and its sizing tested - before its transport is
    written, without being offered as a choice that fails later.

    Returns:
        The names whose engine reports `implemented`.

    Examples:
        ```python
        >>> from earthlens.gee import pixels  # noqa: F401 - registers it
        >>> sorted(selectable_engines())
        ['auto', 'ee', 'eedai', 'pixels']

        ```
    """
    return frozenset(name for name, engine in ENGINES.items() if engine.implemented)


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
    if spec in ENGINES and not ENGINES[spec].implemented:
        raise ValueError(
            f"engine={spec!r} is registered but cannot fetch pixels yet, so it "
            f"is not selectable; use one of {sorted(selectable_engines())}"
        )
    if spec not in ENGINES:
        raise ValueError(
            f"engine must be one of {sorted(selectable_engines())}, got {spec!r}"
        )
    return ENGINES[spec]()


def reader_refusal_types() -> tuple[type[BaseException], ...]:
    """Return the exception types a post-routing reader refusal arrives as.

    Resolved through the guarded import so the optional dependency stays
    optional, and so the types come from `pyramids_eo.errors` rather than the
    reader module, which does not re-export `ReaderError`.

    Returns:
        The exception types to catch around a reader export.

    Raises:
        ImportError: If `pyramids-eo` (the `[eedai]` extra) is not installed.
            Only reached on a path that has already committed to the reader,
            which cannot have been routed to without it.
    """
    from earthlens.gee.backend import _reader_errors

    return _reader_errors(import_earthengine_reader())
