"""Pure, network-free helpers for the SoilGrids backend.

SoilGrids 2.0 publishes each soil property as an independent OGC WCS service
whose coverages are named `<property>_<depth>_<quantile>` (e.g.
`nitrogen_5-15cm_Q0.5`). This module owns the provider glue earthlens is
responsible for: composing that coverage id, expanding a `(properties, depths,
quantiles)` request into the concrete `(property, depth, quantile)` triples to
fetch (validating each against the catalog with a did-you-mean hint), and
turning a `SpatialExtent` into a WCS bbox. The actual WCS transport lives in
`pyramids` (`Dataset.from_wcs`); this module never touches the network.
"""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from earthlens.base.abstractdatasource import SpatialExtent

    from earthlens.soilgrids.catalog import Catalog

#: The quantile layer requested when a call names properties but no quantiles
#: (`G7`): the central mean prediction, present for every property.
DEFAULT_QUANTILE: str = "mean"

#: SoilGrids' native grid CRS (`EPSG:152160`) is a custom Interrupted Goode
#: Homolosine that is absent from the PROJ database, so GDAL's WCS driver cannot
#: place the request window without help. This proj4 string is passed as
#: `pyramids.dataset.Dataset.from_wcs(coverage_crs=...)` so the coverage's real
#: CRS is attached client-side (pinned in the `A1` gate).
IGH_PROJ4: str = "+proj=igh +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs"

#: CC-BY 4.0 attribution string logged once per successful download (`G6`).
SOILGRIDS_ATTRIBUTION: str = (
    "SoilGrids 2.0 (c) ISRIC - World Soil Information, licensed CC-BY 4.0; "
    "cite Poggio et al. (2021), SOIL 7, 217-240. https://soilgrids.org"
)


def coverage_id(property_id: str, depth: str, quantile: str) -> str:
    """Compose the WCS `COVERAGEID` for one `(property, depth, quantile)`.

    Args:
        property_id: A SoilGrids property id (`"nitrogen"`, `"phh2o"`).
        depth: A depth interval token (`"5-15cm"`, `"0-30cm"`).
        quantile: A quantile / layer token (`"Q0.5"`, `"mean"`,
            `"uncertainty"`).

    Returns:
        str: The coverage id `"<property>_<depth>_<quantile>"`.

    Examples:
        - The three parts join with underscores:
            ```python
            >>> from earthlens.soilgrids._helpers import coverage_id
            >>> coverage_id("nitrogen", "5-15cm", "Q0.5")
            'nitrogen_5-15cm_Q0.5'
            >>> coverage_id("clay", "0-5cm", "mean")
            'clay_0-5cm_mean'

            ```
    """
    return f"{property_id}_{depth}_{quantile}"


def _validate_dimension(
    requested: list[str], allowed: list[str], noun: str, property_id: str
) -> list[str]:
    """Validate requested depth / quantile tokens against a property's set.

    Args:
        requested: The tokens the caller asked for.
        allowed: The tokens the property publishes.
        noun: `"depth"` or `"quantile"`, for the error message.
        property_id: The property being validated, for the error message.

    Returns:
        list[str]: `requested` unchanged (every token is valid).

    Raises:
        ValueError: If any token is not in `allowed`; the message lists the
            valid options with a did-you-mean hint.
    """
    unknown = [token for token in requested if token not in allowed]
    if unknown:
        first = unknown[0]
        close = difflib.get_close_matches(first, allowed, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"{property_id!r} has no {noun} {first!r}; "
            f"available {noun}s: {allowed}.{hint}"
        )
    return requested


def expand_request(
    properties: list[str],
    depths: list[str] | None,
    quantiles: list[str] | None,
    catalog: Catalog,
) -> list[tuple[str, str, str]]:
    """Expand a request into the concrete `(property, depth, quantile)` triples.

    Resolves every property against the catalog (did-you-mean on a miss) and
    takes the Cartesian product of the selected depths and quantiles per
    property. Defaults (`G7`): when `depths` is `None` every depth the property
    publishes is used (all six standard depths, or the single `0-30cm` for
    `ocs`); when `quantiles` is `None` only the `mean` layer is used. Every
    explicit depth / quantile is validated against the property's own set.

    Args:
        properties: SoilGrids property ids (`["clay", "phh2o"]`).
        depths: Depth tokens to fetch, or `None` for all of each property's
            depths.
        quantiles: Quantile / layer tokens to fetch, or `None` for
            `["mean"]`.
        catalog: The `Catalog` the properties + tokens are validated against.

    Returns:
        list[tuple[str, str, str]]: One `(property, depth, quantile)` triple
            per coverage to fetch, in request order.

    Raises:
        ValueError: If a property is unknown, or a requested depth / quantile
            is not published by a property (did-you-mean surfaced).

    Examples:
        - Only-properties defaults to every depth at the `mean` layer:
            ```python
            >>> from earthlens.soilgrids import Catalog
            >>> from earthlens.soilgrids._helpers import expand_request
            >>> triples = expand_request(["clay"], None, None, Catalog())
            >>> len(triples)
            6
            >>> triples[0]
            ('clay', '0-5cm', 'mean')

            ```
    """
    triples: list[tuple[str, str, str]] = []
    for property_id in properties:
        row = catalog.get(property_id)
        sel_depths = (
            _validate_dimension(depths, row.depths, "depth", row.id)
            if depths is not None
            else list(row.depths)
        )
        sel_quantiles = (
            _validate_dimension(quantiles, row.quantiles, "quantile", row.id)
            if quantiles is not None
            else [DEFAULT_QUANTILE]
        )
        for depth in sel_depths:
            for quantile in sel_quantiles:
                triples.append((row.id, depth, quantile))
    return triples


def bbox_from_extent(space: SpatialExtent) -> tuple[float, float, float, float]:
    """Return the `(west, south, east, north)` WCS bbox of a spatial extent.

    Args:
        space: A `SpatialExtent` (the backend's `self.space`) exposing
            `west` / `south` / `east` / `north`.

    Returns:
        tuple[float, float, float, float]: `(west, south, east, north)` in
            degrees (EPSG:4326).
    """
    return (space.west, space.south, space.east, space.north)
