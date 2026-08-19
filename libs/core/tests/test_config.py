from __future__ import annotations

from pathlib import Path

import pandas as pd
import platformdirs
import pytest

from earthlens.base import (
    AbstractDataSource,
    SpatialExtent,
    TemporalExtent,
    to_datetime,
)
from earthlens.config import (
    CACHE_DIR_ENV,
    OUTPUT_DIR_ENV,
    cache_dir,
    output_dir,
    set_cache_dir,
    set_output_dir,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Clear both overrides and both env vars so each test starts from the default."""
    set_output_dir(None)
    set_cache_dir(None)
    monkeypatch.delenv(OUTPUT_DIR_ENV, raising=False)
    monkeypatch.delenv(CACHE_DIR_ENV, raising=False)
    yield
    set_output_dir(None)
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


class TestOutputDirResolution:
    """output_dir() honours override, then env var, then the built-in default."""

    def test_default_is_home_dot_earthlens_data(self):
        """With nothing set it resolves to ~/.earthlens/data."""
        assert output_dir() == (Path.home() / ".earthlens" / "data").resolve()

    def test_env_var_is_used(self, monkeypatch, tmp_path):
        """EARTHLENS_DATA_DIR is picked up when no override is set."""
        monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path))
        assert output_dir() == Path(tmp_path).resolve()

    def test_override_beats_env(self, monkeypatch, tmp_path):
        """An explicit override wins over the environment variable."""
        monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path / "from_env"))
        set_output_dir(tmp_path / "from_call")
        assert output_dir() == (tmp_path / "from_call").resolve()

    def test_none_restores_env(self, monkeypatch, tmp_path):
        """Passing None clears the override and falls back to the env var."""
        monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path))
        set_output_dir(tmp_path / "other")
        set_output_dir(None)
        assert output_dir() == Path(tmp_path).resolve()

    def test_expanduser(self):
        """A leading ~ is expanded when the override is set."""
        set_output_dir("~/some-earthlens-output")
        assert output_dir() == (Path.home() / "some-earthlens-output").resolve()

    def test_blank_env_var_falls_back_to_default(self, monkeypatch):
        """An env var set to an empty value is ignored rather than used as a path."""
        monkeypatch.setenv(OUTPUT_DIR_ENV, "")
        assert output_dir() == (Path.home() / ".earthlens" / "data").resolve()

    def test_relative_override_is_resolved_absolute(self, tmp_path, monkeypatch):
        """A relative override is resolved against the working directory."""
        monkeypatch.chdir(tmp_path)
        set_output_dir("relative-output")
        resolved = output_dir()
        assert resolved.is_absolute(), f"output_dir() must be absolute, got {resolved}"
        assert resolved == (tmp_path / "relative-output").resolve()

    def test_does_not_create_the_directory(self, tmp_path):
        """Resolving the output dir never creates it on disk."""
        target = tmp_path / "not_yet"
        set_output_dir(target)
        assert output_dir() == target.resolve()
        assert not target.exists()

    def test_follows_a_changed_home(self, monkeypatch, tmp_path):
        """The home-relative default is read per call, not frozen at import."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert output_dir() == (tmp_path / ".earthlens" / "data").resolve(), (
            "the default must follow Path.home(), so a test can redirect it"
        )

    def test_only_none_clears_the_override(self, tmp_path, monkeypatch):
        """An empty string is a directory (the cwd), not a request to clear."""
        monkeypatch.chdir(tmp_path)
        set_output_dir(tmp_path / "somewhere")
        set_output_dir("")
        assert output_dir() == Path(tmp_path).resolve(), (
            'set_output_dir("") should mean the working directory'
        )

    def test_path_object_override_is_not_treated_as_falsy(self, tmp_path, monkeypatch):
        """Path("") behaves the same as "" — a Path is never falsy."""
        monkeypatch.chdir(tmp_path)
        set_output_dir(Path(""))
        assert output_dir() == Path(tmp_path).resolve()


class TestCacheDirResolution:
    """cache_dir() honours override, then EARTHLENS_CACHE, then the user cache dir."""

    def test_default_is_the_platform_user_cache(self):
        """With nothing set it resolves to the per-platform user cache directory."""
        expected = platformdirs.user_cache_dir("earthlens", appauthor=False)
        assert cache_dir() == Path(expected).resolve()

    def test_env_var_is_used(self, monkeypatch, tmp_path):
        """EARTHLENS_CACHE is picked up when no override is set."""
        monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path))
        assert cache_dir() == Path(tmp_path).resolve()

    def test_override_beats_env(self, monkeypatch, tmp_path):
        """An explicit override wins over the environment variable."""
        monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path / "from_env"))
        set_cache_dir(tmp_path / "from_call")
        assert cache_dir() == (tmp_path / "from_call").resolve()

    def test_is_independent_of_the_output_dir(self, tmp_path):
        """Setting one directory does not move the other."""
        set_output_dir(tmp_path / "products")
        set_cache_dir(tmp_path / "intermediates")
        assert output_dir() == (tmp_path / "products").resolve()
        assert cache_dir() == (tmp_path / "intermediates").resolve()

    def test_does_not_create_the_directory(self, tmp_path):
        """Resolving the cache dir never creates it on disk."""
        target = tmp_path / "not_yet"
        set_cache_dir(target)
        assert cache_dir() == target.resolve()
        assert not target.exists()


class TestEndGranularityDefault:
    """The inclusive-end flag has a safe class default."""

    def test_class_default_is_false(self):
        """A backend that never records the flag does not widen its end bound."""
        assert AbstractDataSource._end_is_date_only is False, (
            "the conservative default must exist on the class, not only on "
            "instances that ran _check_input_dates"
        )


class TestBackendUsesOutputDir:
    """A backend built without path= falls back to the configured output dir."""

    def test_omitted_path_uses_override(self, tmp_path):
        """No path= -> the set_output_dir() location."""
        set_output_dir(tmp_path)
        assert _build().root_dir == Path(tmp_path).resolve()

    def test_omitted_path_uses_env(self, monkeypatch, tmp_path):
        """No path= and no override -> the env-var location."""
        monkeypatch.setenv(OUTPUT_DIR_ENV, str(tmp_path))
        assert _build().root_dir == Path(tmp_path).resolve()

    def test_explicit_path_wins(self, tmp_path):
        """An explicit path= overrides the configured output dir."""
        set_output_dir(tmp_path / "configured")
        explicit = tmp_path / "explicit"
        assert _build(path=str(explicit)).root_dir == explicit.absolute()

    def test_blank_path_means_the_working_directory(self, tmp_path, monkeypatch):
        """path="" stays the documented way to ask for the cwd."""
        monkeypatch.chdir(tmp_path)
        set_output_dir(tmp_path / "configured")
        assert _build(path="").root_dir == Path.cwd(), (
            'path="" must keep meaning the working directory'
        )

    def test_whitespace_path_means_the_working_directory(self, tmp_path, monkeypatch):
        """Whitespace is treated the same as an empty string."""
        monkeypatch.chdir(tmp_path)
        set_output_dir(tmp_path / "configured")
        assert _build(path="   ").root_dir == Path.cwd()

    def test_relative_path_is_made_absolute(self, tmp_path, monkeypatch):
        """An explicit relative path is anchored to the working directory."""
        monkeypatch.chdir(tmp_path)
        root = _build(path="sub/dir").root_dir
        assert root.is_absolute(), f"root_dir must be absolute, got {root}"
        assert root == (tmp_path / "sub" / "dir").absolute()

    def test_padded_path_is_stripped(self, tmp_path, monkeypatch):
        """A padded value is stripped, not turned into a space-named directory."""
        monkeypatch.chdir(tmp_path)
        assert _build(path=" out ").root_dir == (tmp_path / "out").absolute(), (
            "surrounding whitespace must not become part of the directory name"
        )

    def test_path_object_is_accepted(self, tmp_path):
        """path= may be a Path, not only a string."""
        explicit = tmp_path / "explicit"
        assert _build(path=explicit).root_dir == explicit.absolute()

    def test_path_alias_matches_root_dir(self, tmp_path):
        """The legacy self.path alias tracks the resolved root_dir."""
        set_output_dir(tmp_path)
        backend = _build()
        assert backend.path == backend.root_dir

    def test_resolution_is_read_at_construction(self, tmp_path):
        """The output dir is captured when the backend is built, not on download."""
        set_output_dir(tmp_path / "first")
        backend = _build()
        set_output_dir(tmp_path / "second")
        assert backend.root_dir == (tmp_path / "first").resolve(), (
            "a later set_output_dir() must not retroactively move an existing backend"
        )
