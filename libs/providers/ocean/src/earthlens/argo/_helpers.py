"""Pure request helpers for the Argo float backend.

Stateless, network-free helpers the :class:`earthlens.argo.backend.ARGO`
backend composes: parsing the selection mode out of `variables`
(:func:`parse_selection`), building the 8-element `argopy`
`.region([...])` box (:func:`region_box`), and the zero-row fallback for
an empty fetch (:func:`empty_canonical`). Keeping these here — free of
any `argopy` import — lets them be unit-tested without the optional
`[argo]` extra installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from earthlens.base import SpatialExtent, TemporalExtent

#: The standard Argo data-acknowledgement statement (`G6`). Emitted once
#: as a `logger.info` on a successful fetch — attribution courtesy, not a
#: usage restriction, so it is never a warning.
ARGO_ACKNOWLEDGEMENT: str = (
    "These data were collected and made freely available by the "
    "International Argo Program and the national programs that contribute "
    "to it (https://argo.ucsd.edu, https://www.ocean-ops.org). The Argo "
    "Program is part of the Global Ocean Observing System."
)

#: Canonical long-format columns for the empty-fetch fallback (`G8`): the
#: float / cycle / position / time identity plus the core measured
#: variables, pinned from the live `argopy` phy region columns (see
#: the A1 gate captures).
ARGO_COLUMNS: list[str] = [
    "PLATFORM_NUMBER",
    "CYCLE_NUMBER",
    "DIRECTION",
    "DATA_MODE",
    "TIME",
    "LATITUDE",
    "LONGITUDE",
    "PRES",
    "TEMP",
    "PSAL",
]


@dataclass(frozen=True)
class Selection:
    """Which `argopy` selection method :meth:`ARGO._fetch` should call.

    Attributes:
        kind: `"region"` (bbox + depth + time), `"float"` (one or more
            WMO ids), or `"profile"` (one WMO + a cycle number).
        wmos: The WMO float ids for a `"float"` / `"profile"` selection;
            empty for `"region"`.
        cycle: The cycle number for a `"profile"` selection; `None`
            otherwise.
    """

    kind: str
    wmos: tuple[int, ...] = ()
    cycle: int | None = None


#: The selector prefixes that switch `variables` out of region mode.
_SELECTOR_PREFIXES: tuple[str, ...] = ("float:", "profile:")


def parse_selection(variables: list[str]) -> Selection:
    """Route a `variables` list to its `argopy` selection mode (`G2`).

    A `variables` list is either a set of parameter names (`["TEMP",
    "PSAL"]`) — a **region** selection — or a single `float:` / `profile:`
    selector token. The selector, when present, must be the sole entry.

    Args:
        variables: The request `variables`. `"float:6902746"` (or a
            comma-separated `"float:6902746,6902747"`) selects floats by
            WMO id; `"profile:6902746/12"` selects one float's cycle;
            anything else (parameter names, or an empty list) is a region
            selection.

    Returns:
        Selection: The parsed selection mode.

    Raises:
        ValueError: If a `float:` / `profile:` selector is mixed with
            other entries, a `profile:` token omits its `/<cycle>`, or a
            WMO id / cycle is not an integer.
    """
    selectors = [v for v in variables if v.startswith(_SELECTOR_PREFIXES)]
    if not selectors:
        return Selection("region")
    if len(selectors) > 1 or len(variables) > 1:
        raise ValueError(
            "An Argo float:/profile: selector must be the only entry in "
            f"variables=, got {variables!r}. Pass e.g. "
            "variables=['float:6902746'] on its own."
        )
    token = selectors[0]
    if token.startswith("float:"):
        body = token[len("float:") :]
        wmos = tuple(
            _selector_int(part, token, "WMO id") for part in body.split(",") if part
        )
        if not wmos:
            raise ValueError(f"float: selector has no WMO id: {token!r}.")
        return Selection("float", wmos)
    body = token[len("profile:") :]
    wmo_part, sep, cycle_part = body.partition("/")
    if not sep or not wmo_part or not cycle_part:
        raise ValueError(
            f"profile: selector must be 'profile:<WMO>/<cycle>', got {token!r}."
        )
    return Selection(
        "profile",
        (_selector_int(wmo_part, token, "WMO id"),),
        _selector_int(cycle_part, token, "cycle"),
    )


def _selector_int(value: str, token: str, what: str) -> int:
    """Convert a selector field to `int`, with a token-aware error message.

    Args:
        value: The substring to parse (a WMO id or cycle number).
        token: The full selector token, for the error message.
        what: A short label for `value` (`"WMO id"` / `"cycle"`).

    Returns:
        int: The parsed integer.

    Raises:
        ValueError: If `value` is not an integer; the message names the
            offending token and the expected selector shape.
    """
    try:
        return int(value)
    except ValueError:
        raise ValueError(
            f"Argo {token!r} selector has a non-numeric {what} {value!r}; "
            f"expected an integer (e.g. 'float:6902746' / "
            f"'profile:6902746/12')."
        ) from None


def region_box(
    space: SpatialExtent,
    time: TemporalExtent,
    depth: tuple[float, float],
) -> list:
    """Build the 8-element `argopy` `.region([...])` box (`G7`).

    The element order is pinned from the live `argopy` 1.4.0 docstring
    (see the A1 gate captures):
    `[lon_min, lon_max, lat_min, lat_max, depth_min, depth_max,
    date_min, date_max]`.

    Args:
        space: The request bbox (`west`/`east`/`south`/`north`).
        time: The request window (`start_date`/`end_date`).
        depth: The `(min, max)` depth range in dbar.

    Returns:
        list: The 8-element box, dates as ISO-8601 strings.
    """
    return [
        space.west,
        space.east,
        space.south,
        space.north,
        depth[0],
        depth[1],
        time.start_date.isoformat(),
        time.end_date.isoformat(),
    ]


def empty_canonical(columns: list[str] = ARGO_COLUMNS) -> pd.DataFrame:
    """Return a zero-row frame with the canonical Argo columns (`G8`).

    Args:
        columns: The columns to give the empty frame; defaults to
            :data:`ARGO_COLUMNS`.

    Returns:
        pd.DataFrame: An empty frame with exactly `columns`.
    """
    import pandas as pd

    return pd.DataFrame({column: [] for column in columns})
