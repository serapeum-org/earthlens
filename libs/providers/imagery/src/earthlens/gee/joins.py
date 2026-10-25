"""Composable wrappers over `ee.Join` — link two collections on a condition.

Earth Engine's join API is verbose: pick a factory
(`ee.Join.saveAll(matchesKey=..., ordering=..., ascending=...)`), then call
`.apply(primary, secondary, condition)` with an `ee.Filter` condition. The
wrappers here name that intent as frozen, validated config objects — a typo'd
keyword or a missing required key raises at construction instead of surfacing
as an opaque server error — while `apply` stays the SDK's own
`(primary, secondary, condition)` shape.

A join is inherently a two-collection operation, so these are standalone
composables, never drop-ins for the single-collection `filters=` hook or the
per-image `cloud_mask=` hook. The condition is an ordinary `ee.Filter`
(`ee.Filter.equals`, `ee.Filter.maxDifference`, ... — combined with
`ee.Filter.And`), exactly as raw Earth Engine spells it; the wrappers add
nothing between it and the join.

The five factories map one to one:

| Wrapper | Factory | Pairs each primary element with |
|---|---|---|
| `InnerJoin` | `ee.Join.inner` | every match, one result per pair |
| `SaveAllJoin` | `ee.Join.saveAll` | the full match list, as a property |
| `SaveBestJoin` | `ee.Join.saveBest` | the best-measure match |
| `SaveFirstJoin` | `ee.Join.saveFirst` | the first match in `ordering` |
| `SimpleJoin` | `ee.Join.simple` | nothing — it filters to matched elements |

`GapFill._join_neighbours` builds its within-`max_gap` neighbour pairing
through `SaveAllJoin` — the wrapper's first in-repo consumer — and
`CloudScorePlus` stays on `linkCollection`, which is a per-image
`system:index` link rather than a join.

Joins are server-side compute: a pipeline using one runs on an Earth Engine
engine, never the EEDAI reader (which reads stored pixels and cannot run a
graph).
"""

from __future__ import annotations

from typing import Any

import ee
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "InnerJoin",
    "Join",
    "SaveAllJoin",
    "SaveBestJoin",
    "SaveFirstJoin",
    "SimpleJoin",
]


class Join(BaseModel):
    """A frozen `ee.Join` configuration with an explicit `apply` seam.

    Subclasses carry the factory's keyword arguments as validated pydantic
    fields (`frozen=True`, `extra="forbid"`) and implement `_join()`; the
    shared `apply` builds the configured `ee.Join` and applies it to the two
    collections with the given condition.

    Examples:
        - The base class only names the contract; applying it raises:
            ```python
            >>> Join().apply("primary", "secondary", condition=None)
            Traceback (most recent call last):
                ...
            NotImplementedError: Join must implement _join()

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def _join(self) -> Any:
        """Build the configured `ee.Join`; concrete wrappers implement this.

        Returns:
            The `ee.Join` this wrapper stands for.

        Raises:
            NotImplementedError: Always — `Join` itself is the contract, not a
                join.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement _join()")

    def apply(self, primary: Any, secondary: Any, condition: Any) -> Any:
        """Join `secondary` onto `primary` where `condition` matches.

        Args:
            primary: The `ee.ImageCollection` / `ee.FeatureCollection` the
                result is built from.
            secondary: The collection matched against it.
            condition: The `ee.Filter` join condition — e.g.
                `ee.Filter.equals(leftField=..., rightField=...)`, or an
                `ee.Filter.And` combination; `maxDifference` /
                `withinDistance` conditions also produce the join measure
                the `measure_key` fields can save.

        Returns:
            The joined collection, as Earth Engine returns it. The server
            hands back a generic collection, so wrap the result in
            `ee.ImageCollection(...)` when the elements are images.
        """
        return self._join().apply(
            primary=primary, secondary=secondary, condition=condition
        )


class InnerJoin(Join):
    """Pair every matching element pair — `ee.Join.inner`.

    Each result is a feature carrying the primary element under
    `primary_key` and the matching secondary element under `secondary_key`;
    a primary element with several matches yields several results.

    Attributes:
        primary_key: The property each result saves the primary element under.
        secondary_key: The property each result saves the matching secondary
            element under.
        measure_key: Save the join measure under this property, when the
            condition produces one (`maxDifference` / `withinDistance`).
            `None` (the default) attaches no measure.

    Examples:
        - The config mirrors ee's documented defaults:
            ```python
            >>> join = InnerJoin()
            >>> (join.primary_key, join.secondary_key, join.measure_key)
            ('primary', 'secondary', None)

            ```
    """

    primary_key: str = "primary"
    secondary_key: str = "secondary"
    measure_key: str | None = None

    def _join(self) -> Any:
        """Build the configured `ee.Join.inner`.

        Returns:
            The inner `ee.Join`.
        """
        kwargs: dict[str, Any] = {
            "primaryKey": self.primary_key,
            "secondaryKey": self.secondary_key,
        }
        if self.measure_key is not None:
            kwargs["measureKey"] = self.measure_key
        return ee.Join.inner(**kwargs)


class _OrderedJoin(Join):
    """Shared ordering / measure / outer config for the save-style joins.

    Attributes:
        ordering: The property the matches are sorted on before being saved;
            `None` (the default) leaves them unordered.
        ascending: Sort direction for `ordering`. Meaningless without it, so
            `ascending=False` with no `ordering` is refused at construction.
        measure_key: Save the join measure under this property, when the
            condition produces one (`maxDifference` / `withinDistance`).
        outer: Keep primary elements that matched nothing. Earth Engine's
            default (`False`) drops them.
    """

    ordering: str | None = None
    ascending: bool = True
    measure_key: str | None = None
    outer: bool = False

    @model_validator(mode="after")
    def _sortable(self) -> _OrderedJoin:
        """Refuse a sort direction with nothing to sort.

        Returns:
            The config unchanged once it is consistent.

        Raises:
            ValueError: If `ascending=False` was given without `ordering`.
        """
        if self.ordering is None and self.ascending is False:
            raise ValueError(
                "ascending=False needs ordering= (there is nothing to sort)"
            )
        return self

    def _ordered_kwargs(self, key_name: str, key: str) -> dict[str, Any]:
        """Build the factory kwargs, naming only the knobs that are set.

        Args:
            key_name: The factory's wire name for the save key
                (`matchesKey` / `matchKey`).
            key: The configured save key.

        Returns:
            The keyword arguments for the `ee.Join.saveAll` / `saveFirst`
            factory, with `ascending` accompanying `ordering` and the
            optional `measureKey` / `outer` included only when set.
        """
        kwargs: dict[str, Any] = {key_name: key}
        if self.ordering is not None:
            kwargs["ordering"] = self.ordering
            kwargs["ascending"] = self.ascending
        if self.measure_key is not None:
            kwargs["measureKey"] = self.measure_key
        if self.outer:
            kwargs["outer"] = True
        return kwargs


class SaveAllJoin(_OrderedJoin):
    """Save every match as a list property — `ee.Join.saveAll`.

    Each primary element comes back with the full list of its matches under
    `matches_key`, optionally sorted — the join behind temporal
    neighbourhoods, and the one `GapFill._join_neighbours` builds its
    within-`max_gap` `before` / `after` lists with.

    Attributes:
        matches_key: The property the match list is saved under.

    Examples:
        - The gapfill neighbour pairing, as config:
            ```python
            >>> join = SaveAllJoin(
            ...     matches_key="after", ordering="system:time_start", ascending=True
            ... )
            >>> (join.matches_key, join.ordering, join.ascending)
            ('after', 'system:time_start', True)

            ```
        - A sort direction without a sort property is refused:
            ```python
            >>> SaveAllJoin(ascending=False)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    matches_key: str = "matches"

    def _join(self) -> Any:
        """Build the configured `ee.Join.saveAll`.

        Returns:
            The save-all `ee.Join`.
        """
        return ee.Join.saveAll(**self._ordered_kwargs("matchesKey", self.matches_key))


class SaveBestJoin(Join):
    """Save the single best-measure match — `ee.Join.saveBest`.

    The condition must produce a join measure (`maxDifference` /
    `withinDistance`); the match with the best measure is saved under
    `match_key` and its measure under `measure_key`. Both keys are required —
    the ee factory has no defaults for them.

    Attributes:
        match_key: The property the best match is saved under.
        measure_key: The property its join measure is saved under.
        outer: Keep primary elements that matched nothing. Earth Engine's
            default (`False`) drops them.

    Examples:
        - Both keys are required at construction:
            ```python
            >>> SaveBestJoin(match_key="best")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    match_key: str
    measure_key: str
    outer: bool = False

    def _join(self) -> Any:
        """Build the configured `ee.Join.saveBest`.

        Returns:
            The save-best `ee.Join`.
        """
        kwargs: dict[str, Any] = {
            "matchKey": self.match_key,
            "measureKey": self.measure_key,
        }
        if self.outer:
            kwargs["outer"] = True
        return ee.Join.saveBest(**kwargs)


class SaveFirstJoin(_OrderedJoin):
    """Save the first match in sort order — `ee.Join.saveFirst`.

    Each primary element comes back with its first match (by `ordering`)
    under `match_key` — e.g. the nearest-in-time scene from a second
    collection, without carrying the whole match list.

    Attributes:
        match_key: The property the first match is saved under.

    Examples:
        - The nearest-scene idiom, as config:
            ```python
            >>> join = SaveFirstJoin(match_key="closest", ordering="system:time_start")
            >>> (join.match_key, join.ordering, join.ascending)
            ('closest', 'system:time_start', True)

            ```
    """

    match_key: str

    def _join(self) -> Any:
        """Build the configured `ee.Join.saveFirst`.

        Returns:
            The save-first `ee.Join`.
        """
        return ee.Join.saveFirst(**self._ordered_kwargs("matchKey", self.match_key))


class SimpleJoin(Join):
    """Filter the primary collection to its matched elements — `ee.Join.simple`.

    No properties are added: the result is simply the primary elements that
    matched at least one secondary element, in their original form — set
    intersection by condition.

    Examples:
        - There is nothing to configure:
            ```python
            >>> SimpleJoin()
            SimpleJoin()

            ```
    """

    def _join(self) -> Any:
        """Build `ee.Join.simple`.

        Returns:
            The simple `ee.Join`.
        """
        return ee.Join.simple()
