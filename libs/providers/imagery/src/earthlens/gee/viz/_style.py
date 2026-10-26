"""Translate GEE catalog metadata into digitalearth style keyword arguments.

Pure and catalog-only: nothing here imports digitalearth, cleopatra or
matplotlib, so it is safe in core and testable without the `[viz]` extra. The
output is a frozen `StyleHints` the facade forwards to a digitalearth verb.

Two sources, in this order:

1. **The publisher's own rendering**, captured on the catalog row as
   `Dataset.visualizations` (from the asset's STAC `gee:visualizations`). This
   is what Earth Engine's catalog page draws — the band triple, the stretch and
   the palette the data's publisher chose — so it wins whenever it covers the
   requested bands.
2. **Heuristics over band metadata**, when the row carries no usable rendering.
   `limits` come from `Band.min` / `Band.max`, and the colour map from a match
   table deliberately mirroring digitalearth's own
   `base/autostyle/library/variables.yml`, so a fallback style agrees with what
   digitalearth would have picked for the same variable rather than inventing a
   second opinion.

`StyleHints.source` records which of the two answered, because "the publisher
says 0-3000" and "we guessed from the band's declared range" are not the same
claim and a caller may reasonably treat them differently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict

from earthlens.gee._helpers import NoBool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from earthlens.gee.catalog import Catalog, Dataset, Visualization

__all__ = ["CMAP_MATCHES", "StyleHints", "style_for"]

#: Band-id / description substrings to a matplotlib colour map, mirroring
#: digitalearth's `base/autostyle/library/variables.yml` for the six concepts
#: it names and extending it with the Earth-Engine-specific ones it does not
#: (vegetation indices, water, categorical maps, SAR backscatter). First match
#: wins, tested case-insensitively, so the order is the priority.
CMAP_MATCHES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ndvi", "evi", "savi", "lai", "vegetation", "greenness"), "YlGn"),
    (("ndwi", "water", "occurrence", "flood", "moisture"), "Blues"),
    (("landcover", "land_cover", "lulc", "classification", "class"), "tab20"),
    (("temp", "t2m", "2t", "sst", "tas", "skt", "lst"), "coolwarm"),
    (("precip", "tp", "rain", "pr", "mtpr"), "Blues"),
    (("dem", "elev", "orog", "topo", "height", "altitude"), "terrain"),
    (("acc", "discharge", "runoff", "q_", "streamflow"), "Blues"),
    (("wind", "u10", "v10", "ws", "speed"), "viridis"),
    (("pressure", "msl", "sp", "slp"), "cividis"),
    (("vv", "vh", "hh", "hv", "backscatter", "sigma0"), "gray"),
    (("cloud", "cirrus", "aerosol"), "bone"),
)

#: The colour map used when nothing matches — digitalearth's own default.
DEFAULT_CMAP = "viridis"


class StyleHints(BaseModel):
    """Default rendering for a GEE request, as digitalearth keyword arguments.

    A frozen value object, not a renderer: it carries what to draw and how to
    stretch it, and `digitalearth_kwargs` spells that as the kwargs a
    digitalearth verb takes.

    Attributes:
        bands: The band ids to draw, in order. Three for a colour composite,
            one for a single-band stretch.
        limits: One `(min, max)` pair per band, or `()` when neither the
            publisher nor the band metadata gives a usable range.
        palette: An explicit colour ramp from the publisher, as Earth Engine
            spells it (hex without `#`, or a CSS colour name). Empty for an
            RGB composite or when only a `cmap` applies.
        cmap: A matplotlib colour map name, set only by the heuristic path and
            only for a single-band request. `None` whenever `palette` carries
            the publisher's own ramp, since the two would conflict.
        gamma: Per-band gamma from the publisher, or `()`.
        gain: Per-band multiplier from the publisher, or `()`.
        source: `"catalog"` when the publisher's own `gee:visualizations`
            answered, `"heuristic"` when this was derived from band metadata
            and the match table.

    The two `*_kwargs` methods spell the hints for the two different
    digitalearth draws — a single-band `field` / `quickmap` (`vmin` / `vmax`)
    and an `rgb_composite` (per-band `limits`) — because those verbs take
    genuinely different keywords, and `quickmap` has no RGB renderer at all.

    Examples:
        - A three-band composite is RGB:
            ```python
            >>> hints = StyleHints(
            ...     bands=("B4", "B3", "B2"),
            ...     limits=((0.0, 3000.0), (0.0, 3000.0), (0.0, 3000.0)),
            ...     source="catalog",
            ... )
            >>> hints.is_rgb
            True
            >>> hints.rgb_kwargs()["limits"][0]
            (0.0, 3000.0)

            ```
        - A single-band request stretches with `vmin` / `vmax`:
            ```python
            >>> one = StyleHints(
            ...     bands=("NDVI",), limits=((0.0, 8000.0),), cmap="YlGn", source="heuristic"
            ... )
            >>> one.field_kwargs()
            {'vmin': 0.0, 'vmax': 8000.0, 'cmap': 'YlGn'}

            ```
        - A boolean where a number belongs is refused rather than coerced:
            ```python
            >>> StyleHints(bands=("b",), gamma=(True,), source="catalog")  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for StyleHints

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bands: tuple[str, ...]
    limits: tuple[tuple[Annotated[float, NoBool], Annotated[float, NoBool]], ...] = ()
    palette: tuple[str, ...] = ()
    cmap: str | None = None
    gamma: tuple[Annotated[float, NoBool], ...] = ()
    gain: tuple[Annotated[float, NoBool], ...] = ()
    source: Literal["catalog", "heuristic"]

    @property
    def is_rgb(self) -> bool:
        """Whether these hints describe a three-band colour composite.

        Returns:
            `True` for exactly three bands, `False` otherwise.
        """
        return len(self.bands) == 3

    def field_kwargs(self) -> dict[str, Any]:
        """The keyword arguments a single-band `quickmap` / `field` draw takes.

        This is the `cleopatra` `ArrayGlyph` vocabulary that digitalearth's
        `field` (`kind="auto"`/`"imshow"`) forwards to: a scalar `vmin` / `vmax`
        stretch, a `cmap`, and `gamma` — **not** `limits`, which is the
        `rgb_composite` word and which the single-band path rejects by name.
        Only the first band's range is used, since a single-band draw has one.
        Absent hints are omitted so digitalearth's own defaults still apply.

        The publisher's `palette` is deliberately **not** here: cleopatra takes
        a named `cmap`, not a hex ramp, and converting one would need
        matplotlib imported in this renderer-free module. The palette still
        applies to the Earth-Engine-side `ServerThumbnail` / `MapTiles`
        previews, where `visParams` takes it directly.

        Returns:
            The keyword arguments, with absent hints left out.

        Examples:
            - A single-band stretch becomes `vmin` / `vmax`:
                ```python
                >>> StyleHints(
                ...     bands=("NDVI",), limits=((0.0, 8000.0),), cmap="YlGn", source="heuristic"
                ... ).field_kwargs()
                {'vmin': 0.0, 'vmax': 8000.0, 'cmap': 'YlGn'}

                ```
        """
        out: dict[str, Any] = {}
        if self.limits:
            out["vmin"], out["vmax"] = self.limits[0]
        if self.cmap is not None:
            out["cmap"] = self.cmap
        if self.gamma:
            out["gamma"] = self.gamma[0]
        return out

    def rgb_kwargs(self) -> dict[str, Any]:
        """The keyword arguments an RGB `rgb_composite` draw takes.

        `rgb_composite` reads the band **order** of the raster (its `bands=`
        default is the positional `(1, 2, 3)`), so the band *names* are not
        passed — earthlens already fetched the bands in the requested order.
        What it does take is `limits`, a per-band `(min, max)` sequence, which
        is exactly `StyleHints.limits`. `gamma` is forwarded when present.

        Returns:
            The keyword arguments, with absent hints left out.

        Examples:
            - A composite forwards its per-band limits, not band names:
                ```python
                >>> StyleHints(
                ...     bands=("B4", "B3", "B2"),
                ...     limits=((0.0, 3000.0), (0.0, 3000.0), (0.0, 3000.0)),
                ...     source="catalog",
                ... ).rgb_kwargs()
                {'limits': [(0.0, 3000.0), (0.0, 3000.0), (0.0, 3000.0)]}

                ```
        """
        out: dict[str, Any] = {}
        if self.limits:
            out["limits"] = list(self.limits)
        if self.gamma:
            out["gamma"] = list(self.gamma)
        return out


def cmap_for(*texts: str | None) -> str:
    """Pick a colour map by matching the first hit in `CMAP_MATCHES`.

    Args:
        *texts: Band id, description, units — whatever is known, in priority
            order. `None` entries are skipped.

    Returns:
        The matched colour map name, or `DEFAULT_CMAP` when nothing matches.

    Examples:
        - A vegetation index and an elevation band take different ramps:
            ```python
            >>> cmap_for("NDVI")
            'YlGn'
            >>> cmap_for("elevation")
            'terrain'

            ```
        - The description is consulted when the id says nothing:
            ```python
            >>> cmap_for("b1", "Total precipitation")
            'Blues'

            ```
        - Nothing matched falls back to digitalearth's own default:
            ```python
            >>> cmap_for("xyzzy")
            'viridis'

            ```
    """
    haystack = " ".join(t for t in texts if t).lower()
    result = DEFAULT_CMAP
    for needles, cmap in CMAP_MATCHES:
        if any(n in haystack for n in needles):
            result = cmap
            break
    return result


def _pick_visualization(
    row: Dataset, bands: tuple[str, ...] | None, name: str | None
) -> Visualization | None:
    """Choose the publisher rendering that fits the request, or `None`.

    A rendering is usable when it draws exactly the requested bands; with no
    bands requested, the one whose `name` was asked for, else the first.
    """
    candidates = list(row.visualizations)
    chosen: Visualization | None = None
    if name is not None:
        chosen = next((v for v in candidates if v.name == name), None)
    elif bands is None:
        chosen = candidates[0] if candidates else None
    else:
        chosen = next((v for v in candidates if tuple(v.bands) == bands), None)
    return chosen


def _heuristic_hints(row: Dataset, bands: tuple[str, ...]) -> StyleHints:
    """Build hints from band metadata and the match table."""
    limits: list[tuple[float, float]] = []
    for band_id in bands:
        band = row.get_band(band_id)
        if band.min is not None and band.max is not None:
            limits.append((float(band.min), float(band.max)))
    usable = tuple(limits) if len(limits) == len(bands) else ()
    cmap = None
    if len(bands) != 3:
        first = row.get_band(bands[0])
        cmap = cmap_for(first.id, first.description, first.units)
    return StyleHints(bands=bands, limits=usable, cmap=cmap, source="heuristic")


def style_for(
    asset_id: str,
    bands: tuple[str, ...] | None = None,
    *,
    name: str | None = None,
    catalog: Catalog | None = None,
) -> StyleHints:
    """Resolve the default rendering for a dataset and band selection.

    Args:
        asset_id: The Earth Engine asset id, as the catalog spells it.
        bands: The band ids to draw. `None` takes the publisher's own band
            selection when the row has one, else the row's first band.
        name: Pick a specific publisher rendering by its `display_name`
            (`"True Color (432)"`), for a dataset that offers several. Only
            meaningful with `bands=None`.
        catalog: An already-loaded catalog, to avoid re-parsing it per call.

    Returns:
        The `StyleHints` for the request.

    Raises:
        ValueError: If `asset_id` is not a curated dataset, if `bands` is an
            empty tuple, if a named band is not on the row, or if `name` does
            not match any of the row's renderings. The unknown-band message
            comes from `Dataset.get_band`, so it carries the same
            closest-match suggestion as everywhere else.

    Examples:
        - A request for three bands renders as a composite:
            ```python
            >>> hints = style_for("COPERNICUS/S2_SR_HARMONIZED", ("B4", "B3", "B2"))
            >>> hints.bands
            ('B4', 'B3', 'B2')
            >>> hints.is_rgb
            True

            ```
        - A single band resolves to a one-band stretch (here from the
          publisher's own rendering, so `source` is `"catalog"`):
            ```python
            >>> one = style_for("USGS/SRTMGL1_003", ("elevation",))
            >>> len(one.limits)
            1
            >>> one.source
            'catalog'

            ```
        - A misspelt band is refused with a suggestion, not silently dropped:
            ```python
            >>> style_for("USGS/SRTMGL1_003", ("elevashun",))  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'elevashun' is not a band of 'USGS/SRTMGL1_003'. ...Did you mean 'elevation'?

            ```
        - An empty band selection is a mistake, not "use the default":
            ```python
            >>> style_for("USGS/SRTMGL1_003", ())
            Traceback (most recent call last):
                ...
            ValueError: bands=() names no band to style; pass the band ids or None for the dataset default

            ```
    """
    from earthlens.gee.catalog import Catalog as _Catalog

    if bands is not None and not bands:
        raise ValueError(
            "bands=() names no band to style; pass the band ids or None for "
            "the dataset default"
        )
    cat = catalog if catalog is not None else _Catalog()
    row = _resolved_row(cat, asset_id, name)
    chosen = _pick_visualization(row, bands, name)
    if bands is not None:
        for band_id in bands:
            row.get_band(band_id)
    if chosen is not None:
        hints = _catalog_hints(chosen)
    else:
        hints = _default_hints(row, bands, asset_id)
    return hints


def _resolved_row(catalog: Catalog, asset_id: str, name: str | None) -> Dataset:
    """Return the catalog row for `asset_id`, refusing an unknown id or `name`.

    Args:
        catalog: The loaded catalog to look `asset_id` up in.
        asset_id: The Earth Engine asset id.
        name: A rendering name to require on the row, or `None`.

    Returns:
        The matching `Dataset`.

    Raises:
        ValueError: If `asset_id` is not curated, or `name` names no rendering.
    """
    row = catalog.datasets.get(asset_id)
    if row is None:
        raise ValueError(
            f"{asset_id!r} is not a curated GEE dataset, so it has no style to "
            f"resolve; list the curated ids with Catalog().datasets"
        )
    if name is not None and not any(v.name == name for v in row.visualizations):
        known = ", ".join(repr(v.name) for v in row.visualizations) or "none"
        raise ValueError(
            f"{name!r} is not a rendering of {asset_id!r}; it offers {known}"
        )
    return row


def _catalog_hints(chosen: Visualization) -> StyleHints:
    """Build `StyleHints` from a publisher rendering."""
    return StyleHints(
        bands=tuple(chosen.bands),
        limits=chosen.limits,
        palette=tuple(chosen.palette),
        gamma=tuple(chosen.gamma),
        gain=tuple(chosen.gain),
        source="catalog",
    )


def _default_hints(
    row: Dataset, bands: tuple[str, ...] | None, asset_id: str
) -> StyleHints:
    """Build heuristic hints for the requested bands, or the row's first band.

    Raises:
        ValueError: If no bands are requested and the row carries none.
    """
    selected = bands if bands is not None else (next(iter(row.bands), ""),)
    if not selected[0]:
        raise ValueError(
            f"{asset_id!r} carries no bands, so it has no default to style; "
            f"hydrate the row or pass bands explicitly"
        )
    return _heuristic_hints(row, selected)
