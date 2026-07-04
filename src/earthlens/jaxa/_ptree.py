"""`ptree` protocol branch — Himawari-8/9 HSD granules from JAXA P-Tree.

Downloads raw HSD `.DAT.bz2` granules from `ftp.ptree.jaxa.jp` over
plain FTP (stdlib `ftplib` — the A1 probe on 2026-07-04 confirmed the
archive still serves FTP, so this branch does **not** require
`paramiko`). Decoding HSD to arrays is deliberately out of scope — that
is `pyramids PY-2` (a `satpy` reader bridge); this module never imports
`satpy` / `xarray` / `cfgrib`.

A full-disk observation is split into **10 segments** per band per
10-minute timeslot (`S0110`…`S1010`), with **per-band resolution codes**
pinned by the A1 probe: `B03=R05`, `B01/B02/B04=R10`, and `B05`-`B16=R20`.
For each `(band, timeslot)` in the requested window this module builds
all 10 segment paths under `/jma/hsd/YYYYMM/DD/HH/` and downloads every
one; a single-segment request would silently drop 90 % of the disk.

The transport client is **injectable** (`transport_factory=`) so tests
substitute a fake without touching the network. The default factory
returns an :class:`FtplibTransport`, a thin wrapper over `ftplib.FTP`
that resolves credentials from :class:`JaxaAuth` and translates the
protocol's 30-day retention into a client-side guard (`G4`) rather than
letting a bare `450`/`550` FTP error surface to callers.
"""

from __future__ import annotations

import datetime as dt
import ftplib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
from earthlens.jaxa.auth import AuthenticationError, JaxaAuth
from earthlens.jaxa.catalog import Dataset

#: The active Himawari satellite currently populating the P-Tree
#: archive. The A1 probe on 2026-07-04 saw only `H09` (Himawari-9) in
#: the last 30 days of live traffic — Himawari-8 is retired. Kept as a
#: module constant so a future `H10` swap is a one-line change.
_DEFAULT_SATELLITE: str = "H09"

#: HSD filename resolution code per band. Pinned by the A1 live probe
#: — the plan's roadmap sketch was ambiguous, so this table is the
#: authoritative source. B03 (visible high-res) is 0.5 km, three visible
#: / near-IR bands are 1.0 km, and every IR band is 2.0 km.
_HIMAWARI_BAND_RESOLUTION: dict[str, str] = {
    "B01": "R10",
    "B02": "R10",
    "B03": "R05",
    "B04": "R10",
    "B05": "R20",
    "B06": "R20",
    "B07": "R20",
    "B08": "R20",
    "B09": "R20",
    "B10": "R20",
    "B11": "R20",
    "B12": "R20",
    "B13": "R20",
    "B14": "R20",
    "B15": "R20",
    "B16": "R20",
}

#: All 10 full-disk segment codes for one band × one 10-minute slot.
_SEGMENTS: tuple[str, ...] = tuple(f"S{i:02d}10" for i in range(1, 11))

#: HSD cadence — one observation every 10 minutes, on the 10-minute mark.
_CADENCE_MINUTES: int = 10

#: Rolling P-Tree archive window in days. Confirmed by the A1 retention
#: sweep: dates D-1 through D-30 return listings; D-32 and older reject
#: with `450 No such file or directory`.
_RETENTION_DAYS: int = 30

#: P-Tree FTP host. Only exists on port 21 (plain FTP), per A1.
_HOST: str = "ftp.ptree.jaxa.jp"

#: FTP command timeout for connect / login / list / download.
_TIMEOUT_SECONDS: int = 60

#: HSD granule filename pattern used to sanity-check a listing.
#: Segment codes are strictly `S0110`..`S1010` per the HSD README
#: (the 10-of-10 full-disk set), so the segment group rejects
#: nonsense values like `S9910`.
_HSD_FILENAME_RE: re.Pattern[str] = re.compile(
    r"^HS_(H\d\d)_(\d{8})_(\d{4})_(B\d\d)_FLDK_(R\d\d)_"
    r"(S(?:0[1-9]|10)10)\.DAT\.bz2$"
)


class RetentionError(ValueError):
    """Raised when a P-Tree request falls outside the 30-day archive.

    P-Tree keeps only the last 30 days of HSD granules; older dates return
    an FTP `450 No such file or directory` (not a permanent `550`), which
    is opaque. This client-side guard fails fast with the exact archive
    window instead so users can adjust the request.
    """


class PtreeTransport(Protocol):
    """Minimal transport surface a P-Tree fetch needs.

    Any object implementing this protocol can back :func:`fetch_ptree`;
    tests supply a fake that never touches the network. The default
    concrete implementation is :class:`FtplibTransport`.
    """

    def login(self, user: str, password: str) -> None:
        """Authenticate the transport with the P-Tree host."""

    def download_file(self, remote_path: str, local_path: Path) -> None:
        """Fetch `remote_path` to `local_path` on the local filesystem."""

    def close(self) -> None:
        """Release any underlying connection/session."""


class FtplibTransport:
    """Default P-Tree transport backed by stdlib `ftplib.FTP`.

    Attributes:
        host: The P-Tree FTP host — always `ftp.ptree.jaxa.jp`.
        timeout: Seconds before an FTP command aborts.

    Examples:
        - Constructing the transport does not connect (login does):
            ```python
            >>> from earthlens.jaxa._ptree import FtplibTransport
            >>> t = FtplibTransport()
            >>> t.host
            'ftp.ptree.jaxa.jp'

            ```
    """

    def __init__(
        self,
        host: str = _HOST,
        timeout: int = _TIMEOUT_SECONDS,
    ) -> None:
        """Initialise the transport.

        Args:
            host: P-Tree FTP host (defaults to `ftp.ptree.jaxa.jp`).
            timeout: Per-command timeout in seconds.
        """
        self.host = host
        self.timeout = timeout
        self._ftp: ftplib.FTP | None = None

    def login(self, user: str, password: str) -> None:
        """Open the FTP connection and authenticate.

        Args:
            user: P-Tree username (the registered email).
            password: P-Tree password.

        Raises:
            ConnectionError: Wraps any transport-level failure from the
                FTP handshake (`OSError`, `ftplib.error_temp` /
                `error_proto`, `EOFError`, …). The connection is
                closed before re-raising.
            AuthenticationError: Wraps `ftplib.error_perm` on bad
                credentials — the message names the fix but does not
                echo the raw server reply (which some servers
                interpolate the attempted username into).
        """
        try:
            self._ftp = ftplib.FTP(self.host, timeout=self.timeout)
        except OSError as exc:
            raise ConnectionError(
                f"could not connect to {self.host}:21 -- {exc}"
            ) from exc
        try:
            self._ftp.login(user=user, passwd=password)
        except ftplib.error_perm as exc:
            self._ftp.close()
            self._ftp = None
            raise AuthenticationError(
                f"P-Tree FTP login rejected on {self.host}. "
                "Check JAXA_PTREE_USERNAME / JAXA_PTREE_PASSWORD."
            ) from exc
        except ftplib.all_errors as exc:
            # error_temp (421 too many users), error_proto, EOFError, OSError,
            # socket.timeout — any non-auth failure. Same cleanup shape as the
            # error_perm branch so no dangling socket to `ftp.ptree.jaxa.jp`
            # leaks into the process; ConnectionError distinguishes "the
            # server rejected the *handshake*" from AuthenticationError's
            # "the server rejected the *credentials*".
            self._ftp.close()
            self._ftp = None
            raise ConnectionError(
                f"P-Tree FTP handshake failed on {self.host}: {exc}"
            ) from exc

    def download_file(self, remote_path: str, local_path: Path) -> None:
        """Fetch `remote_path` to `local_path` (binary transfer).

        A `.part` sidecar receives the bytes and is renamed to
        `local_path` only on a successful transfer; a mid-flight failure
        removes the `.part` file so a subsequent read never sees a
        truncated `.DAT.bz2`.

        Args:
            remote_path: Absolute path on the FTP server.
            local_path: Destination on the local filesystem (parent dir
                is created).

        Raises:
            RuntimeError: If :meth:`login` was not called first.
            FileNotFoundError: When the server rejects the path with
                `error_perm` (550) or `error_temp` (450) — translated so
                a caller does not have to import `ftplib` to catch it.
            ConnectionError: For any other transport-side failure
                (`OSError`, `socket.timeout`, EOF, …). The partial file
                is removed before re-raising.
        """
        if self._ftp is None:
            raise RuntimeError("call login() before download_file().")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        partial = local_path.with_suffix(local_path.suffix + ".part")
        try:
            with partial.open("wb") as handle:
                self._ftp.retrbinary(f"RETR {remote_path}", handle.write)
        except (ftplib.error_perm, ftplib.error_temp) as exc:
            partial.unlink(missing_ok=True)
            raise FileNotFoundError(
                f"P-Tree rejected {remote_path}: {exc}"
            ) from exc
        except ftplib.all_errors as exc:
            partial.unlink(missing_ok=True)
            raise ConnectionError(
                f"P-Tree transfer failed for {remote_path}: {exc}"
            ) from exc
        partial.replace(local_path)

    def close(self) -> None:
        """Close the FTP connection if it is open.

        Prefers `QUIT` (which flushes buffers), but falls back to a bare
        socket close if the quit exchange fails — `ftplib.all_errors`
        already covers `OSError`, `EOFError`, and every `ftplib.Error`
        subclass, so listing it once is enough.
        """
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
        except ftplib.all_errors:
            self._ftp.close()
        finally:
            self._ftp = None


def _floor_to_slot(when: dt.datetime) -> dt.datetime:
    """Round `when` down to the nearest HSD 10-minute observation mark."""
    minute = (when.minute // _CADENCE_MINUTES) * _CADENCE_MINUTES
    return when.replace(minute=minute, second=0, microsecond=0)


def _iter_slots(
    start: dt.datetime, end: dt.datetime,
) -> Iterator[dt.datetime]:
    """Yield every HSD 10-minute observation timestamp in `[start, end]`.

    Both bounds are inclusive; both are floored to the previous
    10-minute mark so a `start` of `10:04` yields the `10:00` slot and
    so on. Yields nothing when `start`'s floored slot exceeds `end`'s
    floored slot — in practice `TemporalExtent` enforces
    `start_date <= end_date` so this case is unreachable via the
    backend, but the guard keeps the helper safe for direct callers.
    """
    cursor = _floor_to_slot(start)
    stop = _floor_to_slot(end)
    step = dt.timedelta(minutes=_CADENCE_MINUTES)
    while cursor <= stop:
        yield cursor
        cursor += step


def _as_utc(when: dt.datetime) -> dt.datetime:
    """Return `when` in UTC, treating a naive input as UTC.

    Backend construction produces naive `datetime`s from
    `strptime(fmt)` on the user's ISO strings — those strings are
    documented as UTC in the P-Tree docs. A tz-aware input is
    converted to UTC via `astimezone`.
    """
    if when.tzinfo is None:
        return when.replace(tzinfo=dt.UTC)
    return when.astimezone(dt.UTC)


def _guard_retention(
    time: TemporalExtent, *, now: dt.datetime | None = None,
) -> None:
    """Raise :class:`RetentionError` if the window is outside the archive.

    Guards **both bounds** so a long window that begins inside
    retention but extends past `now` (a `start_date` in the past,
    `end_date` in the future) is rejected up-front instead of failing
    mid-download when the loop reaches a slot the archive has not
    populated yet. A configurable `now` keeps the check testable
    without freezing the system clock. Both bounds are compared in
    UTC — a tz-naive `datetime` is treated as UTC (mirrors the
    backend's own `_check_input_dates` convention).
    """
    if now is None:
        now = dt.datetime.now(dt.UTC)
    request_start = _as_utc(time.start_date)
    request_end = _as_utc(time.end_date)
    horizon = now - dt.timedelta(days=_RETENTION_DAYS)
    if request_start < horizon:
        raise RetentionError(
            "P-Tree serves only the last "
            f"{_RETENTION_DAYS} days of HSD granules. The requested "
            f"start {request_start.date()} predates the current archive "
            f"window (>= {horizon.date()}). Shorten the window or use a "
            "different archive for older dates."
        )
    if request_end > now:
        raise RetentionError(
            f"the requested end {request_end.date()} is in the future "
            f"({now.date()}); P-Tree only serves already-observed slots. "
            "Move `end` to now-or-earlier before retrying."
        )


def _resolve_bands(dataset: Dataset, bands_override: list[str] | None) -> list[str]:
    """Return the bands to fetch, validated against the Himawari band set.

    Falls back to the dataset row's `default_band` when the caller did
    not pass an override (`bands_override is None`). An empty
    `bands_override=[]` is a caller error and is rejected — it is
    distinct from "no override" so a downstream mis-computed empty
    list surfaces here instead of silently masking as the
    `default_band` recovery.
    """
    if bands_override is None:
        if not dataset.default_band:
            raise ValueError(
                f"dataset {dataset.key!r} has no default_band and no "
                "bands= override was supplied; pass at least one of "
                "B01..B16."
            )
        bands = [dataset.default_band]
    else:
        if not bands_override:
            raise ValueError(
                "bands= was passed as an empty list; supply at least "
                "one of B01..B16, or drop the argument to use the "
                f"dataset's default_band ({dataset.default_band!r})."
            )
        bands = list(bands_override)
    unknown = [b for b in bands if b not in _HIMAWARI_BAND_RESOLUTION]
    if unknown:
        raise ValueError(
            f"unknown Himawari band(s) {unknown!r}; valid bands are "
            f"{sorted(_HIMAWARI_BAND_RESOLUTION)}."
        )
    return bands


def _segment_paths(
    slot: dt.datetime, band: str, satellite: str,
) -> list[str]:
    """Return the 10 segment paths for `(slot, band)` under `/jma/hsd/`.

    The filename layout is fixed by the HSD README and confirmed live:
    `HS_H09_20260704_0000_B03_FLDK_R05_S0110.DAT.bz2` for the first
    segment of B03 at 00:00 UTC on 2026-07-04.
    """
    resolution = _HIMAWARI_BAND_RESOLUTION[band]
    yyyymm = slot.strftime("%Y%m")
    dd = slot.strftime("%d")
    hh = slot.strftime("%H")
    yyyymmdd = slot.strftime("%Y%m%d")
    hhmm = slot.strftime("%H%M")
    directory = f"/jma/hsd/{yyyymm}/{dd}/{hh}/"
    return [
        f"{directory}HS_{satellite}_{yyyymmdd}_{hhmm}_{band}_FLDK_"
        f"{resolution}_{seg}.DAT.bz2"
        for seg in _SEGMENTS
    ]


def _local_target(remote_path: str, out_dir: Path) -> Path:
    """Map a remote HSD path onto a mirrored local layout under `out_dir`.

    HSD paths built by `_segment_paths` always have shape
    `/jma/hsd/YYYYMM/DD/HH/<filename>` (6 parts after `strip("/")`),
    so the mirrored branch is the only one that fires via the fetch
    flow. The short-path fallback exists purely so a direct caller who
    hands in a non-HSD `remote_path` (e.g. a README under `/pub/`)
    still gets a sensible `out_dir / <filename>` back — dead code from
    the fetch loop's perspective.
    """
    filename = Path(remote_path).name
    parts = remote_path.strip("/").split("/")
    if len(parts) >= 5:
        yyyymm, dd, hh = parts[2], parts[3], parts[4]
        return out_dir / yyyymm / dd / hh / filename
    return out_dir / filename


def fetch_ptree(
    *,
    dataset: Dataset,
    space: SpatialExtent,
    time: TemporalExtent,
    auth: JaxaAuth,
    out_dir: Path,
    bands: list[str] | None = None,
    satellite: str = _DEFAULT_SATELLITE,
    transport_factory: Callable[[], PtreeTransport] | None = None,
    now: dt.datetime | None = None,
) -> list[Path]:
    """Download the full-disk HSD segments for `dataset` over P-Tree FTP.

    For every 10-minute observation timestamp in `time` and every band
    in `bands` (or the row's `default_band` when the caller did not pass
    one), fetches all 10 segment files of the full-disk (`FLDK`) tile
    from `ftp.ptree.jaxa.jp` and writes them under `out_dir` in a
    `YYYYMM/DD/HH/` mirrored layout. The `space` argument is accepted
    for API symmetry with the other JAXA branches but the FLDK tile is
    always full-disk — nothing is bbox-cropped.

    Args:
        dataset: The resolved catalog row. Must carry
            `protocol="ptree"`; `default_band` is used when `bands` is
            not supplied.
        space: The requested WGS84 bbox. Accepted but unused (full-disk
            HSD covers the entire hemisphere the satellite sees).
        time: The requested date/time window; timestamps are floored to
            the 10-minute HSD cadence and expanded to every slot in the
            inclusive range. Naive `datetime`s on either bound are
            interpreted as UTC (matches the backend's own strptime
            output).
        auth: A configured :class:`JaxaAuth` bound to `protocol="ptree"`.
            The resolved username / password are passed straight to the
            transport's `login()`.
        out_dir: Output directory (created if missing). Written files
            live under `out_dir / YYYYMM / DD / HH /` to keep several
            timeslots from colliding on filenames.
        bands: Optional list of Himawari bands (`B01`..`B16`) to fetch.
            When omitted, falls back to `dataset.default_band`; an
            explicit empty list is a caller error.
        satellite: Himawari satellite id (`H09` today; `H10` if/when
            JAXA swaps).
        transport_factory: Injectable transport factory — tests supply a
            fake to avoid the network. `None` uses the stdlib-`ftplib`
            :class:`FtplibTransport`.
        now: Injectable "current UTC time" reference. `None` uses
            `datetime.now(UTC)`. Kept for deterministic retention tests.

    Returns:
        list[Path]: One local path per downloaded segment file, in
            `(slot, band, segment)` order. `TemporalExtent` enforces
            `start <= end`, so the returned list is never empty on a
            valid input.

    Raises:
        RetentionError: When `time.start_date` predates the 30-day
            P-Tree archive window, or `time.end_date` is in the future.
        AuthenticationError: When `auth` has not been configured or the
            transport's login is refused.
        ValueError: When the resolved bands include a code that is not
            a known Himawari band, or the dataset row has no
            `default_band` and the caller passed no `bands=` override,
            or `bands=` was passed as an empty list.
        FileNotFoundError: When the P-Tree host rejects an expected
            segment path (translated from `ftplib.error_perm` /
            `error_temp`).
        ConnectionError: When a transport-side failure aborts the
            handshake or a transfer mid-flight (any non-auth
            `ftplib.all_errors` variant on login / retr).
    """
    del space  # accepted for API uniformity; FLDK is always full-disk.
    _guard_retention(time, now=now)

    if auth.username is None or auth.password is None:
        raise AuthenticationError(
            "P-Tree credentials are not resolved; call auth.configure() first."
        )

    band_list = _resolve_bands(dataset, bands)
    # `FtplibTransport` (a class) is itself a callable that returns a
    # fresh instance, so no factory helper is needed.
    factory = transport_factory or FtplibTransport
    transport = factory()

    written: list[Path] = []
    try:
        transport.login(auth.username, auth.password.get_secret_value())
        for slot in _iter_slots(time.start_date, time.end_date):
            for band in band_list:
                for remote in _segment_paths(slot, band, satellite):
                    local = _local_target(remote, out_dir)
                    transport.download_file(remote, local)
                    written.append(local)
    finally:
        transport.close()
    return written
