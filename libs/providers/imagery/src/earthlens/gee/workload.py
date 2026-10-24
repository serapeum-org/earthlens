"""`WorkloadTag`: a nest-safe scope for Earth Engine workload tags.

Earth Engine attaches a *workload tag* to every request it serves, so Cloud
billing can attribute cost per run: with a tag set, `getDownloadURL` fetches,
`computePixels` calls and `ee.batch` exports all carry it. earthlens had no
way to set one, so all its traffic was billed untagged.

The SDK's own context manager, `ee.data.workloadTagContext`, is **not
nest-safe**: its exit calls `resetWorkloadTag()`, which resets to the
*default* tag rather than the enclosing one, so an inner scope wipes the tag
of the outer scope it sits in. `WorkloadTag` is the save-and-restore
alternative: `__enter__` records `ee.data.getWorkloadTag()` before setting its
own tag, `__exit__` puts the saved value back (on the exception path too), and
the saved values live on a stack so even the same object can be re-entered.

The tag covers the Earth Engine-served calls — exactly where billing accrues.
The EEDAI reader path (`engine="eedai"`) fetches stored pixels through GDAL's
EEDAI driver, not through `ee`, so those reads are never attributed: a run
that only uses the reader gains nothing from a tag.

**The scope is per-process and not thread-safe.** The tag it sets is
process-wide SDK state (`ee/_state.py`), and the save stack is a plain list,
so two threads entering scopes concurrently interleave their saves and can
restore each other's tags. Nesting within *one* thread is what the stack
makes safe. Tag a run from the thread that drives it, or give each worker its
own process.

Earth Engine constrains the string, and `WorkloadTag` enforces the
constraint at construction, so a typo'd tag raises immediately instead of
mid-download. The rule is **the SDK's enforced one, not its documented
one**: `ee/_state.py`'s `_WorkloadTag.validate` requires 1-63 characters
beginning and ending with a *lowercase* alphanumeric character, with `-`
and `_` allowed between, while `ee.data.setWorkloadTag`'s docstring
describes a looser rule that also allows case and dots. The two disagree,
and the validator is what runs — `setWorkloadTag("A1")` and `("a.b")` both
raise `ValueError` on earthengine-api 1.7.35 — so `WorkloadTag` matches the
validator. The empty string is the SDK's "no tag" sentinel rather than a
tag, so it is refused too.

Examples:
    - Nesting restores the *enclosing* tag, not the default (the workload-tag
      state is client-side, so this runs without credentials):
        ```python
        >>> import ee
        >>> from earthlens.gee import WorkloadTag
        >>> with WorkloadTag(tag="outer-run"):
        ...     with WorkloadTag(tag="inner-run"):
        ...         ee.data.getWorkloadTag()
        ...     ee.data.getWorkloadTag()
        'inner-run'
        'outer-run'
        >>> ee.data.getWorkloadTag()
        ''

        ```
    - An illegal tag is refused at construction — including the uppercase
      and dotted forms the SDK's docstring claims to allow:
        ```python
        >>> from earthlens.gee import WorkloadTag
        >>> WorkloadTag(tag="-starts-wrong")
        Traceback (most recent call last):
            ...
        pydantic_core._pydantic_core.ValidationError: ...
        >>> WorkloadTag(tag="Run-42")
        Traceback (most recent call last):
            ...
        pydantic_core._pydantic_core.ValidationError: ...

        ```
"""

from __future__ import annotations

import re
from types import TracebackType

import ee
from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator

__all__ = ["WorkloadTag"]

#: Earth Engine's *enforced* workload-tag rule, copied from the SDK's own
#: validator (`ee/_state.py`, `_WorkloadTag.validate`): 1-63 characters,
#: beginning and ending with a **lowercase** alphanumeric character, with
#: dashes and underscores allowed between. `ee.data.setWorkloadTag`'s
#: docstring describes a looser rule (case, and dots, allowed) that the
#: validator refuses — this is the one that decides whether a request flies.
#: Applied with `fullmatch`, as the SDK applies it: under `match`, the `$`
#: also matches just before a trailing newline, so `"ab\n"` would pass here
#: and then be rejected by `ee.data.setWorkloadTag`.
_TAG_RE = re.compile(r"^[a-z0-9](?:[-_a-z0-9]{0,61}[a-z0-9])?$")


class WorkloadTag(BaseModel):
    """Scope Earth Engine traffic under a billing workload tag, nest-safely.

    A frozen value object holding a validated `tag`. Entering sets it via
    `ee.data.setWorkloadTag`; exiting restores whatever
    `ee.data.getWorkloadTag` answered on entry — the enclosing scope's tag,
    not the default — whether the body returned or raised. That is the one
    behaviour `ee.data.workloadTagContext` lacks: its exit resets to the
    *default* tag, so nesting it wipes the outer scope.

    `GEE.workload_tag` is the backend-side convenience returning one, so
    `with gee.workload_tag("run-42"): gee.download()` tags every Earth
    Engine-served request the download makes. The EEDAI reader path is
    separate and stays unattributed.

    The tag is **process-wide** SDK state and the save stack is a plain list,
    so the scope is nest-safe within one thread but **not thread-safe**: two
    threads entering scopes concurrently interleave their saves and can
    restore each other's tags. Exiting a scope that was never entered raises
    `RuntimeError` rather than popping an empty stack.

    The `tag` must satisfy the rule the SDK's validator enforces — 1-63
    characters, lowercase alphanumeric at both ends, `-` and `_` between —
    which is stricter than the rule `ee.data.setWorkloadTag`'s docstring
    states; see the module docstring.

    Examples:
        - A scope is a frozen value object, constructed without Earth Engine:
            ```python
            >>> from earthlens.gee import WorkloadTag
            >>> scope = WorkloadTag(tag="run-42")
            >>> scope.tag
            'run-42'
            >>> scope.tag = "other"
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
        - Entering sets the tag and exiting restores the saved one:
            ```python
            >>> import ee
            >>> from earthlens.gee import WorkloadTag
            >>> with WorkloadTag(tag="run-42"):
            ...     ee.data.getWorkloadTag()
            'run-42'
            >>> ee.data.getWorkloadTag()
            ''

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str  # the workload tag EE attaches to every served request

    #: The tags saved by `__enter__` and consumed by `__exit__`, restored
    #: verbatim. A stack rather than a single slot, so re-entering the same
    #: object nests instead of clobbering the first saved value.
    _saved: list[int | str | None] = PrivateAttr(default_factory=list)

    @field_validator("tag")
    @classmethod
    def _well_formed(cls, value: str) -> str:
        """Refuse a tag the Earth Engine SDK's own validator would reject.

        Args:
            value: The parsed `tag` input.

        Returns:
            The value unchanged when it matches the rule.

        Raises:
            ValueError: If `value` is not 1-63 characters beginning and ending
                with a lowercase alphanumeric character, with only `-` or `_`
                between.
        """
        if _TAG_RE.fullmatch(value) is None:
            raise ValueError(
                "workload tag must be 1-63 characters, starting and ending "
                "with a lowercase alphanumeric character, with '-' or '_' "
                f"allowed between, got {value!r}"
            )
        return value

    def __enter__(self) -> WorkloadTag:
        """Save the current workload tag and set this one.

        Returns:
            This scope, so `with ... as scope:` hands back the value object.
        """
        self._saved.append(ee.data.getWorkloadTag())
        ee.data.setWorkloadTag(self.tag)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Restore the tag saved on entry, whether or not the body raised.

        Args:
            exc_type: The exception class, when the body raised.
            exc: The exception instance, when the body raised.
            tb: Its traceback, when the body raised.

        Returns:
            `None`, so an in-flight exception always propagates.

        Raises:
            RuntimeError: If the scope was never entered (or has already been
                exited) — a bare `IndexError: pop from empty list` named
                neither the object nor the mistake. Checked before the
                `setWorkloadTag` call, so the process-wide tag is left as it
                was.
        """
        if not self._saved:
            raise RuntimeError(
                f"WorkloadTag(tag={self.tag!r}) was not entered, so there is "
                "no saved tag to restore; use it as a context manager "
                "(`with gee.workload_tag(...):`) rather than calling "
                "__exit__ directly"
            )
        ee.data.setWorkloadTag(self._saved.pop())
