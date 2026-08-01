"""Zenodo resolution and archive access for the Caravan backend.

Everything between "a catalog row" and "the bytes of one catchment's timeseries"
lives here, in two transports that the rest of the backend never has to tell
apart:

* **ZIP over HTTP Range (the fast path).** A ZIP stores its central directory at
  the tail, so :class:`earthlens.base.HttpRangeFile` turns a multi-gigabyte
  Zenodo archive into random-access storage. Indexing the 8.84 GB GRDC archive
  costs 4 requests / 0.8 MB, and one catchment a further 2 requests / 2.1 MB —
  nothing is written to disk. Every community extension, and base ≤ 1.2, is a
  ZIP.
* **tar.gz downloaded whole (the fallback).** A `.tar.gz` is a single gzip
  stream with no index, so it cannot be seeked: the archive is fetched to the
  cache, md5-verified, and members are pulled out by streaming scans. Only base
  ≥ 1.4 needs this, and it is why that row is opt-in.

Member paths are always resolved from the archive's own listing, never composed
from a template: the directory every member sits under differs per record
(`Caravan/`, `GRDC_Caravan_extension_csv/`, `Caravan_extension_Israel_Ver4/`)
and is absent entirely in the Denmark and Germany archives.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import platformdirs
from loguru import logger

from earthlens.base.http import HttpClient, HttpRangeFile

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

    from earthlens.caravan.catalog import ArchiveFile

#: Zenodo's REST endpoint for one record's metadata.
RECORD_URL = "https://zenodo.org/api/records/{record}"

#: A timeseries member, in any archive layout. The leading `(?:^|/)` is what
#: makes the pattern prefix-agnostic, so it matches both
#: `timeseries/csv/camelsdk/camelsdk_100006.csv` (Denmark, no root directory)
#: and `GRDC_Caravan_extension_csv/timeseries/csv/grdc/GRDC_1159100.csv`.
_TIMESERIES_RE = re.compile(
    r"(?:^|/)timeseries/(?P<fmt>csv|netcdf)/(?P<source>[^/]+)/(?P<gauge>[^/]+)\.(?:csv|nc)$"
)

#: An attribute table, e.g. `attributes/grdc/attributes_other_grdc.csv`.
_ATTRIBUTE_RE = re.compile(
    r"(?:^|/)attributes/(?P<source>[^/]+)/attributes_(?P<kind>[^/]+?)_(?P=source)\.csv$"
)

#: A basin-shapefile sidecar. All five extensions are needed together — GDAL
#: cannot open a `.shp` without at least its `.shx` and `.dbf`.
_SHAPEFILE_RE = re.compile(
    r"(?:^|/)shapefiles/(?P<source>[^/]+)/(?P<stem>[^/]+)\.(?P<ext>shp|shx|dbf|prj|cpg)$"
)

#: Read size for hashing and streamed copies — 1 MiB.
_BLOCK = 1 << 20


def cache_dir() -> Path:
    """Return the directory Caravan archives and indexes are cached in.

    Honours `EARTHLENS_CACHE` first (the override the cmip6 resolver already
    reads), then falls back to the per-platform user cache directory the nwp
    backend uses. A hard-coded `~/.cache` would be wrong on Windows, which is
    where this is developed.

    Returns:
        Path: `<EARTHLENS_CACHE>/caravan`, else
            `platformdirs.user_cache_dir("earthlens")/caravan`.
    """
    base = os.environ.get("EARTHLENS_CACHE")
    root = Path(base) if base else Path(platformdirs.user_cache_dir("earthlens"))
    return root / "caravan"


@dataclass(frozen=True)
class ZenodoFile:
    """One file published on a Zenodo record.

    Attributes:
        name: The file name on the record.
        url: The REST content URL the bytes are served from.
        size: Size in bytes.
        md5: The md5 checksum, with Zenodo's `md5:` prefix stripped.
    """

    name: str
    url: str
    size: int
    md5: str


def resolve_record(
    record: int | str, *, client: HttpClient | None = None
) -> dict[str, ZenodoFile]:
    """Read a Zenodo record's file listing.

    Used by the refresh tool and by any caller that wants to confirm a pinned
    catalog row still matches what Zenodo serves. The backend itself composes
    file URLs from the catalog instead, so a normal download needs no metadata
    round trip at all.

    Args:
        record: The Zenodo **version** record id.
        client: Transport to read through; a default :class:`HttpClient` when
            `None`.

    Returns:
        dict[str, ZenodoFile]: The record's files, keyed by file name.

    Raises:
        requests.HTTPError: If the record cannot be read.
    """
    client = client if client is not None else HttpClient()
    payload = client.get_json(RECORD_URL.format(record=record))
    files: dict[str, ZenodoFile] = {}
    for entry in payload.get("files") or []:
        checksum = str(entry.get("checksum") or "")
        files[entry["key"]] = ZenodoFile(
            name=entry["key"],
            url=(entry.get("links") or {}).get("self", ""),
            size=int(entry.get("size") or 0),
            # Zenodo reports `md5:<hex>`; callers want the bare digest so it
            # compares directly against `hashlib.md5().hexdigest()`.
            md5=checksum.removeprefix("md5:"),
        )
    return files


def _assert_safe_member(name: str, dest_dir: Path) -> None:
    """Reject a member name that would extract outside `dest_dir`.

    Mirrors the guard in :mod:`earthlens.base.archive`. `tarfile`'s own
    `filter="data"` also refuses escaping paths, but only on Python 3.11.4+, and
    the repo floor is 3.11.0 — so the check is made explicitly rather than
    assumed.

    Args:
        name: The archive member name.
        dest_dir: The directory members are extracted into.

    Raises:
        ValueError: If the member resolves outside `dest_dir`.
    """
    base = dest_dir.resolve()
    target = (dest_dir / name).resolve()
    if target != base and base not in target.parents:
        raise ValueError(
            f"refusing to extract unsafe path {name!r} from the archive "
            f"(escapes {dest_dir})."
        )


def _file_md5(path: Path) -> str:
    """Return the md5 hex digest of a file, read in blocks.

    Args:
        path: The file to hash.

    Returns:
        str: The lowercase hex digest.
    """
    digest = hashlib.md5()  # noqa: S324 - Zenodo publishes md5, not a secure hash
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_archive(
    archive: ArchiveFile,
    *,
    cache_root: Path | None = None,
    client: HttpClient | None = None,
    progress: bool = True,
) -> Path:
    """Fetch a non-seekable archive to the cache, verifying its checksum.

    The `tar.gz` fallback only — a ZIP is read in place and never lands here.
    An already-cached copy whose md5 matches the catalog is reused, so the
    24–29 GB base download happens at most once; a mismatch re-fetches rather
    than trusting a truncated or superseded file.

    Args:
        archive: The catalog's file descriptor, carrying the URL, size and md5.
        cache_root: Cache directory; :func:`cache_dir` when `None`.
        client: Transport to download through.
        progress: Show a progress bar for the download.

    Returns:
        Path: The verified local archive.

    Raises:
        ValueError: If the freshly downloaded file's md5 does not match the
            catalog — the archive is not what the pinned record promised.
    """
    root = cache_root if cache_root is not None else cache_dir()
    destination = root / str(archive.record) / archive.name
    if destination.exists() and _file_md5(destination) == archive.md5:
        logger.debug(f"caravan: reusing cached archive {destination}")
        return destination

    logger.warning(
        f"caravan: downloading {archive.name} ({archive.size / 1e9:.1f} GB) - "
        f"this archive is a single gzip stream and cannot be read in place."
    )
    client = client if client is not None else HttpClient()
    client.download(archive.url, destination, progress=progress)
    actual = _file_md5(destination)
    if actual != archive.md5:
        raise ValueError(
            f"{archive.name} failed its checksum: expected md5 {archive.md5}, "
            f"got {actual}. The download is corrupt or the catalog is stale."
        )
    return destination


def extract_tar_members(
    tarball: Path, members: set[str], dest_dir: Path
) -> dict[str, Path]:
    """Extract named members from a `.tar.gz` in a single streaming pass.

    A gzip stream can only be read forwards, so every member is matched during
    one sequential scan rather than seeking per member — extracting three
    catchments must not decompress the archive three times. `extractall` is
    never used: an archive this size would explode the cache, and the member
    names are not ours to trust.

    Args:
        tarball: The local `.tar.gz`.
        members: Member names to extract.
        dest_dir: Directory to extract into (created if absent).

    Returns:
        dict[str, Path]: Member name to the extracted file, for the members
            that were found. A member absent from the archive is simply missing
            from the mapping — the caller decides whether that is an error.

    Raises:
        ValueError: If a member name would escape `dest_dir`.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    if not members:
        return extracted
    wanted = set(members)
    with tarfile.open(tarball, mode="r|gz") as archive:
        for entry in archive:
            if entry.name not in wanted or not entry.isfile():
                continue
            _assert_safe_member(entry.name, dest_dir)
            target = dest_dir / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(entry)
            if source is None:  # pragma: no cover - isfile() already excludes this
                continue
            with target.open("wb") as handle:
                for block in iter(lambda: source.read(_BLOCK), b""):  # noqa: B023
                    handle.write(block)
            extracted[entry.name] = target
            wanted.discard(entry.name)
            if not wanted:
                break
    return extracted


def _tar_member_names(tarball: Path) -> list[str]:
    """List a tarball's members, caching the listing beside the archive.

    Listing a `.tar.gz` decompresses the whole stream, which for base means
    ~29 GB of work. The result is written to `<tarball>.index.json` so the scan
    happens once per cached archive rather than once per request.

    Args:
        tarball: The local `.tar.gz`.

    Returns:
        list[str]: Every member name in the archive.
    """
    index_path = tarball.with_suffix(tarball.suffix + ".index.json")
    if index_path.exists():
        return list(json.loads(index_path.read_text(encoding="utf-8")))
    logger.warning(
        f"caravan: indexing {tarball.name} - a gzip stream has no directory, so "
        f"this decompresses the whole archive once. The listing is then cached."
    )
    with tarfile.open(tarball, mode="r|gz") as archive:
        names = [entry.name for entry in archive if entry.isfile()]
    index_path.write_text(json.dumps(names), encoding="utf-8")
    return names


@dataclass
class CaravanArchive:
    """Uniform member access over a Caravan archive, remote ZIP or local tar.

    Built by :meth:`open_remote_zip` or :meth:`open_local_tar`. The backend
    talks only to this object, so the two very different transports — random
    access over HTTP versus sequential scans of a downloaded file — do not leak
    into the request logic.

    Attributes:
        members: Every file member name in the archive.
        label: A short human-readable name, used in log and error messages.
    """

    members: tuple[str, ...]
    label: str
    _zip: zipfile.ZipFile | None = None
    _tarball: Path | None = None
    _extract_dir: Path | None = None
    _range_file: HttpRangeFile | None = None
    _timeseries: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Index the timeseries members by `(format, source, gauge_id)`."""
        for name in self.members:
            match = _TIMESERIES_RE.search(name)
            if match is not None:
                key = (match["fmt"], match["source"], match["gauge"])
                self._timeseries[key] = name

    @classmethod
    def open_remote_zip(
        cls,
        url: str,
        *,
        client: HttpClient | None = None,
        size: int | None = None,
        label: str = "",
    ) -> CaravanArchive:
        """Open a remote ZIP over HTTP Range, reading only its directory.

        Args:
            url: The archive's content URL.
            client: Transport to read through.
            size: The archive size when already known from the catalog, saving
                the `HEAD` probe.
            label: Short name for log messages; the URL's last segment when
                empty.

        Returns:
            CaravanArchive: The opened archive. Nothing is written to disk.
        """
        handle = HttpRangeFile(url, client=client, size=size)
        zip_file = zipfile.ZipFile(handle.buffered())
        names = tuple(n for n in zip_file.namelist() if not n.endswith("/"))
        logger.debug(
            f"caravan: indexed {len(names)} members of {label or url} in "
            f"{handle.request_count} range requests "
            f"({handle.bytes_read / 1e6:.2f} MB)"
        )
        return cls(
            members=names,
            label=label or url.rsplit("/", 2)[-2],
            _zip=zip_file,
            _range_file=handle,
        )

    @classmethod
    def open_local_tar(
        cls, tarball: Path, *, extract_dir: Path | None = None, label: str = ""
    ) -> CaravanArchive:
        """Open a downloaded `.tar.gz`, using (and caching) its member listing.

        Args:
            tarball: The local archive, already verified by
                :func:`ensure_archive`.
            extract_dir: Directory members are extracted into; a `members`
                sibling of the tarball when `None`.
            label: Short name for log messages; the file name when empty.

        Returns:
            CaravanArchive: The opened archive.
        """
        names = tuple(_tar_member_names(tarball))
        return cls(
            members=names,
            label=label or tarball.name,
            _tarball=tarball,
            _extract_dir=extract_dir or tarball.parent / "members",
        )

    @property
    def transfer_stats(self) -> tuple[int, float]:
        """Requests issued and megabytes transferred, for the ZIP path.

        Returns:
            tuple[int, float]: `(request_count, megabytes)`; `(0, 0.0)` for a
                local tar, which transfers nothing at read time.
        """
        if self._range_file is None:
            return (0, 0.0)
        return (self._range_file.request_count, self._range_file.bytes_read / 1e6)

    @property
    def sources(self) -> list[str]:
        """The source dataset directories present in this archive, sorted.

        Returns:
            list[str]: e.g. `["grdc"]`, or base's seven sources.
        """
        return sorted({source for _, source, _ in self._timeseries})

    def gauge_ids(self, source: str, timeseries_format: str = "csv") -> list[str]:
        """List the catchments this archive holds for one source.

        Args:
            source: The source directory name (`"grdc"`, `"camelsdk"`).
            timeseries_format: `"csv"` or `"netcdf"`.

        Returns:
            list[str]: Sorted `gauge_id`s.
        """
        return sorted(
            gauge
            for fmt, src, gauge in self._timeseries
            if src == source and fmt == timeseries_format
        )

    def timeseries_member(
        self, source: str, gauge_id: str, timeseries_format: str = "csv"
    ) -> str | None:
        """Return the member holding one catchment's series, if present.

        Args:
            source: The source directory name.
            gauge_id: The catchment id, e.g. `GRDC_1159100` (note that GRDC
                alone uses an uppercase prefix).
            timeseries_format: `"csv"` or `"netcdf"`.

        Returns:
            str | None: The member name, or `None` when the archive has no such
                catchment.
        """
        return self._timeseries.get((timeseries_format, source, gauge_id))

    def attribute_member(self, source: str, kind: str = "other") -> str | None:
        """Return an attribute table's member name.

        Args:
            source: The source directory name.
            kind: Which table — `"other"` (gauge name, country, lat/lon, area),
                `"caravan"` (climate indices), `"hydroatlas"`, or GRDC's extra
                `"additional"`.

        Returns:
            str | None: The member name, or `None` when absent.
        """
        for name in self.members:
            match = _ATTRIBUTE_RE.search(name)
            if (
                match is not None
                and match["source"] == source
                and match["kind"] == kind
            ):
                return name
        return None

    def shapefile_members(self, source: str) -> list[str]:
        """Return every basin-shapefile sidecar member for one source.

        All parts are returned together because GDAL cannot open a `.shp`
        without at least its `.shx` and `.dbf` alongside it.

        Args:
            source: The source directory name.

        Returns:
            list[str]: The `.shp`/`.shx`/`.dbf`/`.prj`/`.cpg` members, sorted.
        """
        return sorted(
            name
            for name in self.members
            if (match := _SHAPEFILE_RE.search(name)) and match["source"] == source
        )

    def read(self, member: str) -> bytes:
        """Read one member's bytes.

        Args:
            member: The member name, from one of the lookup methods.

        Returns:
            bytes: The decompressed member.

        Raises:
            KeyError: If the member is not in the archive.
            RuntimeError: If the archive was built without a usable backing
                store.
        """
        if self._zip is not None:
            return self._zip.read(member)
        return self.read_many([member])[member]

    def read_many(self, members: list[str]) -> dict[str, bytes]:
        """Read several members, using one archive pass where that matters.

        For a ZIP each member is an independent ranged read, so this is just a
        loop. For a tar it is the difference between one streaming scan and one
        per member, which on the base archive is the difference between minutes
        and hours.

        Args:
            members: Member names to read.

        Returns:
            dict[str, bytes]: Member name to its bytes, for those found.

        Raises:
            RuntimeError: If the archive has neither a ZIP handle nor a tarball.
        """
        if self._zip is not None:
            return {name: self._zip.read(name) for name in members}
        if self._tarball is None or self._extract_dir is None:
            raise RuntimeError(f"caravan archive {self.label!r} has no backing store.")
        extracted = extract_tar_members(self._tarball, set(members), self._extract_dir)
        return {name: path.read_bytes() for name, path in extracted.items()}

    def close(self) -> None:
        """Release the underlying handle, if any."""
        if self._zip is not None:
            self._zip.close()


def attribute_index(archive: CaravanArchive, source: str) -> pd.DataFrame:
    """Read one source's per-catchment location table.

    `attributes_other_<source>.csv` is the small table that carries
    `gauge_id`, `gauge_lat`, `gauge_lon`, `country`, `area` and `gauge_name` —
    it is what a bbox or `country=` request is resolved against. Note the
    sibling `attributes_caravan_<source>.csv` holds climate indices, not
    locations, and `country` here is a full English name ("South Africa"); the
    ISO2 code exists only in GRDC's extra `attributes_additional_grdc.csv`.

    Args:
        archive: An opened archive.
        source: The source directory name.

    Returns:
        pandas.DataFrame: The attribute table, indexed by `gauge_id`.

    Raises:
        ValueError: If the archive has no `attributes_other_<source>.csv`.
    """
    import pandas as pd

    member = archive.attribute_member(source, "other")
    if member is None:
        raise ValueError(
            f"archive {archive.label!r} has no attributes_other_{source}.csv, so "
            f"catchments cannot be resolved by bounding box or country."
        )
    frame = pd.read_csv(BytesIO(archive.read(member)))
    return frame.set_index("gauge_id")


def merge_attributes(
    archive: CaravanArchive, source: str, kinds: tuple[str, ...] = ("other", "caravan")
) -> pd.DataFrame:
    """Read and join several of a source's attribute tables.

    Args:
        archive: An opened archive.
        source: The source directory name.
        kinds: Which tables to join, in order.

    Returns:
        pandas.DataFrame: The joined tables, indexed by `gauge_id`. Tables the
            archive does not carry are skipped.
    """
    import pandas as pd

    frames: list[Any] = []
    for kind in kinds:
        member = archive.attribute_member(source, kind)
        if member is None:
            logger.debug(f"caravan: {archive.label} has no attributes_{kind}_{source}")
            continue
        frames.append(pd.read_csv(BytesIO(archive.read(member))).set_index("gauge_id"))
    if not frames:
        return pd.DataFrame()
    joined = frames[0]
    for extra in frames[1:]:
        joined = joined.join(extra, how="left", rsuffix=f"_{source}")
    return joined
