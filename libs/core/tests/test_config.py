from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from earthlens.base import (
    AbstractDataSource,
    SpatialExtent,
    TemporalExtent,
    to_datetime,
)
from earthlens.config import CACHE_DIR_ENV, cache_dir, set_cache_dir


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Clear any override and the env var so each test starts from the default."""
    set_cache_dir(None)
    monkeypatch.delenv(CACHE_DIR_ENV, raising=False)
    yield
    set_cache_dir(None)


class _Backend(AbstractDataSource):
    """Minimal concrete backend used to observe the resolved output dir."""

    def _initialize(self):
        return None

    def _create_grid(self, lat_lim, lon_lim):
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def download(self, progress_bar: bool = True, **kwargs):
        return []

    def _api(self):
        return []


def _build(**kwargs):
    kwargs.setdefault("start", "2024-01-01")
    kwargs.setdefault("end", "2024-01-02")
    kwargs.setdefault("variables", ["x"])
    kwargs.setdefault("lat_lim", [0.0, 1.0])
    kwargs.setdefault("lon_lim", [0.0, 1.0])
    return _Backend(**kwargs)


class TestCacheDirResolution:
    """cache_dir() honours override, then env var, then the built-in default."""

    def test_default_is_home_dot_earthlens(self):
        """With nothing set it resolves to ~/.earthlens/cache."""
        assert cache_dir() == (Path.home() / ".earthlens" / "cache").resolve()

    def test_env_var_is_used(self, monkeypatch, tmp_path):
        """EARTHLENS_CACHE_DIR is picked up when no override is set."""
        monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path))
        assert cache_dir() == Path(tmp_path).resolve()

    def test_set_cache_dir_beats_env(self, monkeypatch, tmp_path):
        """An explicit override wins over the environment variable."""
        monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path / "from_env"))
        set_cache_dir(tmp_path / "from_call")
        assert cache_dir() == (tmp_path / "from_call").resolve()

    def test_set_cache_dir_none_restores_env(self, monkeypatch, tmp_path):
        """Passing None clears the override and falls back to the env var."""
        monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path))
        set_cache_dir(tmp_path / "other")
        set_cache_dir(None)
        assert cache_dir() == Path(tmp_path).resolve()

    def test_expanduser(self):
        """A leading ~ is expanded when the override is set."""
        set_cache_dir("~/some-earthlens-cache")
        assert cache_dir() == (Path.home() / "some-earthlens-cache").resolve()

    def test_does_not_create_the_directory(self, tmp_path):
        """Resolving the cache dir never creates it on disk."""
        target = tmp_path / "not_yet"
        set_cache_dir(target)
        assert cache_dir() == target.resolve()
        assert not target.exists()


class TestBackendUsesCacheDir:
    """A backend built without path= falls back to the configured cache dir."""

    def test_empty_path_uses_override(self, tmp_path):
        """No path= -> the set_cache_dir() location."""
        set_cache_dir(tmp_path)
        assert _build().root_dir == Path(tmp_path).resolve()

    def test_empty_path_uses_env(self, monkeypatch, tmp_path):
        """No path= and no override -> the env-var location."""
        monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path))
        assert _build().root_dir == Path(tmp_path).resolve()

    def test_explicit_path_wins(self, tmp_path):
        """An explicit path= overrides the configured cache dir."""
        set_cache_dir(tmp_path / "cache")
        explicit = tmp_path / "explicit"
        assert _build(path=str(explicit)).root_dir == explicit.absolute()

    def test_path_alias_matches_root_dir(self, tmp_path):
        """The legacy self.path alias tracks the resolved root_dir."""
        set_cache_dir(tmp_path)
        backend = _build()
        assert backend.path == backend.root_dir
