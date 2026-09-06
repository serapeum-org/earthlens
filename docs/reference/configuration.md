# Configuration

earthlens writes two different kinds of file, and they go to two different places.

| | What lands there | Set it with | Environment variable | Default |
|---|---|---|---|---|
| **Output** | the products you asked for | `set_output_dir()` | `EARTHLENS_DATA_DIR` | `~/.earthlens/data` |
| **Cache** | regenerable intermediates | `set_cache_dir()` | `EARTHLENS_CACHE` | the per-platform user cache directory |

Keeping them apart matters: a cleanup script that deletes anything called a cache must not take your requested
products with it.

## Pointing earthlens at one location

```python
from earthlens.core import EarthLens, set_output_dir, set_cache_dir

set_output_dir("/data/earthlens")        # or: export EARTHLENS_DATA_DIR=/data/earthlens
set_cache_dir("/data/earthlens-cache")   # or: export EARTHLENS_CACHE=/data/earthlens-cache

EarthLens(data_source="chc", variables=["precipitation"],
          start="2009-01-01", end="2009-01-02").download()
# writes under /data/earthlens/chc
```

Resolution order for each directory, highest priority first:

1. the `set_*_dir()` override;
2. the environment variable;
3. the built-in default.

Both are process-wide with no locking. Set them once at process start, before constructing any backend: a backend
captures its output directory at construction, so changing the setting mid-run splits output across two locations
rather than raising.

## Where a download goes

`path=` on a backend (or on the `EarthLens` facade) always wins:

| You pass | It writes to |
|---|---|
| `path="out"` | `./out` — a relative path is anchored to the working directory |
| `path="/data/run"` | `/data/run` |
| `path=""` | the current working directory |
| nothing at all | the configured output directory |

The facade adds a per-source subdirectory when `path` is omitted, so `EarthLens(data_source="chc", …).download()`
writes under `<output_dir()>/chc/`.

A qualified `source:topic` key flattens its colon, because a colon cannot appear in a Windows path component:
`EarthLens(data_source="jrc:coastal-forecast", …).download()` writes under `<output_dir()>/jrc_coastal-forecast/`.
It stays one directory per key, which is what the empty-default cleanup relies on.

!!! note "This changed"

    Before this was introduced, omitting `path=` wrote to the current working directory, and the facade wrote to
    `./earthlens-data/<source>/`. If you have a script that globbed `./earthlens-data/**` afterwards, it now finds
    nothing. To keep the old behaviour, pass `path=""` for the working directory, or set
    `EARTHLENS_DATA_DIR=.` — and your existing files are still wherever they were written, nothing was moved for
    you.

## Where cached intermediates go

Backends that download an archive, extract or index on the way to their output cache it under the shared cache
directory, each in its own subdirectory:

| Backend | Cache subdirectory |
|---|---|
| aqueduct, catrare, flopros, glaciers, solar_wind_atlas | `aqueduct/`, `catrare/`, … |
| cmip6 | `cmip6/` |
| caravan | `caravan/` |
| nwp | `nwp/idx/` |
| osm | `osm_pbf/` |

Backends that expose a `cache_dir=` argument still let you override it for one request.

!!! note "This changed too"

    These nine backends previously cached in three different places: some under your output `path=`
    (`<path>/_aqueduct_cache/` and friends), some under `~/.earthlens/cache/`, and some under the platform user
    cache. They now all follow `cache_dir()`.

    Nothing is migrated for you, so anything cached by an earlier version is simply left behind. It is all
    regenerable — delete the old directories whenever convenient:

    - `~/.earthlens/cache/` (the old osm and cmip6 caches)
    - the old platform cache, which on Windows had a doubled path segment
      (`…\AppData\Local\earthlens\earthlens\Cache`, now `…\AppData\Local\earthlens\Cache`)
    - any `_aqueduct_cache/`, `_catrare_cache/`, `_flopros_cache/`, `_glaciers_cache/` or `_cache/gsa/` folder
      sitting inside a previous output directory

    The caravan cache in particular can be tens of GB, so it is worth checking.

## API

::: earthlens.config
    options:
      show_root_heading: false
      members:
        - set_output_dir
        - output_dir
        - set_cache_dir
        - cache_dir
        - resolve_output_path
