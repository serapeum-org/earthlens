"""Catalog-tooling handlers for the Climate Hazards Center (CHC) backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). CHC is served over anonymous
FTP: the refresher BFS-walks the product tree, the prober samples one directory's
filenames, and the audit diffs the live tree against the catalog's `ftp_bases`.
"""

from __future__ import annotations

import re
from ftplib import FTP, error_perm  # nosec B402  # noqa: S402
from typing import Any

from earthlens.cli.toolkit import HTTP_TIMEOUT, lint, require

#: CHC anonymous-FTP host and the products root walked for coverage.
_CHC_FTP_HOST = "data.chc.ucsb.edu"
_CHC_ROOT = "pub/org/chc/products"
#: How far the BFS descends below the root before giving up on a branch.
_CHC_MAX_DEPTH = 6
#: Suffixes that mark a leaf "data file" (so its directory is a product dir).
_CHC_DATA_SUFFIXES = (
    ".tif",
    ".tif.gz",
    ".tiff",
    ".nc",
    ".nc4",
    ".bil",
    ".bil.gz",
    ".bin",
    ".cog",
    ".png",
    ".grb",
    ".grib",
)
_CHC_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def _chc_is_product_listing(entries: list[str]) -> bool:
    """Return whether a directory listing marks a CHC product directory.

    A product directory is one whose children are data files (`.tif`,
    `.nc`, `.bil`, ...) or year-named subdirectories; anything else is an
    intermediate directory to descend into.

    Args:
        entries: The directory's child names.

    Returns:
        `True` if the listing looks like a product directory.
    """
    has_data = any(name.lower().endswith(_CHC_DATA_SUFFIXES) for name in entries)
    has_years = any(_CHC_YEAR_RE.fullmatch(name) for name in entries)
    return has_data or has_years


def _chc_walk(ftp: FTP, root: str, max_depth: int) -> list[str]:
    """BFS-walk `root` and return every discovered CHC product directory.

    Mirrors `tools/chc/refresh_chc_catalog.py`: descends intermediate
    directories until a product directory is reached or `max_depth` levels
    below `root`. Unreachable / permission-denied directories are skipped.

    Args:
        ftp: A logged-in FTP connection.
        root: The products root to walk from (no trailing slash).
        max_depth: Maximum levels to descend below `root`.

    Returns:
        The sorted product-directory paths (each `.../`-terminated).
    """
    discovered: list[str] = []
    queue: list[tuple[str, int]] = [(root, 0)]
    while queue:
        path, depth = queue.pop(0)
        try:
            ftp.cwd("/")
            ftp.cwd(path)
            entries = sorted(ftp.nlst())
        except (error_perm, OSError):
            continue
        if _chc_is_product_listing(entries):
            discovered.append(path.rstrip("/") + "/")
            continue
        if depth >= max_depth:
            continue
        for entry in entries:
            if "." in entry:  # an unrecognised file (e.g. README.txt)
                continue
            queue.append((f"{path.rstrip('/')}/{entry}/", depth + 1))
    return sorted(discovered)


def _chc_discovered_paths() -> list[str]:
    """Return every CHC product directory from a live anonymous-FTP walk."""
    with FTP(_CHC_FTP_HOST, timeout=HTTP_TIMEOUT) as ftp:  # nosec B321
        ftp.login()
        return _chc_walk(ftp, _CHC_ROOT, _CHC_MAX_DEPTH)


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List every CHC product directory from the live FTP tree (anonymous).

    CHC's refreshable axis is the set of FTP product directories, diffed
    against the distinct `ftp_bases` the catalog references (see
    `bundled_ids`) — not the hand-curated `available_datasets:` slugs, which
    are a human-curation artefact the diff cannot derive.

    Args:
        catalog: The loaded CHC `Catalog` (unused; the FTP tree is the source).

    Returns:
        A single-group mapping `{"chc": [sorted product directories]}`.
    """
    return {"chc": sorted({p.rstrip("/") + "/" for p in _chc_discovered_paths()})}


def bundled_ids(catalog: Any) -> list[str]:
    """Return the distinct `ftp_bases` paths the CHC catalog references.

    Serves as both the `audit` diff axis (bundled ids) and the curated-id
    resolver — CHC has no machine-writable `available_datasets:` index.
    """
    return sorted(
        {
            base.rstrip("/") + "/"
            for dataset in catalog.datasets.values()
            for base in dataset.ftp_bases.values()
        }
    )


#: Curated-id resolver over the catalog's `ftp_bases` (same axis as the diff).
curated_ids = bundled_ids


def _chc_sample_files(ftp_base: str, limit: int = 10) -> list[str]:
    """Return a sample of filenames under a CHC FTP directory (anonymous)."""
    with FTP(_CHC_FTP_HOST, timeout=HTTP_TIMEOUT) as ftp:  # nosec B321
        ftp.login()
        ftp.cwd(ftp_base)
        return sorted(ftp.nlst())[:limit]


def _suggest_pattern(filenames: list[str]) -> str:
    """Infer a `{year}.{month}.{day}`-style template from a sample filename.

    Ported from the retired `tools/chc/probe_chirps_gefs.py`: tags 4-digit
    years, 3-digit day-of-year runs, then the first two dotted 2-digit
    segments as month / day. A seed for the catalog `file_patterns` — the
    maintainer eyeballs it against the listing and refines.

    Args:
        filenames: The sampled directory listing.

    Returns:
        The first filename transformed into a template, or `""` when empty.
    """
    if not filenames:
        return ""
    pattern = re.sub(r"\b(19|20)\d{2}\b", "{year}", filenames[0])
    pattern = re.sub(r"(?<!\d)(\d{3})(?!\d)", "{doy}", pattern)
    seen_month = False
    out: list[str] = []
    for piece in re.split(r"(\{year\})", pattern):
        if piece == "{year}":
            out.append(piece)
            continue
        new_piece = piece
        if not seen_month:
            new_piece, hits = re.subn(
                r"(?<=\.)(\d{2})(?=\.|$)", "{month}", new_piece, count=1
            )
            seen_month = bool(hits)
        if seen_month and "{day}" not in new_piece:
            new_piece = re.sub(r"(?<=\.)(\d{2})(?=\.|$)", "{day}", new_piece, count=1)
        out.append(new_piece)
    return "".join(out)


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a CHC dataset's FTP directory for a sample of filenames.

    Args:
        catalog: The loaded CHC `Catalog` (resolves the dataset's `ftp_bases`).
        dataset: A curated CHC dataset key.

    Returns:
        Mapping of sample filename to `{}`, plus a `(suggested pattern)` row
        carrying a `{pattern}` template inferred from the listing (the seed
        for the catalog `file_patterns`).

    Raises:
        ValueError: If the dataset has no `ftp_bases`.
    """
    record = catalog.datasets.get(dataset)
    bases = list(getattr(record, "ftp_bases", {}).values()) if record else []
    if not bases:
        raise ValueError(f"no ftp_bases for {dataset!r}")
    files = _chc_sample_files(bases[0])
    schema: dict[str, dict[str, Any]] = {name: {} for name in files}
    pattern = _suggest_pattern(files)
    if pattern:
        schema["(suggested pattern)"] = {"pattern": pattern}
    return schema


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each CHC dataset needs FTP bases, a file pattern, and variables."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a dataset missing ftp_bases, variables, or a file pattern."""
        issues = require(key, record, ("ftp_bases", "variables"))
        if not (
            getattr(record, "file_patterns", None)
            or getattr(record, "discrete_files", None)
        ):
            issues.append(f"{key}: no file_patterns or discrete_files")
        return issues

    return lint(catalog, check)
