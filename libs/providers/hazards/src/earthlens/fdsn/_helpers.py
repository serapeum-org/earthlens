"""Private helpers for the FDSN ShakeMap side-output.

The FDSN event standard obspy speaks carries no product links: a
`get_events` response is QuakeML, which stops at an event's origins and
magnitudes. USGS publishes its gridded ShakeMap through a *different*
surface — the ComCat event-detail GeoJSON — so pulling a shaking field
for an event means a second, USGS-only request keyed by that event's
ComCat id. This module holds the steps of that path, kept out of
`backend.py` so each is testable on its own:

1. `parse_comcat_id` — recover the ComCat id from the QuakeML resource
   identifier `earthlens.fdsn.events` records in the `event_id` column.
2. `detail_url` / `shakemap_raster_url` — address the detail document
   and walk it to the ShakeMap raster archive's download URL.
3. `extract_layers` / `flt_to_geotiff` — unpack the archive and convert
   one grid into a georeferenced GeoTIFF.

**The archive is not a GeoTIFF.** `download/raster.zip` holds fourteen
ESRI float grids — a `.flt` payload plus a `.hdr` header per layer,
which GDAL reads through its `EHdr` driver — covering seven intensity
measures (`mmi`, `pga`, `pgv`, and spectral acceleration at 0.3 / 0.6 /
1.0 / 3.0 s) each as a `_mean` and a `_std`. The headers carry an
origin, a cell size, and a nodata value, but **no projection**, so
`flt_to_geotiff` assigns `EPSG:4326` explicitly — the grids are
published on a plain lon/lat graticule.

The conversion itself goes through pyramids rather than raw GDAL:
reading a format and writing another one is pyramids' scope, and
earthlens only decides *which* grid to ask for.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from loguru import logger
from pyramids.dataset import Dataset

#: ComCat event-detail endpoint. The same FDSN-event service the backend
#: already queries, asked for `format=geojson` instead of QuakeML — only
#: that representation carries the `products` block.
COMCAT_DETAIL_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

#: The `fdsn_id` whose events carry ShakeMap products. ShakeMap is a USGS
#: product, so a non-USGS network in the same request is skipped.
COMCAT_PROVIDER = "USGS"

#: The seven intensity measures a ShakeMap raster archive carries.
SHAKEMAP_MEASURES: tuple[str, ...] = (
    "mmi",
    "pga",
    "pgv",
    "psa0p3",
    "psa0p6",
    "psa1p0",
    "psa3p0",
)

#: Every grid in the archive: each measure as a mean and a standard
#: deviation, named exactly as the archive's members are.
SHAKEMAP_LAYERS: tuple[str, ...] = tuple(
    f"{measure}_{statistic}"
    for measure in SHAKEMAP_MEASURES
    for statistic in ("mean", "std")
)

#: Written when the caller asks for no layer explicitly. Macroseismic
#: intensity is the headline shaking field, and one layer per event keeps
#: a multi-event query from writing fourteen rasters and ~8.5 MB apiece.
DEFAULT_SHAKEMAP_LAYERS: tuple[str, ...] = ("mmi_mean",)

#: The grids are lon/lat WGS84 but their `.hdr` headers say no such
#: thing, so the CRS is assigned rather than read.
SHAKEMAP_EPSG = 4326

#: Key of the raster bundle inside a ShakeMap product's `contents` map.
RASTER_CONTENT_KEY = "download/raster.zip"

# The QuakeML resource identifier USGS returns embeds the ComCat id as a
# query parameter, e.g.
# `quakeml:earthquake.usgs.gov/fdsnws/event/1/query?eventid=us6000jlqa&format=quakeml`.
_EVENT_ID_PATTERN = re.compile(r"[?&]eventid=([A-Za-z0-9_.-]+)")


def parse_comcat_id(event_id: str | None) -> str | None:
    """Recover a ComCat event id from a QuakeML resource identifier.

    Args:
        event_id: The value of an event row's `event_id` column — a
            QuakeML resource identifier, or `None` / empty for a row
            whose provider did not supply one.

    Returns:
        The bare ComCat id (e.g. `"us6000jlqa"`), or `None` when the
            identifier carries no `eventid` parameter — which is the
            normal case for a non-USGS network.

    Examples:
        - Pull the id out of a USGS resource identifier:
            ```python
            >>> from earthlens.fdsn._helpers import parse_comcat_id
            >>> parse_comcat_id(
            ...     "quakeml:earthquake.usgs.gov/fdsnws/event/1/query"
            ...     "?eventid=us6000jlqa&format=quakeml"
            ... )
            'us6000jlqa'

            ```
        - An identifier without one yields `None`:
            ```python
            >>> from earthlens.fdsn._helpers import parse_comcat_id
            >>> parse_comcat_id("smi:ch.ethz.sed/sc3a/2024abcd") is None
            True

            ```
    """
    if not event_id:
        return None
    match = _EVENT_ID_PATTERN.search(event_id)
    return match.group(1) if match else None


def normalize_layers(layers: Iterable[str] | None) -> tuple[str, ...]:
    """Validate and de-duplicate a requested set of ShakeMap layers.

    Args:
        layers: Layer names to keep, or `None` for
            `DEFAULT_SHAKEMAP_LAYERS`. Order is preserved; a repeated
            name is collapsed.

    Returns:
        The requested layers as a tuple, in first-seen order.

    Raises:
        ValueError: If a name is not one of `SHAKEMAP_LAYERS`, or if an
            explicitly empty selection is passed — asking for the
            ShakeMap side-output and then for no layer of it is a
            contradiction worth surfacing rather than silently writing
            nothing.

    Examples:
        - The default selection is macroseismic intensity:
            ```python
            >>> from earthlens.fdsn._helpers import normalize_layers
            >>> normalize_layers(None)
            ('mmi_mean',)

            ```
        - An unknown name is refused:
            ```python
            >>> from earthlens.fdsn._helpers import normalize_layers
            >>> normalize_layers(["mmi_median"])  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: unknown ShakeMap layer(s): ['mmi_median']. Choose from [...].

            ```
    """
    if layers is None:
        return DEFAULT_SHAKEMAP_LAYERS
    requested = list(layers)
    if not requested:
        raise ValueError(
            "shakemap_layers is empty. Pass at least one of "
            f"{list(SHAKEMAP_LAYERS)}, or leave it as None for "
            f"{list(DEFAULT_SHAKEMAP_LAYERS)}."
        )
    unknown = [name for name in requested if name not in SHAKEMAP_LAYERS]
    if unknown:
        raise ValueError(
            f"unknown ShakeMap layer(s): {sorted(unknown)}. "
            f"Choose from {list(SHAKEMAP_LAYERS)}."
        )
    seen: dict[str, None] = {}
    for name in requested:
        seen.setdefault(name, None)
    return tuple(seen)


def detail_url(comcat_id: str) -> str:
    """Build the ComCat event-detail URL for one event.

    Args:
        comcat_id: A bare ComCat event id, as returned by
            `parse_comcat_id`.

    Returns:
        The absolute detail URL, asked for as GeoJSON.

    Examples:
        - Address one event's detail document:
            ```python
            >>> from earthlens.fdsn._helpers import detail_url
            >>> detail_url("us6000jlqa").endswith("eventid=us6000jlqa")
            True

            ```
    """
    query = urlencode({"format": "geojson", "eventid": comcat_id})
    return f"{COMCAT_DETAIL_URL}?{query}"


def shakemap_raster_url(detail: Mapping[str, Any]) -> str | None:
    """Find the ShakeMap raster archive's URL in a detail document.

    Args:
        detail: A parsed ComCat event-detail GeoJSON document.

    Returns:
        The `download/raster.zip` URL, or `None` when the event has no
            ShakeMap product or that product ships no raster bundle —
            both of which are ordinary for a small or very recent event.

    Examples:
        - Walk a minimal detail document:
            ```python
            >>> from earthlens.fdsn._helpers import shakemap_raster_url
            >>> shakemap_raster_url(
            ...     {"properties": {"products": {"shakemap": [
            ...         {"contents": {"download/raster.zip":
            ...             {"url": "https://example.invalid/raster.zip"}}}
            ...     ]}}}
            ... )
            'https://example.invalid/raster.zip'

            ```
        - An event with no ShakeMap yields `None`:
            ```python
            >>> from earthlens.fdsn._helpers import shakemap_raster_url
            >>> shakemap_raster_url({"properties": {"products": {}}}) is None
            True

            ```
    """
    properties = detail.get("properties") or {}
    products = properties.get("products") or {}
    entries = products.get("shakemap") or []
    if not entries:
        return None
    contents = (entries[0] or {}).get("contents") or {}
    entry = contents.get(RASTER_CONTENT_KEY) or {}
    return entry.get("url") or None


def extract_layers(
    archive: Path,
    layers: Iterable[str],
    dest_dir: Path,
) -> dict[str, Path]:
    """Extract the `.flt` / `.hdr` pair for each requested layer.

    Only the exact member names built from `layers` are read, so a
    hostile archive cannot place a file outside `dest_dir`.

    Args:
        archive: The downloaded `raster.zip`.
        layers: Layer names, already validated by `normalize_layers`.
        dest_dir: Directory the pairs are written into; created if absent.

    Returns:
        A mapping of layer name to the extracted `.flt` path, holding
            only the layers the archive actually carried.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as bundle:
        available = set(bundle.namelist())
        for layer in layers:
            members = (f"{layer}.flt", f"{layer}.hdr")
            if not available.issuperset(members):
                logger.warning(
                    f"ShakeMap archive {archive.name} carries no {layer!r} grid "
                    f"(expected {members[0]} + {members[1]}) — skipping it."
                )
                continue
            for member in members:
                (dest_dir / member).write_bytes(bundle.read(member))
            extracted[layer] = dest_dir / members[0]
    return extracted


def flt_to_geotiff(flt_path: Path, dest: Path) -> Path:
    """Convert one ESRI float grid into a georeferenced GeoTIFF.

    The `.hdr` sibling must sit next to `flt_path` — GDAL's `EHdr`
    driver reads the two together. The header defines the grid's origin,
    cell size, and nodata value but no projection, so `EPSG:4326` is
    assigned before writing.

    Args:
        flt_path: The extracted `.flt` payload.
        dest: Destination GeoTIFF path; parents are created.

    Returns:
        `dest`, for chaining.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    dataset = Dataset.read_file(str(flt_path), read_only=False)
    dataset.set_crs(epsg=SHAKEMAP_EPSG)
    dataset.to_file(str(dest))
    return dest
