"""AOI-sidecar cache primitives for download-and-localise raster backends.

A backend that writes one output file per request whose *filename does not
encode the AOI* (e.g. `fabdem_V1-2.tif`, `efhm_RP100.tif`) cannot tell "does the
cached file hold the AOI this request wants?" from a bare `Path.exists()` check:
a previous AOI's raster would be returned for a new bounding box written to the
same output path. These helpers record the AOI a cached file was produced for in
a `<target>.aoi` sidecar and compare it on the next request, so the
skip-if-exists fast path fires only for a genuine match.

The tag is bbox-plus-polygon: a backend that supports a polygon `aoi=` can be
handed two requests with the same bounding box but different polygon masks, so
the polygon geometry (when present) is hashed into the tag to keep their cached
crops distinct. Shared by the `fabdem` and `jrc_flood` backends (issue #972).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from earthlens.base.abstractdatasource import SpatialExtent

#: Suffix appended to an output path to form its AOI sidecar (`<target>.aoi`).
AOI_SIDECAR_SUFFIX = ".aoi"


def aoi_tag(space: SpatialExtent) -> str:
    """Return a stable cache key for a spatial extent (bbox plus any polygon).

    The bounding box alone is not a sufficient key: a backend that supports a
    polygon `aoi=` can receive two requests with the *same* bounding box but
    different polygon masks, whose cropped outputs differ. When the extent
    carries a polygon geometry it is hashed into the key so those requests get
    distinct tags.

    Args:
        space: The request's spatial extent, exposing `west` / `south` /
            `east` / `north` and an optional `geometry`.

    Returns:
        A `"west,south,east,north"` string, with `|<sha256>` appended when the
        extent carries a polygon geometry.
    """
    tag = f"{space.west},{space.south},{space.east},{space.north}"
    geometry = getattr(space, "geometry", None)
    if geometry is not None:
        # `space.geometry` is a geopandas GeoDataFrame (from the facade's
        # `aoi=`), so serialise it to GeoJSON; fall back to a shapely `.wkt`.
        if hasattr(geometry, "to_json"):
            key = geometry.to_json()
        else:
            key = getattr(geometry, "wkt", str(geometry))
        tag += "|" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    return tag


def sidecar_path(target: Path) -> Path:
    """Return the `<target>.aoi` sidecar path for an output file."""
    return target.with_suffix(target.suffix + AOI_SIDECAR_SUFFIX)


def sidecar_is_fresh(target: Path, tag: str) -> bool:
    """Whether `target` exists and its sidecar records the AOI `tag`.

    Args:
        target: The candidate cached output file.
        tag: The current request's AOI tag (from `aoi_tag`).

    Returns:
        `True` when both the output and its sidecar exist and the sidecar's
        recorded tag matches `tag` — i.e. the cached file holds this exact AOI.
        A `force` re-fetch is the caller's concern and is not checked here.
    """
    sidecar = sidecar_path(target)
    return (
        target.exists()
        and sidecar.exists()
        and sidecar.read_text(encoding="utf-8").strip() == tag
    )


def write_sidecar(target: Path, tag: str) -> None:
    """Record the AOI `tag` that `target` was written for, in its sidecar."""
    sidecar_path(target).write_text(tag, encoding="utf-8")
