"""Unit tests for the MSWEP transport hardening (`C9` / `G9`)."""

from __future__ import annotations

import datetime as dt
import http.client
import warnings

import pytest

from earthlens.biodiversity import LicenseWarning
from earthlens.mswep.backend import MSWEP
from earthlens.mswep.drive import (
    BACKOFF_BASE,
    DownloadQuotaExceededError,
    RateLimitedError,
    classify_http_error,
    download_media,
)

pytestmark = [pytest.mark.mswep, pytest.mark.unit]


class FakeResp:
    """Minimal stand-in for an `httplib2.Response` carrying a status."""

    def __init__(self, status):
        """Store the HTTP status."""
        self.status = status


class FakeHttpError(Exception):
    """Stand-in for `googleapiclient.errors.HttpError`."""

    def __init__(self, status, message):
        """Build an error with a status and a Drive-style reason body."""
        super().__init__(message)
        self.resp = FakeResp(status)


def _quiet(source):
    """Download while suppressing the always-emitted licence warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LicenseWarning)
        return source.download(progress_bar=False)


class TestClassifyHttpError:
    """Mapping Drive failures onto typed errors."""

    def test_quota_reason_becomes_quota_error(self):
        """A `downloadQuotaExceeded` body is classified as a quota refusal."""
        exc = FakeHttpError(403, "downloadQuotaExceeded: quota gone")
        assert isinstance(classify_http_error(exc), DownloadQuotaExceededError)

    def test_plain_403_is_a_rate_limit(self):
        """A 403 without a quota reason is throttling, which retries clear."""
        exc = FakeHttpError(403, "userRateLimitExceeded")
        assert isinstance(classify_http_error(exc), RateLimitedError)

    def test_429_is_a_rate_limit(self):
        """Too Many Requests is throttling."""
        assert isinstance(
            classify_http_error(FakeHttpError(429, "slow")), RateLimitedError
        )

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_rate_limited(self, status):
        """Transient 5xx responses are retried like throttling."""
        exc = FakeHttpError(status, "backend error")
        assert isinstance(classify_http_error(exc), RateLimitedError)

    def test_404_passes_through_unchanged(self):
        """An unrelated failure keeps its own type and traceback."""
        exc = FakeHttpError(404, "not found")
        assert classify_http_error(exc) is exc

    def test_non_http_error_passes_through(self):
        """An error with no `resp` is not misclassified."""
        exc = ValueError("boom")
        assert classify_http_error(exc) is exc

    def test_connection_error_is_retryable(self):
        """A bare connection drop (no HTTP status) is treated as transient."""
        assert isinstance(
            classify_http_error(ConnectionError("reset")), RateLimitedError
        )

    def test_incomplete_read_is_retryable(self):
        """A truncated streaming response is transient and worth a retry."""
        assert isinstance(
            classify_http_error(http.client.IncompleteRead(b"")), RateLimitedError
        )

    def test_disk_full_oserror_passes_through(self):
        """A non-connection OSError (e.g. disk full) is not retried."""
        exc = OSError("No space left on device")
        assert classify_http_error(exc) is exc

    def test_quota_message_names_the_24_hour_reset(self):
        """The message tells the user waiting ~24 h is the fix, not retrying."""
        exc = FakeHttpError(403, "downloadQuotaExceeded")
        assert "24 hours" in str(classify_http_error(exc))

    def test_quota_message_points_at_rclone(self):
        """The message routes bulk users to rclone."""
        exc = FakeHttpError(403, "downloadQuotaExceeded")
        assert "rclone" in str(classify_http_error(exc))


class TestDownloadRetries:
    """Back-off applies to throttling but never to a quota refusal."""

    def test_quota_error_is_not_retried(self, drive, tmp_path):
        """Waiting cannot clear a quota refusal, so it raises at once."""
        file_id = drive.add_file("g.nc", "F")
        drive.media_errors[file_id] = FakeHttpError(403, "downloadQuotaExceeded")
        slept = []
        with pytest.raises(DownloadQuotaExceededError):
            download_media(drive, file_id, tmp_path / "g.nc", sleep=slept.append)
        assert slept == []
        assert drive.media_calls == [file_id]

    def test_throttling_is_retried_then_raises(self, drive, tmp_path):
        """Throttling retries to the budget, then surfaces as a rate limit."""
        file_id = drive.add_file("g.nc", "F")
        drive.media_errors[file_id] = FakeHttpError(429, "rate limited")
        slept = []
        with pytest.raises(RateLimitedError):
            download_media(
                drive, file_id, tmp_path / "g.nc", max_retries=3, sleep=slept.append
            )
        assert len(drive.media_calls) == 3

    def test_backoff_grows_exponentially(self, drive, tmp_path):
        """Each retry waits longer than the last."""
        file_id = drive.add_file("g.nc", "F")
        drive.media_errors[file_id] = FakeHttpError(503, "unavailable")
        slept = []
        with pytest.raises(RateLimitedError):
            download_media(
                drive, file_id, tmp_path / "g.nc", max_retries=4, sleep=slept.append
            )
        assert slept == [BACKOFF_BASE**0, BACKOFF_BASE**1, BACKOFF_BASE**2]

    def test_unrelated_error_is_not_retried(self, drive, tmp_path):
        """A 404 is a bug, not congestion, so it fails immediately."""
        file_id = drive.add_file("g.nc", "F")
        drive.media_errors[file_id] = FakeHttpError(404, "gone")
        with pytest.raises(FakeHttpError):
            download_media(drive, file_id, tmp_path / "g.nc", sleep=lambda _: None)
        assert len(drive.media_calls) == 1

    def test_connection_error_is_retried_then_raises(self, drive, tmp_path):
        """A statusless connection drop now retries to the budget, not attempt one."""
        file_id = drive.add_file("g.nc", "F")
        drive.media_errors[file_id] = ConnectionError("connection reset by peer")
        with pytest.raises(RateLimitedError):
            download_media(
                drive, file_id, tmp_path / "g.nc", max_retries=3, sleep=lambda _: None
            )
        assert len(drive.media_calls) == 3

    def test_no_partial_file_survives_a_failure(self, drive, tmp_path):
        """A failed transfer leaves nothing that could look cached."""
        file_id = drive.add_file("g.nc", "F")
        drive.media_errors[file_id] = FakeHttpError(403, "downloadQuotaExceeded")
        with pytest.raises(DownloadQuotaExceededError):
            download_media(drive, file_id, tmp_path / "g.nc", sleep=lambda _: None)
        assert list(tmp_path.iterdir()) == []

    def test_quota_error_is_not_a_missing_granule(self, share, tmp_path):
        """A quota refusal propagates instead of being skipped as a gap."""
        file_id = share.path_id("MSWEP_V315/Past/Daily")
        target = next(
            obj["id"]
            for obj in share.objects
            if obj["name"] == "2020116.nc" and obj["parent"] == file_id
        )
        share.media_errors[target] = FakeHttpError(403, "downloadQuotaExceeded")
        source = MSWEP(
            start="2020-04-25",
            end="2020-04-25",
            temporal_resolution="daily",
            folder_id=share.path_id("MSWEP_V315"),
            service=share,
            path=tmp_path,
        )
        with pytest.raises(DownloadQuotaExceededError):
            _quiet(source)


class TestNrtRevisionWindow:
    """NRT granules inside the revision window are always re-fetched."""

    @pytest.fixture
    def source(self, share, tmp_path, monkeypatch):
        """An NRT request pinned to a fixed 'now'."""
        monkeypatch.setattr(
            MSWEP,
            "_now",
            staticmethod(lambda: dt.datetime(2025, 1, 5, tzinfo=dt.timezone.utc)),
        )
        return MSWEP(
            start="2025-01-01",
            end="2025-01-01",
            temporal_resolution="daily",
            variant="NRT",
            folder_id=share.path_id("MSWEP_V315"),
            service=share,
            path=tmp_path,
        )

    def test_recent_nrt_granule_is_under_revision(self, source):
        """A granule four days old is still being rewritten upstream."""
        stamp = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        assert source.is_under_revision(stamp, "MSWEP_V315/NRT/Daily")

    def test_old_nrt_granule_is_settled(self, source):
        """Past the ten-day window an NRT granule is stable."""
        stamp = dt.datetime(2024, 12, 1, tzinfo=dt.timezone.utc)
        assert not source.is_under_revision(stamp, "MSWEP_V315/NRT/Daily")

    def test_past_variant_is_never_under_revision(self, source):
        """The historical record is stable regardless of age."""
        stamp = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        assert not source.is_under_revision(stamp, "MSWEP_V315/Past/Daily")

    def test_naive_timestamp_is_treated_as_utc(self, source):
        """A tz-naive stamp does not crash the comparison."""
        assert source.is_under_revision(dt.datetime(2025, 1, 1), "MSWEP_V315/NRT/Daily")

    def test_revised_granule_is_redownloaded(self, source, share):
        """An existing NRT file inside the window is fetched again."""
        _quiet(source)
        first = len(share.media_calls)
        _quiet(source)
        assert len(share.media_calls) == first * 2

    def test_settled_granule_is_reused(self, share, tmp_path, monkeypatch):
        """Outside the window an existing file is not re-downloaded."""
        monkeypatch.setattr(
            MSWEP,
            "_now",
            staticmethod(lambda: dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)),
        )
        kwargs = dict(
            start="2025-01-01",
            end="2025-01-01",
            temporal_resolution="daily",
            variant="NRT",
            folder_id=share.path_id("MSWEP_V315"),
            service=share,
            path=tmp_path,
        )
        _quiet(MSWEP(**kwargs))
        first = len(share.media_calls)
        paths = _quiet(MSWEP(**kwargs))
        assert len(share.media_calls) == first
        assert paths and paths[0].exists()
