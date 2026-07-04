"""No-network unit tests for the P-Tree fetch branch and injectable transport."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from pydantic import SecretStr

from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
from earthlens.jaxa import AuthenticationError, JaxaAuth, JaxaCredentials
from earthlens.jaxa._ptree import (
    RetentionError,
    _default_transport_factory,
    _floor_to_slot,
    _guard_retention,
    _HIMAWARI_BAND_RESOLUTION,
    _HSD_FILENAME_RE,
    _iter_slots,
    _local_target,
    _resolve_bands,
    _segment_paths,
    fetch_ptree,
    FtplibTransport,
    PtreeTransport,
)
from earthlens.jaxa.catalog import Dataset

pytestmark = [pytest.mark.jaxa, pytest.mark.unit]


class _RecordingTransport:
    """Fake `PtreeTransport` that records every remote path it was asked for.

    Each `download_file` call writes a tiny 4-byte bz2 stub so the caller
    sees a real (nonzero) local file, while the paths themselves land in
    `.remote_paths` and `.local_paths` for the tests to assert on.
    """

    #: BZ2 magic bytes followed by a filler byte — enough to look like a
    #: bz2 stream to a downstream reader without invoking `bz2` here.
    _STUB_BYTES: bytes = b"BZh9"

    def __init__(self) -> None:
        self.logged_in: tuple[str, str] | None = None
        self.remote_paths: list[str] = []
        self.local_paths: list[Path] = []
        self.closed: bool = False

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        self.remote_paths.append(remote_path)
        self.local_paths.append(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self._STUB_BYTES)

    def close(self) -> None:
        self.closed = True


def _make_extent(start: dt.datetime, end: dt.datetime) -> TemporalExtent:
    """Build a `TemporalExtent` for a fixed inclusive window."""
    return TemporalExtent(
        start_date=start,
        end_date=end,
        resolution="h",
        dates=pd.date_range(start, end, freq="h"),
    )


@pytest.fixture
def himawari_row() -> Dataset:
    """The bundled Himawari catalog row (loaded via the real Catalog)."""
    from earthlens.jaxa import Catalog
    return Catalog().get("himawari-ahi-fldk")


@pytest.fixture
def bbox() -> SpatialExtent:
    """A tiny WGS84 bbox — fetch_ptree ignores it but the API needs one."""
    return SpatialExtent.from_pairs(lat_lim=[35.0, 36.0], lon_lim=[138.0, 139.0])


@pytest.fixture
def configured_auth(monkeypatch: pytest.MonkeyPatch) -> JaxaAuth:
    """A `JaxaAuth(protocol='ptree')` with resolved credentials."""
    monkeypatch.delenv("JAXA_PTREE_USERNAME", raising=False)
    monkeypatch.delenv("JAXA_PTREE_PASSWORD", raising=False)
    auth = JaxaAuth(
        JaxaCredentials(
            ptree_username="alice@example.org",
            ptree_password=SecretStr("pytest-fixture-not-a-real-pw"),
        ),
        protocol="ptree",
    )
    auth.configure()
    return auth


class TestSegmentPaths:
    """Tests for `_segment_paths` — the deterministic filename builder."""

    def test_ten_segments_per_band_slot(self) -> None:
        """A single (slot, band) call produces exactly 10 segment paths."""
        slot = dt.datetime(2026, 7, 4, 0, 0, tzinfo=dt.UTC)
        paths = _segment_paths(slot, "B03", "H09")
        assert len(paths) == 10

    def test_paths_match_live_hsd_layout(self) -> None:
        """The first segment matches the exact layout the A1 probe saw."""
        slot = dt.datetime(2026, 7, 4, 0, 0, tzinfo=dt.UTC)
        first = _segment_paths(slot, "B03", "H09")[0]
        assert first == (
            "/jma/hsd/202607/04/00/"
            "HS_H09_20260704_0000_B03_FLDK_R05_S0110.DAT.bz2"
        )

    def test_segment_codes_are_S0110_through_S1010(self) -> None:
        """Every segment code follows the SNN10 pattern in order."""
        slot = dt.datetime(2026, 7, 4, 0, 0, tzinfo=dt.UTC)
        paths = _segment_paths(slot, "B03", "H09")
        codes = [Path(p).stem.split("_")[-1].removesuffix(".DAT") for p in paths]
        assert codes == [f"S{i:02d}10" for i in range(1, 11)]

    @pytest.mark.parametrize(
        "band, expected",
        [
            ("B01", "R10"),
            ("B02", "R10"),
            ("B03", "R05"),
            ("B04", "R10"),
            ("B05", "R20"),
            ("B13", "R20"),
            ("B16", "R20"),
        ],
    )
    def test_per_band_resolution_code(self, band: str, expected: str) -> None:
        """Each band emits its A1-pinned resolution code in the filename."""
        slot = dt.datetime(2026, 7, 4, 0, 0, tzinfo=dt.UTC)
        first = _segment_paths(slot, band, "H09")[0]
        assert f"_{expected}_" in first

    def test_every_output_matches_hsd_regex(self) -> None:
        """Every filename parses against `_HSD_FILENAME_RE`."""
        slot = dt.datetime(2026, 7, 4, 0, 0, tzinfo=dt.UTC)
        for band in _HIMAWARI_BAND_RESOLUTION:
            for path in _segment_paths(slot, band, "H09"):
                assert _HSD_FILENAME_RE.match(Path(path).name), path


class TestFloorAndSlots:
    """Tests for `_floor_to_slot` and `_iter_slots`."""

    def test_floor_to_10_minute_mark(self) -> None:
        """Any minute floors down to the previous 10-minute observation."""
        when = dt.datetime(2026, 7, 4, 12, 47, 33, tzinfo=dt.UTC)
        assert _floor_to_slot(when) == dt.datetime(
            2026, 7, 4, 12, 40, tzinfo=dt.UTC,
        )

    def test_iter_slots_yields_every_10_minutes_inclusive(self) -> None:
        """`[00:04, 00:32]` yields the four floored 10-minute marks."""
        slots = list(_iter_slots(
            dt.datetime(2026, 7, 4, 0, 4, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 4, 0, 32, tzinfo=dt.UTC),
        ))
        assert [s.strftime("%H:%M") for s in slots] == [
            "00:00", "00:10", "00:20", "00:30",
        ]

    def test_iter_slots_empty_when_start_after_end(self) -> None:
        """`start > end` yields nothing (no exception)."""
        start = dt.datetime(2026, 7, 4, 1, 0, tzinfo=dt.UTC)
        end = dt.datetime(2026, 7, 4, 0, 0, tzinfo=dt.UTC)
        assert list(_iter_slots(start, end)) == []


class TestResolveBands:
    """Tests for `_resolve_bands` — override vs default vs invalid."""

    def test_override_wins_over_default(self) -> None:
        """An explicit override is used verbatim, ignoring `default_band`."""
        row = Dataset(
            key="k", protocol="ptree", short_name="s", default_band="B03",
        )
        assert _resolve_bands(row, ["B13", "B14"]) == ["B13", "B14"]

    def test_default_band_used_when_no_override(self) -> None:
        """Without an override, the row's `default_band` becomes the request."""
        row = Dataset(
            key="k", protocol="ptree", short_name="s", default_band="B03",
        )
        assert _resolve_bands(row, None) == ["B03"]

    def test_missing_default_and_no_override_raises(self) -> None:
        """A row with no default_band and no override is unresolvable."""
        row = Dataset(key="k", protocol="ptree", short_name="s")
        with pytest.raises(ValueError, match="no default_band"):
            _resolve_bands(row, None)

    def test_unknown_band_rejected(self) -> None:
        """A non-Himawari band code fails fast, before any FTP call."""
        row = Dataset(
            key="k", protocol="ptree", short_name="s", default_band="B03",
        )
        with pytest.raises(ValueError, match="B99"):
            _resolve_bands(row, ["B99"])

    def test_empty_bands_override_rejected(self) -> None:
        """An explicit empty `bands=[]` list is a caller error, not a default recovery."""
        row = Dataset(
            key="k", protocol="ptree", short_name="s", default_band="B03",
        )
        with pytest.raises(ValueError, match="empty list"):
            _resolve_bands(row, [])


class TestRetentionGuard:
    """Tests for `_guard_retention` — the 30-day archive window enforcement."""

    def test_recent_start_allowed(self) -> None:
        """A 5-day-old start is within the archive; no error."""
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        te = _make_extent(now - dt.timedelta(days=5), now)
        _guard_retention(te, now=now)

    def test_thirty_day_boundary_allowed(self) -> None:
        """The 30-day boundary itself is within reach."""
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        te = _make_extent(now - dt.timedelta(days=30), now)
        _guard_retention(te, now=now)

    def test_older_than_retention_raises(self) -> None:
        """A 45-day-old start is unreachable; error names the archive window."""
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        te = _make_extent(now - dt.timedelta(days=45), now)
        with pytest.raises(RetentionError, match="30 days"):
            _guard_retention(te, now=now)

    def test_naive_start_treated_as_utc(self) -> None:
        """A tz-naive `start_date` is normalised to UTC before the check."""
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        te = TemporalExtent(
            start_date=dt.datetime(2026, 5, 1),  # naive; predates window
            end_date=dt.datetime(2026, 5, 2),
            resolution="D",
            dates=pd.date_range("2026-05-01", "2026-05-02"),
        )
        with pytest.raises(RetentionError):
            _guard_retention(te, now=now)

    def test_end_date_in_future_rejected(self) -> None:
        """A window that extends past `now` is rejected up-front."""
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        te = _make_extent(
            now - dt.timedelta(days=1), now + dt.timedelta(days=3),
        )
        with pytest.raises(RetentionError, match="in the future"):
            _guard_retention(te, now=now)

    def test_end_date_exactly_now_allowed(self) -> None:
        """`end == now` is within the window; no error."""
        now = dt.datetime(2026, 7, 4, 12, 0, tzinfo=dt.UTC)
        te = _make_extent(now - dt.timedelta(days=1), now)
        _guard_retention(te, now=now)


class TestFetchPtree:
    """End-to-end (fake transport) tests for `fetch_ptree`."""

    def test_returns_paths_for_one_band_one_slot(
        self,
        himawari_row: Dataset,
        bbox: SpatialExtent,
        configured_auth: JaxaAuth,
        tmp_path: Path,
    ) -> None:
        """One 10-min slot × one band → 10 written paths."""
        transport = _RecordingTransport()
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        slot = now - dt.timedelta(hours=1)
        window = _make_extent(slot, slot)
        paths = fetch_ptree(
            dataset=himawari_row, space=bbox, time=window,
            auth=configured_auth, out_dir=tmp_path,
            bands=["B03"],
            transport_factory=lambda: transport, now=now,
        )
        assert len(paths) == 10
        assert all(p.exists() for p in paths)
        assert all(p.name.endswith(".DAT.bz2") for p in paths)

    def test_multi_slot_multi_band_writes_grid(
        self,
        himawari_row: Dataset,
        bbox: SpatialExtent,
        configured_auth: JaxaAuth,
        tmp_path: Path,
    ) -> None:
        """3 slots × 2 bands → 60 files (3 × 2 × 10 segments each)."""
        transport = _RecordingTransport()
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        start = now - dt.timedelta(hours=1)
        end = start + dt.timedelta(minutes=20)  # 00, 10, 20 → 3 slots
        window = _make_extent(start, end)
        paths = fetch_ptree(
            dataset=himawari_row, space=bbox, time=window,
            auth=configured_auth, out_dir=tmp_path,
            bands=["B03", "B13"],
            transport_factory=lambda: transport, now=now,
        )
        assert len(paths) == 3 * 2 * 10

    def test_transport_login_receives_configured_credentials(
        self,
        himawari_row: Dataset,
        bbox: SpatialExtent,
        configured_auth: JaxaAuth,
        tmp_path: Path,
    ) -> None:
        """The resolved username/password reach `transport.login(...)`."""
        transport = _RecordingTransport()
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        slot = now - dt.timedelta(hours=1)
        window = _make_extent(slot, slot)
        fetch_ptree(
            dataset=himawari_row, space=bbox, time=window,
            auth=configured_auth, out_dir=tmp_path,
            bands=["B03"],
            transport_factory=lambda: transport, now=now,
        )
        assert transport.logged_in == (
            "alice@example.org", "pytest-fixture-not-a-real-pw",
        )

    def test_transport_close_always_called(
        self,
        himawari_row: Dataset,
        bbox: SpatialExtent,
        configured_auth: JaxaAuth,
        tmp_path: Path,
    ) -> None:
        """`transport.close()` fires even when `download_file` raises."""

        class _RaisingTransport(_RecordingTransport):
            def download_file(self, remote_path: str, local_path: Path) -> None:
                raise FileNotFoundError(f"gone: {remote_path}")

        transport = _RaisingTransport()
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        slot = now - dt.timedelta(hours=1)
        window = _make_extent(slot, slot)
        with pytest.raises(FileNotFoundError, match="gone:"):
            fetch_ptree(
                dataset=himawari_row, space=bbox, time=window,
                auth=configured_auth, out_dir=tmp_path,
                bands=["B03"],
                transport_factory=lambda: transport, now=now,
            )
        assert transport.closed

    def test_retention_error_before_any_transport_call(
        self,
        himawari_row: Dataset,
        bbox: SpatialExtent,
        configured_auth: JaxaAuth,
        tmp_path: Path,
    ) -> None:
        """A start-date past retention raises before any transport call."""
        transport = _RecordingTransport()
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        old_start = now - dt.timedelta(days=45)
        window = _make_extent(old_start, old_start)
        with pytest.raises(RetentionError):
            fetch_ptree(
                dataset=himawari_row, space=bbox, time=window,
                auth=configured_auth, out_dir=tmp_path,
                bands=["B03"],
                transport_factory=lambda: transport, now=now,
            )
        assert transport.logged_in is None
        assert transport.remote_paths == []

    def test_unconfigured_auth_rejected(
        self,
        himawari_row: Dataset,
        bbox: SpatialExtent,
        tmp_path: Path,
    ) -> None:
        """An auth that never ran `configure()` is rejected."""
        auth = JaxaAuth(
            JaxaCredentials(ptree_username="a@b", ptree_password=SecretStr("p")),
            protocol="ptree",
        )
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        slot = now - dt.timedelta(hours=1)
        window = _make_extent(slot, slot)
        with pytest.raises(AuthenticationError, match="not resolved"):
            fetch_ptree(
                dataset=himawari_row, space=bbox, time=window,
                auth=auth, out_dir=tmp_path,
                bands=["B03"],
                transport_factory=_RecordingTransport, now=now,
            )

    def test_local_path_mirrors_server_layout(
        self,
        himawari_row: Dataset,
        bbox: SpatialExtent,
        configured_auth: JaxaAuth,
        tmp_path: Path,
    ) -> None:
        """Local files land under `out_dir / YYYYMM / DD / HH / filename`."""
        transport = _RecordingTransport()
        now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
        slot = now - dt.timedelta(hours=1)
        window = _make_extent(slot, slot)
        paths = fetch_ptree(
            dataset=himawari_row, space=bbox, time=window,
            auth=configured_auth, out_dir=tmp_path,
            bands=["B03"],
            transport_factory=lambda: transport, now=now,
        )
        for p in paths:
            rel = p.relative_to(tmp_path)
            parts = rel.parts
            assert len(parts) == 4
            assert len(parts[0]) == 6
            assert len(parts[1]) == 2
            assert len(parts[2]) == 2


class TestLocalTargetFallback:
    """Tests for `_local_target` when the remote path is unusually shaped."""

    def test_flat_remote_path_falls_back_to_out_dir_root(
        self, tmp_path: Path,
    ) -> None:
        """A remote with fewer than 5 segments lands at `out_dir / filename`."""
        assert _local_target("/pub/README.txt", tmp_path) == tmp_path / "README.txt"


class TestNoSatpyImport:
    """G5 conformance: `_ptree` must never import `satpy` / `xarray` / `cfgrib`."""

    def test_ptree_module_source_has_no_decode_imports(self) -> None:
        """Grep the module source for banned import statements."""
        import re
        from earthlens.jaxa import _ptree as ptree_module
        source = Path(ptree_module.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(
                r"^\s*(from|import)\s+(satpy|xarray|cfgrib)\b", stripped,
            ):
                raise AssertionError(
                    f"banned import in _ptree.py: {stripped!r}",
                )


class _FakeFTP:
    """Duck-type replacement for `ftplib.FTP` covering the calls used."""

    #: Class-level knob so a test can force `FTP(host, timeout=...)` to
    #: raise before returning an instance.
    connect_raises: OSError | None = None
    login_raises: BaseException | None = None
    retr_raises: BaseException | None = None
    quit_raises: BaseException | None = None

    def __init__(self, host: str, timeout: int) -> None:
        if self.__class__.connect_raises is not None:
            raise self.__class__.connect_raises
        self.host = host
        self.timeout = timeout
        self.logged_in: tuple[str, str] | None = None
        self.retr_calls: list[str] = []
        self.quit_called = False
        self.close_called = False

    def login(self, user: str, passwd: str) -> None:
        if self.__class__.login_raises is not None:
            raise self.__class__.login_raises
        self.logged_in = (user, passwd)

    def retrbinary(self, cmd: str, callback):  # noqa: ANN001
        self.retr_calls.append(cmd)
        if self.__class__.retr_raises is not None:
            raise self.__class__.retr_raises
        callback(b"payload")

    def quit(self) -> None:
        self.quit_called = True
        if self.__class__.quit_raises is not None:
            raise self.__class__.quit_raises

    def close(self) -> None:
        self.close_called = True


@pytest.fixture(autouse=True)
def _reset_fake_ftp() -> None:
    """Reset the class-level failure knobs before each test."""
    _FakeFTP.connect_raises = None
    _FakeFTP.login_raises = None
    _FakeFTP.retr_raises = None
    _FakeFTP.quit_raises = None


class TestDefaultFactory:
    """The default factory returns the concrete FtplibTransport."""

    def test_returns_ftplib_transport(self) -> None:
        """`_default_transport_factory()` produces an `FtplibTransport`."""
        transport = _default_transport_factory()
        assert isinstance(transport, FtplibTransport)
        assert transport.host == "ftp.ptree.jaxa.jp"

    def test_ftplib_transport_download_before_login_raises(
        self, tmp_path: Path,
    ) -> None:
        """`download_file` before `login` fails with a clear runtime error."""
        transport = FtplibTransport()
        with pytest.raises(RuntimeError, match="login"):
            transport.download_file("/x", tmp_path / "y")

    def test_ftplib_transport_close_before_login_is_noop(self) -> None:
        """`close()` on an unopened transport does not raise."""
        FtplibTransport().close()


class TestFtplibTransportBehaviour:
    """`FtplibTransport` end-to-end with a fake `ftplib.FTP`."""

    def test_login_success_then_download_and_close(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Happy path: login, download writes bytes, close QUITs the session."""
        import ftplib
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        transport.login("alice", "secret")
        assert transport._ftp is not None
        assert transport._ftp.logged_in == ("alice", "secret")
        local = tmp_path / "seg.DAT.bz2"
        transport.download_file("/jma/hsd/x/seg.DAT.bz2", local)
        assert local.read_bytes() == b"payload"
        transport.close()
        assert transport._ftp is None

    def test_login_wraps_connect_oserror_as_connection_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A DNS / TCP failure surfaces as `ConnectionError`."""
        import ftplib
        _FakeFTP.connect_raises = OSError("dns down")
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        with pytest.raises(ConnectionError, match="ftp.ptree.jaxa.jp"):
            FtplibTransport().login("u", "p")

    def test_login_wraps_bad_creds_as_authentication_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `530 Login incorrect` (error_perm) becomes `AuthenticationError`."""
        import ftplib
        _FakeFTP.login_raises = ftplib.error_perm("530 Login incorrect.")
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        with pytest.raises(AuthenticationError, match="JAXA_PTREE_USERNAME"):
            transport.login("u", "p")
        assert transport._ftp is None

    def test_download_translates_perm_error_to_file_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """A `550` on `RETR` surfaces as `FileNotFoundError`, no local file left."""
        import ftplib
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        transport.login("u", "p")
        _FakeFTP.retr_raises = ftplib.error_perm("550 not found")
        local = tmp_path / "seg.DAT.bz2"
        with pytest.raises(FileNotFoundError, match="/gone"):
            transport.download_file("/gone", local)
        assert not local.exists()

    def test_download_translates_temp_error_to_file_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """A `450` on `RETR` (retention rejection) also becomes `FileNotFoundError`."""
        import ftplib
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        transport.login("u", "p")
        _FakeFTP.retr_raises = ftplib.error_temp("450 too old")
        local = tmp_path / "seg.DAT.bz2"
        with pytest.raises(FileNotFoundError, match="too old"):
            transport.download_file("/old", local)

    def test_close_falls_back_to_socket_close_on_quit_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When `QUIT` raises, `close()` bare-closes the socket and clears state."""
        import ftplib
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        transport.login("u", "p")
        _FakeFTP.quit_raises = OSError("broken pipe")
        transport.close()
        assert transport._ftp is None

    def test_login_wraps_temp_error_as_connection_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `421 too many users` (error_temp) surfaces as `ConnectionError`, no leak."""
        import ftplib
        _FakeFTP.login_raises = ftplib.error_temp("421 too many users")
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        with pytest.raises(ConnectionError, match="handshake failed"):
            transport.login("u", "p")
        assert transport._ftp is None

    def test_login_wraps_eof_error_as_connection_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An `EOFError` mid-handshake surfaces as `ConnectionError`, no leak."""
        import ftplib
        _FakeFTP.login_raises = EOFError("server closed")
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        with pytest.raises(ConnectionError, match="handshake failed"):
            transport.login("u", "p")
        assert transport._ftp is None

    def test_login_error_message_omits_raw_ftp_reply(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bad-creds error does not echo the raw server reply / username."""
        import ftplib
        _FakeFTP.login_raises = ftplib.error_perm(
            "530 Login incorrect for alice@example.org.",
        )
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        with pytest.raises(AuthenticationError) as excinfo:
            transport.login("alice@example.org", "p")
        assert "alice@example.org" not in str(excinfo.value)

    def test_download_translates_generic_oserror_to_connection_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """`OSError` on `RETR` -> `ConnectionError`, no partial file left."""
        import ftplib
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        transport.login("u", "p")
        _FakeFTP.retr_raises = OSError("connection reset")
        local = tmp_path / "seg.DAT.bz2"
        with pytest.raises(ConnectionError, match="transfer failed"):
            transport.download_file("/some/remote/seg.DAT.bz2", local)
        assert not local.exists()
        partial = local.with_suffix(local.suffix + ".part")
        assert not partial.exists()

    def test_download_writes_via_part_file_rename(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """A successful transfer leaves the final path, not the .part file."""
        import ftplib
        monkeypatch.setattr(ftplib, "FTP", _FakeFTP)
        transport = FtplibTransport()
        transport.login("u", "p")
        local = tmp_path / "seg.DAT.bz2"
        transport.download_file("/some/remote/seg.DAT.bz2", local)
        assert local.exists()
        assert local.read_bytes() == b"payload"
        partial = local.with_suffix(local.suffix + ".part")
        assert not partial.exists()


class TestPtreeTransportProtocol:
    """The `PtreeTransport` typing.Protocol accepts duck-typed fakes."""

    def test_recording_transport_is_a_ptree_transport(self) -> None:
        """`_RecordingTransport` structurally satisfies the protocol."""
        transport: PtreeTransport = _RecordingTransport()
        transport.login("u", "p")
        transport.close()
