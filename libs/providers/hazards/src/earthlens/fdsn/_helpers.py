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

import json
import os
import re
import shutil
import zipfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
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
#: a multi-event query from writing fourteen rasters and several megabytes apiece.
DEFAULT_SHAKEMAP_LAYERS: tuple[str, ...] = ("mmi_mean",)

#: The grids are lon/lat WGS84 but their `.hdr` headers say no such
#: thing, so the CRS is assigned rather than read.
SHAKEMAP_EPSG = 4326

#: Key of the raster bundle inside a ShakeMap product's `contents` map.
RASTER_CONTENT_KEY = "download/raster.zip"

#: Name of the per-event manifest recording what its archive yielded.
MANIFEST_NAME = ".shakemap.json"

#: Layout version of the manifest payload. A manifest written by a different
#: version is treated as absent rather than guessed at, so the event is simply
#: refetched — which is always safe, if occasionally wasteful.
MANIFEST_SCHEMA = 1

#: Ceiling on a single decompressed archive member. A real ShakeMap grid is
#: well under a megabyte (a 30 arc-second raster over one event's footprint,
#: ~810 KB for the 2023 Kahramanmaras event), so 64 MB leaves two orders of
#: magnitude of headroom for legitimate data while refusing an archive whose
#: declared expansion is absurd, before any of it is written.
MAX_MEMBER_BYTES = 64 * 1024 * 1024

# The QuakeML resource identifier USGS returns embeds the ComCat id as a
# query parameter, e.g.
# `quakeml:earthquake.usgs.gov/fdsnws/event/1/query?eventid=us6000jlqa&format=quakeml`.
#
# The captured id becomes a directory name, so the character class deliberately
# excludes `.`: a capture of `.` or `..` would resolve the event directory to
# somewhere the caller never asked for. Real ComCat ids are a network code plus
# alphanumerics (`us6000jlqa`, `nc73872510`, `official20110311054624120_30`) and
# never contain a dot, so excluding it costs nothing.
# Length-bounded as well: the id becomes a directory name, and a pathological
# identifier should be refused rather than handed to the filesystem. Real ComCat
# ids are well under 40 characters (`official20110311054624120_30` is 28).
#
# The capture must be terminated by `&` or the end of the string, so a value
# carrying anything outside the character class is refused outright rather than
# silently truncated to its leading run of legal characters.
_EVENT_ID_PATTERN = re.compile(r"[?&]eventid=([A-Za-z0-9_-]{1,64})(?:&|$)")


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
        - A degenerate id that would escape the output directory is refused:
            ```python
            >>> from earthlens.fdsn._helpers import parse_comcat_id
            >>> parse_comcat_id("quakeml:x?eventid=..&format=quakeml") is None
            True

            ```

    See Also:
        detail_url: Builds the request URL from the id returned here.
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
        TypeError: If a bare string is passed. A string is iterable, so
            it would otherwise be read one character at a time.
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

    See Also:
        SHAKEMAP_LAYERS: The fourteen names accepted here.
        DEFAULT_SHAKEMAP_LAYERS: What `None` resolves to.
    """
    if layers is None:
        return DEFAULT_SHAKEMAP_LAYERS
    if isinstance(layers, str):
        raise TypeError(
            f"shakemap_layers must be a sequence of layer names, not the bare "
            f"string {layers!r} — pass [{layers!r}] for a single layer."
        )
    requested = list(layers)
    if not requested:
        raise ValueError(
            "shakemap_layers is empty. Pass at least one of "
            f"{list(SHAKEMAP_LAYERS)}, or leave it as None for "
            f"{list(DEFAULT_SHAKEMAP_LAYERS)}."
        )
    unknown = [name for name in requested if name not in SHAKEMAP_LAYERS]
    if unknown:
        # Rendered by repr and sorted as text: a mixed-type list would make a
        # plain `sorted` raise TypeError, hiding the layer-name error behind an
        # unrelated one.
        rendered = sorted(repr(name) for name in unknown)
        raise ValueError(
            f"unknown ShakeMap layer(s): [{', '.join(rendered)}]. "
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
            >>> detail_url("us6000jlqa")
            'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&eventid=us6000jlqa'

            ```
        - Chain it onto an id recovered from an event row:
            ```python
            >>> from earthlens.fdsn._helpers import detail_url, parse_comcat_id
            >>> row_id = (
            ...     "quakeml:earthquake.usgs.gov/fdsnws/event/1/query"
            ...     "?eventid=nc73872510&format=quakeml"
            ... )
            >>> detail_url(parse_comcat_id(row_id)).split("eventid=")[1]
            'nc73872510'

            ```

    See Also:
        parse_comcat_id: Recovers the id this URL is built from.
        shakemap_raster_url: Walks the document this URL returns.
    """
    query = urlencode({"format": "geojson", "eventid": comcat_id})
    return f"{COMCAT_DETAIL_URL}?{query}"


def shakemap_raster_url(detail: Mapping[str, Any]) -> str | None:
    """Find the ShakeMap raster archive's URL in a detail document.

    Args:
        detail: A parsed ComCat event-detail GeoJSON document.

    Returns:
        The `download/raster.zip` URL, or `None` when the event has no
            ShakeMap product, that product ships no raster bundle — both
            ordinary for a small or very recent event — or the URL is not
            `https`. The value comes from an upstream document and is
            handed straight to the downloader, so any other scheme is
            refused rather than fetched.

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

    See Also:
        detail_url: Addresses the document this walks.
        RASTER_CONTENT_KEY: The `contents` key looked up here.
    """
    properties = detail.get("properties") or {}
    products = properties.get("products") or {}
    entries = products.get("shakemap") or []
    if not entries:
        return None
    contents = (entries[0] or {}).get("contents") or {}
    entry = contents.get(RASTER_CONTENT_KEY) or {}
    url = entry.get("url") or None
    if url is not None and not str(url).lower().startswith("https://"):
        # The URL comes from an upstream document and is handed straight to the
        # downloader; anything but https is refused rather than fetched.
        logger.warning(f"refusing non-https ShakeMap archive URL {url!r}.")
        return None
    return url


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

    Examples:
        - Pull one layer out of a two-layer archive:
            ```python
            >>> import shutil, tempfile, zipfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import extract_layers
            >>> workspace = Path(tempfile.mkdtemp())
            >>> archive = workspace / "raster.zip"
            >>> with zipfile.ZipFile(archive, "w") as bundle:
            ...     for layer in ("mmi_mean", "pga_mean"):
            ...         bundle.writestr(f"{layer}.flt", bytes(4))
            ...         bundle.writestr(f"{layer}.hdr", "NROWS 1")
            >>> found = extract_layers(archive, ["mmi_mean"], workspace / "out")
            >>> sorted(found)
            ['mmi_mean']
            >>> found["mmi_mean"].name
            'mmi_mean.flt'
            >>> (workspace / "out" / "mmi_mean.hdr").is_file()
            True
            >>> shutil.rmtree(workspace)

            ```
        - A layer the archive lacks is skipped rather than raised:
            ```python
            >>> import shutil, tempfile, zipfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import extract_layers
            >>> workspace = Path(tempfile.mkdtemp())
            >>> archive = workspace / "raster.zip"
            >>> with zipfile.ZipFile(archive, "w") as bundle:
            ...     bundle.writestr("mmi_mean.flt", bytes(4))
            ...     bundle.writestr("mmi_mean.hdr", "NROWS 1")
            >>> sorted(extract_layers(archive, ["mmi_mean", "pgv_std"], workspace / "out"))
            ['mmi_mean']
            >>> shutil.rmtree(workspace)

            ```

    See Also:
        normalize_layers: Validates the layer names passed here.
        flt_to_geotiff: Converts an extracted `.flt` into a GeoTIFF.
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
            oversized = [
                name
                for name in members
                if bundle.getinfo(name).file_size > MAX_MEMBER_BYTES
            ]
            if oversized:
                logger.warning(
                    f"ShakeMap archive {archive.name} declares {oversized} larger "
                    f"than {MAX_MEMBER_BYTES} bytes — refusing {layer!r}."
                )
                continue
            for member in members:
                with (
                    bundle.open(member) as source,
                    (dest_dir / member).open("wb") as target,
                ):
                    shutil.copyfileobj(source, target)
            extracted[layer] = dest_dir / members[0]
    return extracted


def flt_to_geotiff(flt_path: Path, dest: Path) -> Path:
    """Convert one ESRI float grid into a georeferenced GeoTIFF.

    The `.hdr` sibling must sit next to `flt_path` — GDAL's `EHdr`
    driver reads the two together. The header defines the grid's origin,
    cell size, and nodata value but no projection, so `EPSG:4326` is
    assigned before writing.

    Assigning it means opening the source read-write, and the `EHdr`
    driver persists a projection by dropping a `<basename>.prj` beside
    the grid. That mutation is deliberate but confined: callers pass a
    grid they extracted into a scratch directory, which is discarded
    wholesale, so nothing GDAL writes alongside the source reaches the
    caller's output.

    Args:
        flt_path: The extracted `.flt` payload.
        dest: Destination GeoTIFF path; parents are created.

    Returns:
        `dest`, for chaining.

    Examples:
        - Convert a small grid and read back its georeferencing:
            ```python
            >>> import shutil, struct, tempfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import flt_to_geotiff
            >>> workspace = Path(tempfile.mkdtemp())
            >>> header = [
            ...     "BYTEORDER  LSBFIRST", "LAYOUT  BIL", "NROWS  2", "NCOLS  2",
            ...     "NBANDS  1", "NBITS  32", "BANDROWBYTES  8", "TOTALROWBYTES  8",
            ...     "PIXELTYPE  FLOAT", "ULXMAP  35.0", "ULYMAP  39.0",
            ...     "XDIM  0.5", "YDIM  0.5", "NODATA  999.0",
            ... ]
            >>> _ = (workspace / "mmi_mean.hdr").write_text(chr(10).join(header))
            >>> _ = (workspace / "mmi_mean.flt").write_bytes(
            ...     struct.pack("<4f", 5.0, 6.0, 7.0, 8.0)
            ... )
            >>> written = flt_to_geotiff(
            ...     workspace / "mmi_mean.flt", workspace / "mmi_mean.tif"
            ... )
            >>> written.name
            'mmi_mean.tif'
            >>> from pyramids.dataset import Dataset
            >>> converted = Dataset.read_file(str(written))
            >>> converted.epsg
            4326
            >>> converted.rows, converted.columns
            (2, 2)
            >>> del converted
            >>> shutil.rmtree(workspace)

            ```

    See Also:
        extract_layers: Produces the `.flt` / `.hdr` pair this reads.
        SHAKEMAP_EPSG: The CRS assigned during the conversion.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Written to a sibling and renamed on success. `Dataset.to_file` writes
    # straight to the path it is given, so without this a run interrupted
    # mid-write (Ctrl-C, a full disk) would leave a non-empty but unreadable
    # `.tif` — and the caller's skip-if-present check, which can only ask
    # whether a file exists and is non-empty, would honour it forever. The
    # staged name keeps the `.tif` suffix so GDAL still infers the driver.
    # Staged beside the *source* grid — which the caller keeps in a scratch
    # directory — rather than beside the destination, so an interrupted run
    # leaves nothing at all in the user's output folder.
    staged = flt_path.parent / f".{dest.stem}.partial.tif"
    try:
        # The context manager, not a trailing `del`: on failure the exception's
        # traceback keeps this frame — and any local still bound to the dataset
        # — alive, so GDAL's handle on the grid outlives the call. Windows then
        # refuses to unlink the file and the caller's scratch directory survives
        # inside the user's output. `__exit__` closes it on both paths.
        with Dataset.read_file(str(flt_path), read_only=False) as dataset:
            dataset.set_crs(epsg=SHAKEMAP_EPSG)
            dataset.to_file(str(staged))
        if not staged.is_file():
            raise RuntimeError(
                f"converting {flt_path.name} produced no output at {staged} — "
                "refusing to publish a missing raster."
            )
        staged.replace(dest)
    finally:
        # Only ever removes the staged name, and never at the cost of the real
        # error: a failed cleanup here would otherwise replace the exception
        # that explains why the conversion failed.
        with suppress(OSError):
            staged.unlink(missing_ok=True)
    return dest


def read_manifest(dest_dir: Path, quiet: bool = False) -> dict[str, Any] | None:
    """Read an event directory's ShakeMap manifest, if it has one.

    Args:
        dest_dir: The event's output directory.
        quiet: Suppress the warning a malformed or unreadable manifest
            normally logs. Set by `write_manifest`, which reads the old
            payload only to merge it and is about to replace it anyway.

    Returns:
        The parsed manifest, or `None` when the event has never been
            fetched, its manifest is unreadable, or the payload does not
            match `MANIFEST_SCHEMA` — every one of which means "fetch it
            again". A structurally wrong manifest is reported and treated
            as absent rather than raising, so the next write repairs it.

    Examples:
        - A directory with no manifest reads as `None`:
            ```python
            >>> import tempfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import read_manifest
            >>> read_manifest(Path(tempfile.mkdtemp())) is None
            True

            ```
        - A written manifest round-trips:
            ```python
            >>> import shutil, tempfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import read_manifest, write_manifest
            >>> workspace = Path(tempfile.mkdtemp())
            >>> write_manifest(workspace, ["mmi_mean", "pga_mean"], ["mmi_mean"])
            >>> read_manifest(workspace)["produced"]
            ['mmi_mean']
            >>> shutil.rmtree(workspace)

            ```
        - A manifest from a different layout version reads as absent, so the
          event is refetched rather than half-understood:
            ```python
            >>> import json, shutil, tempfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import MANIFEST_NAME, read_manifest
            >>> workspace = Path(tempfile.mkdtemp())
            >>> _ = (workspace / MANIFEST_NAME).write_text(
            ...     json.dumps({"schema": 99, "requested": [], "produced": [],
            ...                 "checked": 0.0})
            ... )
            >>> read_manifest(workspace) is None
            True
            >>> shutil.rmtree(workspace)

            ```

    See Also:
        write_manifest: Produces the file read here.
        MANIFEST_SCHEMA: The layout version this accepts.
    """
    path = dest_dir / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if not quiet:
            logger.warning(f"unreadable ShakeMap manifest at {path} — refetching.")
        return None
    if not _is_valid_manifest(loaded):
        # Structurally wrong, not merely unreadable: treated as absent so the
        # event refetches and the next write repairs the file, rather than
        # letting a bad type escape as an error from somewhere downstream.
        if not quiet:
            logger.warning(f"malformed ShakeMap manifest at {path} — refetching.")
        return None
    validated: dict[str, Any] = loaded
    return validated


def _is_valid_manifest(loaded: object) -> bool:
    """Report whether a parsed manifest has the shape this version writes.

    Args:
        loaded: The object `json.loads` produced from the manifest file.

    Returns:
        `True` when every field is present with the expected type and the
            schema version matches, `False` otherwise.
    """
    if not isinstance(loaded, dict):
        return False
    if loaded.get("schema") != MANIFEST_SCHEMA:
        return False
    for key in ("requested", "produced"):
        value = loaded.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            return False
    version = loaded.get("product_version")
    if version is not None and not isinstance(version, str):
        return False
    checked = loaded.get("checked")
    return isinstance(checked, (int, float)) and not isinstance(checked, bool)


def write_manifest(
    dest_dir: Path,
    requested: Iterable[str],
    produced: Iterable[str],
    checked: float = 0.0,
    product_version: str | None = None,
) -> None:
    """Record what one event's archive actually yielded.

    Without this, a rerun cannot tell an event it has never fetched from
    one whose archive simply does not carry a requested layer. The
    skip-if-present check compares against the layers that *exist*, so an
    archive permanently missing a grid stops being re-downloaded on every
    run.

    Args:
        dest_dir: The event's output directory; created if absent.
        requested: The layers this call asked for. Recorded so a later
            call asking for *more* layers refetches rather than reusing a
            narrower result.
        produced: The layers the archive actually carried. Empty when the
            event publishes no ShakeMap at all, which is itself worth
            recording so the detail request is not repeated. Merged with
            anything already recorded rather than replacing it.
        checked: POSIX timestamp of this check, used to age out a negative
            result. `0.0` (the default) reads as "long ago", so an entry
            written without one is re-checked.
        product_version: The ShakeMap product's `updateTime`, when the detail
            document supplied one. Recorded so a later version can tell a
            revised grid from the one on disk; nothing reads it yet.

    Returns:
        None: The manifest is written to `dest_dir` as a side effect.

    Examples:
        - Record a fetch that produced one of the two requested grids:
            ```python
            >>> import shutil, tempfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import read_manifest, write_manifest
            >>> workspace = Path(tempfile.mkdtemp())
            >>> write_manifest(workspace, ["mmi_mean", "pga_mean"], ["mmi_mean"])
            >>> manifest = read_manifest(workspace)
            >>> manifest["requested"]
            ['mmi_mean', 'pga_mean']
            >>> manifest["produced"]
            ['mmi_mean']
            >>> shutil.rmtree(workspace)

            ```
        - Record an event that publishes no ShakeMap, so the next run can
          skip it without re-requesting its detail document:
            ```python
            >>> import shutil, tempfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import read_manifest, write_manifest
            >>> workspace = Path(tempfile.mkdtemp())
            >>> write_manifest(workspace, ["mmi_mean"], [])
            >>> read_manifest(workspace)["produced"]
            []
            >>> shutil.rmtree(workspace)

            ```
        - The event directory is created if it does not exist yet:
            ```python
            >>> import shutil, tempfile
            >>> from pathlib import Path
            >>> from earthlens.fdsn._helpers import MANIFEST_NAME, write_manifest
            >>> workspace = Path(tempfile.mkdtemp())
            >>> event_dir = workspace / "us6000jlqa"
            >>> write_manifest(event_dir, ["mmi_mean"], ["mmi_mean"])
            >>> (event_dir / MANIFEST_NAME).is_file()
            True
            >>> shutil.rmtree(workspace)

            ```

    See Also:
        read_manifest: Reads back what this records.
        MANIFEST_NAME: The filename written inside `dest_dir`.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Merged with whatever is already recorded, never replaced. Narrowing the
    # requested layers on a later run would otherwise drop the record of
    # rasters still sitting on disk, so the next widening run would refetch an
    # archive it already has.
    # Read quietly: a malformed file being repaired by this very write should
    # not log "refetching", which describes what a *reader* would do.
    existing = read_manifest(dest_dir, quiet=True) or {}
    payload = {
        "schema": MANIFEST_SCHEMA,
        "requested": sorted(set(existing.get("requested", [])) | set(requested)),
        "produced": sorted(set(existing.get("produced", [])) | set(produced)),
        "checked": checked,
        "product_version": product_version,
    }
    # Staged and renamed, like the rasters it describes: a manifest truncated
    # by an interrupted write would otherwise be read back as malformed.
    # Process-unique, like the scratch directory: two runs staging the same
    # name would otherwise rename each other's half-written file into place.
    staged = dest_dir / f".{MANIFEST_NAME}.{os.getpid()}.partial"
    try:
        staged.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        staged.replace(dest_dir / MANIFEST_NAME)
    finally:
        with suppress(OSError):
            staged.unlink(missing_ok=True)


def shakemap_product_version(detail: Mapping[str, Any]) -> str | None:
    """Return the ShakeMap product's `updateTime`, when the document has one.

    ComCat republishes ShakeMaps — the 2023 Kahramanmaras grid is on its
    twelfth version, last updated in 2025 — so the value is recorded
    alongside a fetch to give a later version something to compare against.

    Args:
        detail: A parsed ComCat event-detail GeoJSON document.

    Returns:
        The `updateTime` as a string, or `None` when the event has no
            ShakeMap product or the product omits it.

    Examples:
        - Read the version off a minimal detail document:
            ```python
            >>> from earthlens.fdsn._helpers import shakemap_product_version
            >>> shakemap_product_version(
            ...     {"properties": {"products": {"shakemap": [
            ...         {"updateTime": 1756575631263}
            ...     ]}}}
            ... )
            '1756575631263'

            ```
        - An event with no ShakeMap yields `None`:
            ```python
            >>> from earthlens.fdsn._helpers import shakemap_product_version
            >>> shakemap_product_version({"properties": {"products": {}}}) is None
            True

            ```

    See Also:
        write_manifest: Records the value returned here.
    """
    properties = detail.get("properties") or {}
    products = properties.get("products") or {}
    entries = products.get("shakemap") or []
    if not entries:
        return None
    update_time = (entries[0] or {}).get("updateTime")
    return None if update_time is None else str(update_time)
