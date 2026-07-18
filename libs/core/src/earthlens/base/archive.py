"""Backend-agnostic archive extraction.

One Zip-Slip-guarded extractor for the provider backends that download a
`.zip` / `.7z` and pull raster members out of it (ghsl, worldpop, ...). Kept
here rather than re-implemented per backend so the CWE-22 (path-traversal)
guard is applied uniformly — several backends previously extracted without it.

Specialised archive handling stays in its backend: ecmwf's in-place
single-NetCDF unwrap (`os.replace` atomic swap) and the backends that cache a
zip for `/vsizip/` reads without extracting (glaciers, solar_wind_atlas) do
not fit this member-to-directory shape.
"""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

from loguru import logger


def _assert_safe_members(names: list[str], dest_dir: Path) -> None:
    """Reject member names that would extract outside `dest_dir` (Zip Slip).

    Extracting attacker-controlled member names (CWE-22) is the standard
    untrusted-archive pitfall, so every member's resolved destination is
    checked to stay within `dest_dir` before any extraction runs.

    Args:
        names: The archive's member names.
        dest_dir: The directory members will be extracted into.

    Raises:
        ValueError: If any member resolves outside `dest_dir`.
    """
    base = dest_dir.resolve()
    for name in names:
        target = (dest_dir / name).resolve()
        if target != base and base not in target.parents:
            raise ValueError(
                f"refusing to extract unsafe path {name!r} from the archive "
                f"(escapes {dest_dir})."
            )


def _matches(name: str, include: tuple[str, ...]) -> bool:
    """Whether a member name is a file matching one of the `include` suffixes.

    Directory entries never match. An archive may carry explicit directory
    members (a trailing `/`), and with the default `include=()` — which keeps
    every *file* — those would otherwise be returned as extracted paths and,
    under `fix_mode`, get `chmod`ed as if they were files.

    Args:
        name: The archive member name.
        include: Suffixes to keep; an empty tuple keeps every file.

    Returns:
        bool: `True` when `name` is not a directory entry and either `include`
            is empty or `name` ends with one of its suffixes, compared
            case-insensitively.
    """
    if name.endswith("/"):
        return False
    return not include or name.lower().endswith(include)


def extract_members(
    archive_path: Path,
    dest_dir: Path,
    *,
    include: tuple[str, ...] = (),
    fmt: str | None = None,
    single: bool = False,
    fix_mode: bool = False,
) -> list[Path]:
    """Extract the archive members whose name ends with an `include` suffix.

    Members are filtered by case-insensitive suffix and every member's resolved
    destination is checked to stay within `dest_dir` (Zip Slip / CWE-22) before
    anything is written.

    Args:
        archive_path: The downloaded `.zip` or `.7z`.
        dest_dir: Directory to extract into (created if absent).
        include: Suffixes to keep (e.g. `(".tif", ".tiff")`); an empty tuple
            keeps every member.
        fmt: `"zip"` or `"7z"`; inferred from `archive_path`'s suffix when
            `None`.
        single: Require exactly one matching member and return it alone —
            raise `ValueError` when none match, and log a warning (keeping the
            first) when more than one does.
        fix_mode: `chmod` each extracted file user read/write. py7zr restores
            the archive's stored POSIX mode, which is often restrictive enough
            to break the downstream GDAL read on Linux.

    Returns:
        list[Path]: The extracted member paths, sorted.

    Raises:
        ValueError: On an unsafe (escaping) member, an unknown `fmt`, or when
            `single=True` and no member matches `include`.
        ImportError: When `fmt == "7z"` and the optional `py7zr` is missing.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    fmt = fmt or ("7z" if archive_path.suffix.lower() == ".7z" else "zip")

    if fmt == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            _assert_safe_members(zf.namelist(), dest_dir)
            members = sorted(m for m in zf.namelist() if _matches(m, include))
            members = _select(members, archive_path, single=single)
            for member in members:
                zf.extract(member, dest_dir)
    elif fmt == "7z":
        try:
            import py7zr
        except ImportError as exc:
            raise ImportError(
                "extracting .7z archives needs py7zr. "
                "Install it with: pip install earthlens[worldpop]"
            ) from exc
        with py7zr.SevenZipFile(archive_path) as zf:
            names = zf.getnames()
            _assert_safe_members(names, dest_dir)
            members = sorted(n for n in names if _matches(n, include))
            members = _select(members, archive_path, single=single)
            zf.extract(path=dest_dir, targets=members)
    else:
        raise ValueError(f"unsupported archive format {fmt!r}; expected 'zip' or '7z'.")

    # A member name can carry sub-directories, and py7zr may nest, so resolve
    # every extracted member from its own name rather than an rglob sweep (which
    # would also pick up files that were already in `dest_dir`).
    #
    # Filter to real files on disk: `_matches` rejects directory entries by
    # their trailing `/`, but py7zr's `getnames()` can list a directory member
    # *without* one, so a name-only check can't catch it. Testing the extracted
    # path is authoritative for both formats and keeps a directory out of the
    # returned list (and away from the `fix_mode` chmod).
    extracted = sorted(
        path for member in members if (path := dest_dir / member).is_file()
    )
    if fix_mode:
        for path in extracted:
            path.chmod(path.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)
    return extracted


def _select(members: list[str], archive_path: Path, *, single: bool) -> list[str]:
    """Narrow the matched members to one when `single`, else return them all.

    Args:
        members: The suffix-matched member names (sorted).
        archive_path: The archive, for the error / warning message.
        single: Whether exactly one member is expected.

    Returns:
        The one-element list (`single`) or the full `members` list.

    Raises:
        ValueError: When `single` and no member matched.
    """
    if not single:
        return members
    if not members:
        raise ValueError(f"archive {archive_path} contains no matching member.")
    if len(members) > 1:
        logger.warning(
            f"archive {archive_path} has multiple matching members {members}; "
            f"using the first ({members[0]})."
        )
    return members[:1]
