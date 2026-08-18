"""Unit tests for the NWP provider-agnostic helpers."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.nwp._helpers import (
    cog_name,
    ensure_dir,
    enumerate_cycles,
    grib_name,
    valid_time,
)

pytestmark = [pytest.mark.nwp, pytest.mark.unit]


class TestEnumerateCycles:
    """Tests for enumerate_cycles."""

    def test_single_day_four_cycles(self):
        """One day with four run hours yields four ascending datetimes."""
        out = enumerate_cycles(
            dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1), [0, 6, 12, 18]
        )
        assert out == [
            dt.datetime(2024, 6, 1, 0),
            dt.datetime(2024, 6, 1, 6),
            dt.datetime(2024, 6, 1, 12),
            dt.datetime(2024, 6, 1, 18),
        ], out

    def test_multi_day_count(self):
        """Three days with two run hours yield six cycles."""
        out = enumerate_cycles(
            dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 3), [0, 12]
        )
        assert len(out) == 6, out

    def test_uses_calendar_date_of_bounds(self):
        """A datetime bound is reduced to its calendar day for enumeration."""
        out = enumerate_cycles(
            dt.datetime(2024, 6, 1, 9, 30), dt.datetime(2024, 6, 1, 23), [0]
        )
        assert out == [dt.datetime(2024, 6, 1, 0)], out

    def test_dedupes_and_sorts_hours(self):
        """Duplicate / unsorted run hours are de-duplicated and ordered."""
        out = enumerate_cycles(
            dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1), [12, 0, 12]
        )
        assert out == [dt.datetime(2024, 6, 1, 0), dt.datetime(2024, 6, 1, 12)], out

    def test_inverted_range_raises(self):
        """A start after end raises ValueError."""
        with pytest.raises(ValueError, match="after end"):
            enumerate_cycles(dt.datetime(2024, 6, 2), dt.datetime(2024, 6, 1), [0])

    @pytest.mark.parametrize("hour", [-1, 24, 99])
    def test_out_of_range_hour_raises(self, hour):
        """A run hour outside [0, 23] raises ValueError."""
        with pytest.raises(ValueError, match="outside"):
            enumerate_cycles(dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1), [hour])


class TestNameHelpers:
    """Tests for cog_name, grib_name, and valid_time."""

    def test_cog_name(self):
        """The COG name embeds the model key, cycle stamp, and f-step."""
        name = cog_name("gfs", dt.datetime(2024, 6, 1, 12), 24)
        assert name == "gfs_2024060112_f024.tif", name

    def test_grib_name(self):
        """The GRIB name mirrors the COG name with a .grib2 suffix."""
        name = grib_name("icon", dt.datetime(2024, 6, 1, 0), 3)
        assert name == "icon_2024060100_f003.grib2", name

    def test_valid_time(self):
        """valid_time adds the step hours to the cycle."""
        assert valid_time(dt.datetime(2024, 6, 1, 0), 30) == dt.datetime(2024, 6, 2, 6)


class TestEnsureDir:
    """Tests for ensure_dir."""

    def test_creates_and_returns_absolute(self, tmp_path):
        """A missing directory is created and returned as an absolute Path."""
        target = tmp_path / "a" / "b"
        result = ensure_dir(target)
        assert result.exists() and result.is_absolute(), result

    def test_idempotent_on_existing(self, tmp_path):
        """Calling on an existing directory is a no-op that still returns it."""
        result = ensure_dir(tmp_path)
        assert result == tmp_path.absolute(), result


class TestWindowLabels:
    """Tests for window_labels (aggregation-window labelling)."""

    def test_empty_freq_bin_skipped(self):
        """A freq window containing no input times is skipped, not labelled."""
        import datetime as dt

        from earthlens.nwp._helpers import window_labels

        times = [dt.datetime(2026, 1, 1, 0), dt.datetime(2026, 1, 1, 3)]
        # hourly bins -> the 01:00 / 02:00 windows are empty and skipped
        assert window_labels(times, "1h") == ["2026010100", "2026010103"]


_NOAA_IDX = (
    "1:0:d=2024060100:HGT:1000 mb:anl\n"
    "2:8192:d=2024060100:TMP:2 m above ground:anl\n"
    "3:16384:d=2024060100:UGRD:10 m above ground:anl\n"
)

_ECMWF_INDEX = (
    '{"_offset":0,"_length":256,"param":"2t","levelist":"sfc","step":0}\n'
    '{"_offset":256,"_length":300,"param":"tp","step":0}\n'
)


def _counting_downloader(body):
    """Build a downloader closure that writes `body` and counts calls."""
    calls = []

    def _dl(url, path):
        calls.append(url)
        path.write_text(body, encoding="utf-8")

    return _dl, calls


class TestParseIdx:
    """Tests for `_parse_idx` and the NOAA / ECMWF dispatchers."""

    def test_noaa_text_parses_to_canonical_frame(self):
        """NOAA `:`-separated input returns the canonical columns."""
        from earthlens.nwp._helpers import _IDX_COLUMNS, _parse_idx

        frame = _parse_idx(_NOAA_IDX)
        assert tuple(frame.columns) == _IDX_COLUMNS
        assert len(frame) == 3
        assert int(frame.loc[0, "offset"]) == 0
        assert frame.loc[0, "var"] == "HGT"
        assert int(frame.loc[0, "length"]) == 8192
        assert int(frame.loc[2, "length"]) == -1

    def test_ecmwf_json_parses_to_canonical_frame(self):
        """ECMWF newline-JSON input returns the canonical columns."""
        from earthlens.nwp._helpers import _IDX_COLUMNS, _parse_idx

        frame = _parse_idx(_ECMWF_INDEX)
        assert tuple(frame.columns) == _IDX_COLUMNS
        assert len(frame) == 2
        assert int(frame.loc[1, "offset"]) == 256

    def test_both_shapes_return_same_columns(self):
        """NOAA and ECMWF parsers collapse to the same frame shape."""
        from earthlens.nwp._helpers import _parse_idx

        assert list(_parse_idx(_NOAA_IDX).columns) == list(
            _parse_idx(_ECMWF_INDEX).columns
        )

    def test_empty_input_returns_empty_frame(self):
        """Whitespace-only input gives the empty canonical frame."""
        from earthlens.nwp._helpers import _IDX_COLUMNS, _parse_idx

        frame = _parse_idx("   \n\n  ")
        assert tuple(frame.columns) == _IDX_COLUMNS
        assert frame.empty

    def test_noaa_skips_malformed_lines(self):
        """A truncated NOAA line is dropped, not raised."""
        from earthlens.nwp._helpers import _parse_idx

        body = "1:0:d=:HGT:sfc:anl\nnonsense\n2:100:d=:TMP:sfc:anl\n"
        assert len(_parse_idx(body)) == 2

    def test_ecmwf_skips_malformed_json(self):
        """A malformed ECMWF line is dropped, not raised."""
        from earthlens.nwp._helpers import _parse_idx

        body = '{"_offset":0,"_length":100,"param":"2t"}\n{broken json\n'
        assert len(_parse_idx(body)) == 1

    def test_ecmwf_non_numeric_step_dropped(self):
        """A JSON line whose `step` is not int-castable is dropped, not raised."""
        from earthlens.nwp._helpers import _parse_idx

        body = (
            '{"_offset":0,"_length":100,"param":"2t","step":"nope"}\n'
            '{"_offset":100,"_length":50,"param":"tp","step":3}\n'
        )
        frame = _parse_idx(body)
        assert len(frame) == 1
        assert list(frame["step"]) == [3]

    def test_noaa_short_line_dropped(self):
        """A NOAA line with fewer than six colon-separated fields is dropped."""
        from earthlens.nwp._helpers import _parse_idx

        body = "1:0:d=:HGT:sfc\n2:8192:d=:TMP:2 m above ground:anl\n"
        assert len(_parse_idx(body)) == 1

    def test_noaa_non_numeric_msg_id_dropped(self):
        """A NOAA line with a non-numeric msg_id or offset is dropped."""
        from earthlens.nwp._helpers import _parse_idx

        body = "abc:0:d=:HGT:sfc:anl\n2:8192:d=:TMP:sfc:anl\n"
        assert len(_parse_idx(body)) == 1

    def test_noaa_all_bad_returns_empty(self):
        """All-malformed NOAA body collapses to the empty canonical frame."""
        from earthlens.nwp._helpers import _IDX_COLUMNS, _parse_idx

        frame = _parse_idx("only one field\nshort:line\n")
        assert tuple(frame.columns) == _IDX_COLUMNS
        assert frame.empty

    def test_ecmwf_non_numeric_offset_dropped(self):
        """An ECMWF JSON line with a non-numeric `_offset` / `_length` is dropped."""
        from earthlens.nwp._helpers import _parse_idx

        body = (
            '{"_offset":"nope","_length":"nope","param":"2t"}\n'
            '{"_offset":256,"_length":100,"param":"tp"}\n'
        )
        assert len(_parse_idx(body)) == 1

    def test_ecmwf_all_bad_returns_empty(self):
        """All-malformed ECMWF body collapses to the empty canonical frame."""
        from earthlens.nwp._helpers import _IDX_COLUMNS, _parse_idx

        frame = _parse_idx("{not json\n{still not\n")
        assert tuple(frame.columns) == _IDX_COLUMNS
        assert frame.empty

    def test_blank_lines_skipped_in_both_shapes(self):
        """Embedded blank lines in either shape are skipped, not counted."""
        from earthlens.nwp._helpers import _parse_idx

        noaa = "1:0:d=:HGT:sfc:anl\n\n2:8192:d=:TMP:sfc:anl\n"
        ecmwf = '{"_offset":0,"_length":100,"param":"2t"}\n\n{"_offset":100,"_length":50,"param":"tp"}\n'
        assert len(_parse_idx(noaa)) == 2
        assert len(_parse_idx(ecmwf)) == 2

    def test_noaa_step_parses_to_int(self):
        """NOAA forecast field is parsed to int (anl -> 0, N hour fcst -> N)."""
        from earthlens.nwp._helpers import _parse_idx

        body = (
            "1:0:d=:HGT:sfc:anl\n"
            "2:100:d=:TMP:sfc:6 hour fcst\n"
            "3:200:d=:APCP:sfc:0-3 hour acc fcst\n"
        )
        frame = _parse_idx(body)
        steps = list(frame["step"])
        assert steps == [0, 6, 0]
        assert all(isinstance(s, int) for s in steps)

    def test_noaa_unknown_step_token_marked(self):
        """An unrecognised forecast field maps to -1 (explicit sentinel)."""
        from earthlens.nwp._helpers import _parse_idx_noaa

        body = "1:0:d=:HGT:sfc:wat\n"
        assert int(_parse_idx_noaa(body).loc[0, "step"]) == -1

    def test_ecmwf_step_parses_to_int(self):
        """ECMWF JSON `step` is coerced to int — same dtype as the NOAA column."""
        from earthlens.nwp._helpers import _parse_idx

        body = (
            '{"_offset":0,"_length":100,"param":"2t","step":0}\n'
            '{"_offset":100,"_length":50,"param":"tp","step":6}\n'
        )
        frame = _parse_idx(body)
        assert list(frame["step"]) == [0, 6]

    def test_ecmwf_missing_length_is_dropped(self):
        """An ECMWF entry without `_length` is skipped, not flagged with length=-1."""
        from earthlens.nwp._helpers import _parse_idx

        body = (
            '{"_offset":0,"_length":100,"param":"2t"}\n'
            '{"_offset":100,"param":"tp","step":0}\n'
        )
        frame = _parse_idx(body)
        assert len(frame) == 1
        # `-1` is reserved for the NOAA tail row; ECMWF must never emit it.
        assert -1 not in frame["length"].tolist()

    def test_noaa_duplicate_offsets_are_deduped(self):
        """A duplicate offset does not collapse a row to length=0."""
        from earthlens.nwp._helpers import _parse_idx

        body = "1:0:d=:HGT:sfc:anl\n2:8192:d=:TMP:sfc:anl\n3:8192:d=:UGRD:sfc:anl\n"
        frame = _parse_idx(body)
        # Only the first row at offset 8192 is kept; the dup is dropped.
        assert len(frame) == 2
        assert 0 not in frame["length"].tolist()


class TestGetIdx:
    """Tests for the cached `.idx` fetcher."""

    @pytest.fixture(autouse=True)
    def _cache_under_tmp(self, monkeypatch, tmp_path):
        """Redirect the cache root under the per-test tmp dir."""
        from earthlens.nwp import _helpers as helpers

        monkeypatch.setattr(helpers, "_idx_cache_root", lambda: tmp_path / "idx")
        monkeypatch.delenv("EARTHLENS_NWP_IDX_TTL", raising=False)

    def test_first_call_downloads_and_caches(self):
        """The first fetch writes the cache file and parses it."""
        from earthlens.nwp._helpers import _idx_cache_path, get_idx

        dl, calls = _counting_downloader(_NOAA_IDX)
        frame = get_idx("https://example.test/gfs.idx", dl)
        assert len(calls) == 1
        assert _idx_cache_path("https://example.test/gfs.idx").exists()
        assert len(frame) == 3

    def test_second_call_within_ttl_hits_cache(self):
        """A second fetch within TTL reuses the cached body, no extra download."""
        from earthlens.nwp._helpers import get_idx

        dl, calls = _counting_downloader(_NOAA_IDX)
        url = "https://example.test/cached.idx"
        first = get_idx(url, dl)
        second = get_idx(url, dl)
        assert len(calls) == 1
        assert (first.columns == second.columns).all()

    def test_past_ttl_refetches(self):
        """Aging the cache file past the TTL forces a re-download."""
        import os

        from earthlens.nwp._helpers import _idx_cache_path, get_idx

        dl, calls = _counting_downloader(_NOAA_IDX)
        url = "https://example.test/expired.idx"
        get_idx(url, dl, ttl=10.0)
        path = _idx_cache_path(url)
        ancient = path.stat().st_mtime - 1000.0
        os.utime(path, (ancient, ancient))
        get_idx(url, dl, ttl=10.0)
        assert len(calls) == 2

    def test_corrupt_cache_refetches(self):
        """An empty / unparseable cached body is treated as a miss."""
        from earthlens.nwp._helpers import _idx_cache_path, get_idx

        url = "https://example.test/corrupt.idx"
        path = _idx_cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        dl, calls = _counting_downloader(_NOAA_IDX)
        frame = get_idx(url, dl)
        assert len(calls) == 1
        assert len(frame) == 3

    def test_env_var_overrides_default_ttl(self, monkeypatch):
        """`EARTHLENS_NWP_IDX_TTL` overrides the 24 h default when no kwarg is given."""
        import os

        from earthlens.nwp._helpers import _idx_cache_path, get_idx

        url = "https://example.test/env-ttl.idx"
        dl, calls = _counting_downloader(_NOAA_IDX)
        get_idx(url, dl)
        monkeypatch.setenv("EARTHLENS_NWP_IDX_TTL", "1")
        path = _idx_cache_path(url)
        ancient = path.stat().st_mtime - 10.0
        os.utime(path, (ancient, ancient))
        get_idx(url, dl)
        assert len(calls) == 2

    def test_explicit_ttl_wins_over_env(self, monkeypatch):
        """The `ttl=` kwarg takes precedence over the env variable."""
        from earthlens.nwp._helpers import get_idx

        dl, calls = _counting_downloader(_NOAA_IDX)
        monkeypatch.setenv("EARTHLENS_NWP_IDX_TTL", "1")
        url = "https://example.test/kwarg-wins.idx"
        get_idx(url, dl, ttl=10_000.0)
        get_idx(url, dl, ttl=10_000.0)
        assert len(calls) == 1

    def test_failing_downloader_preserves_existing_cache(self):
        """A downloader exception leaves the previous good cache untouched."""
        from earthlens.nwp._helpers import _idx_cache_path, get_idx

        url = "https://example.test/atomic.idx"
        path = _idx_cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_NOAA_IDX, encoding="utf-8")
        good_bytes = path.read_bytes()

        def failing(url, tmp_path):
            tmp_path.write_text("partial body", encoding="utf-8")
            raise RuntimeError("network down")

        with pytest.raises(RuntimeError, match="network down"):
            get_idx(url, failing, ttl=0.0)
        # The cache survives unchanged, and the failed attempt left no
        # temp shadowing it. Scoped to this cache key's own temps rather
        # than "the directory is empty": the contract is that a failure
        # never strands a partial write for *this* entry, and asserting a
        # pristine directory also fails on anything else sharing the dir.
        assert path.read_bytes() == good_bytes
        leftovers = [p.name for p in path.parent.glob(f"{path.name}.*")]
        assert leftovers == [], f"stray partial writes left behind: {leftovers}"

    def test_stat_race_refetches(self, monkeypatch):
        """A cache file whose `stat()` raises `OSError` falls through to a refetch."""
        from earthlens.nwp._helpers import _idx_cache_path, get_idx

        url = "https://example.test/race.idx"
        path = _idx_cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_NOAA_IDX, encoding="utf-8")

        real_stat = type(path).stat

        def _flaky_stat(self, *args, **kwargs):
            if self == path:
                raise OSError("simulated race")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(type(path), "stat", _flaky_stat)
        dl, calls = _counting_downloader(_NOAA_IDX)
        frame = get_idx(url, dl)
        assert len(calls) == 1
        assert len(frame) == 3


class TestIdxCachePath:
    """Tests for the cache-key path resolution."""

    def test_cache_key_is_stable_per_url(self):
        """The same URL always maps to the same on-disk filename."""
        from earthlens.nwp._helpers import _idx_cache_path

        url = "https://example.test/abc.idx"
        assert _idx_cache_path(url) == _idx_cache_path(url)

    def test_different_urls_map_to_different_files(self):
        """Two URLs do not share the same cache file."""
        from earthlens.nwp._helpers import _idx_cache_path

        assert _idx_cache_path("https://a/x.idx") != _idx_cache_path("https://a/y.idx")

    def test_cache_root_under_the_shared_cache_dir(self):
        """The default cache root hangs off the shared earthlens cache directory."""
        from earthlens.config import cache_dir
        from earthlens.nwp._helpers import _idx_cache_root

        root = _idx_cache_root()
        assert root == cache_dir() / "nwp" / "idx", f"got {root}"
        assert root.name == "idx"


class TestResolveIdxTtl:
    """Tests for the TTL precedence in `_resolve_idx_ttl`."""

    def test_kwarg_wins(self, monkeypatch):
        """An explicit positional TTL wins over the env override."""
        from earthlens.nwp._helpers import _resolve_idx_ttl

        monkeypatch.setenv("EARTHLENS_NWP_IDX_TTL", "60")
        assert _resolve_idx_ttl(7.0) == 7.0

    def test_env_used_when_no_kwarg(self, monkeypatch):
        """`EARTHLENS_NWP_IDX_TTL` is used when no kwarg is given."""
        from earthlens.nwp._helpers import _resolve_idx_ttl

        monkeypatch.setenv("EARTHLENS_NWP_IDX_TTL", "60")
        assert _resolve_idx_ttl(None) == 60.0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        """A non-numeric env var falls back to the 24 h default."""
        from earthlens.nwp._helpers import _DEFAULT_IDX_TTL, _resolve_idx_ttl

        monkeypatch.setenv("EARTHLENS_NWP_IDX_TTL", "abc")
        assert _resolve_idx_ttl(None) == float(_DEFAULT_IDX_TTL)

    def test_default_when_nothing_set(self, monkeypatch):
        """With no kwarg and no env, the 24 h default holds."""
        from earthlens.nwp._helpers import _DEFAULT_IDX_TTL, _resolve_idx_ttl

        monkeypatch.delenv("EARTHLENS_NWP_IDX_TTL", raising=False)
        assert _resolve_idx_ttl(None) == float(_DEFAULT_IDX_TTL)
