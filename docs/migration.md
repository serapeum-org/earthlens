# Migration guide

Breaking changes by release, with the concrete edit each one needs. Releases without breaking changes are
omitted — see the [change log](change-log.md) for the full history.

## 0.21.0 → 0.22.0

One breaking change, and it is the quiet kind: **your code keeps running and writes somewhere else**.

### A qualified `source:topic` key writes to a flattened directory

`EarthLens(data_source="jrc:coastal-forecast").download()` used to derive its default output directory straight
from the key, giving `<output_dir()>/jrc:coastal-forecast/`. A colon cannot appear in a Windows path component,
so that call could not create its directory there at all. The separator is now flattened to `_`:

```python
# 0.21 — on POSIX; on Windows this failed to create the directory
<output_dir()>/jrc:coastal-forecast/

# 0.22
<output_dir()>/jrc_coastal-forecast/
```

Only qualified keys are affected; a bare key such as `chc` is unchanged. Nothing is moved for you, so if you are
on Linux or macOS and already have output under the old colon directory, either move it across or keep passing an
explicit `path=`.

## 0.11.0 → 0.12.0

Five breaking changes. The `OpenAQ` one is the dangerous one: **your code keeps running and quietly means
something different**. The rest fail loudly.

### `OpenAQ(limit=...)` changed meaning

`limit=` used to be the *page size* for the paginated OpenAQ endpoints. It is now a **total row cap**, and the page
size moved to `page_size=`.

Code that passed `limit=1000` to page through results still runs — but now it stops after 1000 rows in total
instead of fetching 1000 rows per page. That is a silent change in the returned data, not an error.

```python
# 0.11 — 1000 rows per page, all pages fetched
lens = EarthLens(data_source="openaq", limit=1000, ...)

# 0.12 — same behaviour
lens = EarthLens(data_source="openaq", page_size=1000, ...)

# 0.12 — a deliberate cap: stop after 1000 rows in total
lens = EarthLens(data_source="openaq", limit=1000, ...)
```

**Action:** grep for `limit=` on any OpenAQ call. If it was there for paging, rename it to `page_size=`.

### An unsupported `temporal_resolution` now raises

Passing a resolution a backend does not support used to fall through and silently download **daily** data. It now
raises.

```python
EarthLens(data_source="chc", temporal_resolution="hourly", ...)   # 0.11: silently daily
                                                                  # 0.12: raises
```

**Action:** none if your resolutions were already valid. If a run suddenly fails, the old behaviour was giving you
daily data under a label that claimed otherwise — check which resolution you actually wanted. Each backend's
supported values are on its reference page.

### `cadence="weekly"` now maps to `7D`

It previously mapped to the pandas `W` alias, which is calendar-anchored (`W-SUN`) and emits **period ends** — so
the first days of the requested window were skipped. `7D` counts seven days forward from the window start.

**Action:** none for correctness — this fixes dropped data. Expect weekly bucket **boundaries** to shift, so
re-generate any cached weekly outputs rather than mixing them with new ones.

### `Catalog.load()` raises `ValueError` on a missing path

All 48 catalogs now report a missing file through the shared loader, which raises `ValueError` rather than
`FileNotFoundError`.

```python
try:
    Catalog.load(path)
except ValueError:        # was: except FileNotFoundError
    ...
```

Pass a `pathlib.Path`, not a `str` — a bare string currently fails with `AttributeError` before the path check
runs.

Related: `earthlens.jaxa.catalog.Catalog.load()` now returns a **fresh instance per call** instead of a shared one.
If you relied on mutating the returned catalog and seeing that change elsewhere, hold your own reference instead.

### Renamed aggregate internals

`aggregate._reduce` → `reduce_time_axis`, and `aggregate._window_groups` → `window_groups`. Both are now public.
Only affects code reaching into the private names.

## 0.10.0 → 0.11.0

### `from earthlens import EarthLens` no longer works

`earthlens` became a **PEP 420 namespace package** split across seven distributions, so the top-level
`__init__.py` — and the re-exports it carried — are gone. The public surface is `earthlens.core`.

```python
# before
from earthlens import EarthLens, download, AggregationConfig

# after
from earthlens.core import EarthLens, download, AggregationConfig
```

Everything that used to be importable from the top level now comes from `earthlens.core`: `EarthLens`,
`download`, `find`, `search`, `sources`, `AggregationConfig`, `aggregate_netcdf`, `__version__`.

Backend subpackages are unchanged — `from earthlens.chc import Catalog`, `from earthlens.gee import Catalog`, and
so on still work exactly as before.

**Action:** a single find-and-replace of `from earthlens import ` → `from earthlens.core import `. Note that
`import earthlens.earthlens` also resolves, but it is the internal module path and not the supported surface — use
`earthlens.core`.
