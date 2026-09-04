"""Lock-in for M1: per-date FTP failures don't abort the rest of a CHIRPS batch."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from earthlens.chc import CHIRPS
from earthlens.chc import backend as chc_backend

pytestmark = [pytest.mark.chc]


def _build_chirps(tmp_path: Path) -> CHIRPS:
    """Build a CHIRPS backend pinned to a 5-day window for the failure scenario."""
    return CHIRPS(
        variables=["precipitation"],
        temporal_resolution="daily",
        start="2020-01-01",
        end="2020-01-05",
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )


class _FakeFtp:
    """Stand-in for the batch's shared FTP session.

    `_fetch_dates_sequential` opens one session up front and hands it to every
    `_api` call, so stubbing `_api` alone still leaves a real anonymous login
    against `data.chc.ucsb.edu` in the path -- which turns these unit tests into
    live-network tests. Both cleanup helpers are no-ops here.
    """

    def quit(self) -> None:
        """Close the session."""

    def close(self) -> None:
        """Drop the session."""


class _CountingApiSpy:
    """Replace `_api` with a callable that fails the 3rd date and counts calls."""

    def __init__(self):
        self.dates_seen: list[pd.Timestamp] = []
        self.raises_on: set[int] = {2}  # 0-indexed -> the 3rd date

    def __call__(self, ds_key, dataset, var, date, ftp=None):  # noqa: D401
        idx = len(self.dates_seen)
        self.dates_seen.append(date)
        if idx in self.raises_on:
            raise RuntimeError(f"synthetic FTP transient on date #{idx}")
        return None


class TestPerDateResilience:
    """`_download_dataset` keeps going past a per-date exception (M1)."""

    def test_sequential_path_continues_past_failed_date(
        self, tmp_path: Path, monkeypatch
    ):
        """A 5-day batch where the 3rd date raises still attempts dates 4 and 5."""
        chirps = _build_chirps(tmp_path)
        spy = _CountingApiSpy()
        monkeypatch.setattr(chirps, "_api", spy)
        monkeypatch.setattr(chc_backend, "_open_ftp", _FakeFtp)
        # Run the per-dataset loop directly so the test doesn't depend on
        # `download()`'s outer try/except.
        ds = chirps.catalog.datasets["global-daily"]
        var = ds.variables["precipitation"]
        chirps._download_dataset(
            "global-daily", ds, var, progress_bar=False, cores=None
        )
        assert len(spy.dates_seen) == 5, (
            "the per-date loop must visit ALL 5 dates after M1, "
            f"got {len(spy.dates_seen)}: {[d.date() for d in spy.dates_seen]}"
        )

    def test_sequential_path_logs_failed_date_summary(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """After M1, the failure summary log names the date count and a sample."""
        import logging

        chirps = _build_chirps(tmp_path)
        spy = _CountingApiSpy()
        monkeypatch.setattr(chirps, "_api", spy)
        monkeypatch.setattr(chc_backend, "_open_ftp", _FakeFtp)
        ds = chirps.catalog.datasets["global-daily"]
        var = ds.variables["precipitation"]
        # Bridge loguru to the std `logging` records caplog watches.
        from loguru import logger as _loguru_logger

        handler_id = _loguru_logger.add(
            lambda msg: logging.getLogger().warning(msg.record["message"]),
            level="WARNING",
        )
        try:
            with caplog.at_level(logging.WARNING):
                chirps._download_dataset(
                    "global-daily", ds, var, progress_bar=False, cores=None
                )
        finally:
            _loguru_logger.remove(handler_id)
        joined = "\n".join(rec.message for rec in caplog.records)
        assert "1/5 dates" in joined, joined
        assert "RuntimeError" in joined, joined

    def test_api_or_capture_returns_path_on_success(self, tmp_path: Path):
        """`_api_or_capture` returns `(path, None)` when `_api` succeeds."""
        chirps = _build_chirps(tmp_path)
        written = tmp_path / "out.tif"
        chirps._api = lambda *a, **kw: written  # type: ignore[assignment]
        ds = chirps.catalog.datasets["global-daily"]
        var = ds.variables["precipitation"]
        result = chirps._api_or_capture(
            "global-daily", ds, var, pd.Timestamp("2020-01-01")
        )
        assert result == (written, None)

    def test_api_or_capture_returns_error_on_failure(self, tmp_path: Path):
        """`_api_or_capture` returns `(None, (date, exc))` when `_api` raises."""
        chirps = _build_chirps(tmp_path)

        def _boom(*a, **kw):
            raise RuntimeError("synthetic")

        chirps._api = _boom  # type: ignore[assignment]
        ds = chirps.catalog.datasets["global-daily"]
        var = ds.variables["precipitation"]
        date = pd.Timestamp("2020-01-01")
        path, err = chirps._api_or_capture("global-daily", ds, var, date)
        assert path is None
        captured_date, captured_exc = err
        assert captured_date == date
        assert isinstance(captured_exc, RuntimeError)
        assert "synthetic" in str(captured_exc)
