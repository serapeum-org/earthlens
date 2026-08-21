# Base contracts

`earthlens.base` holds the contracts every one of the 61 provider backends implements. This page documents the
shared surface — what a backend must provide, what it gets for free, and the guarantees the base layer makes about
memory, errors, and re-runs.

Two reworks established the current shape: [#804](https://github.com/serapeum-org/earthlens/pull/804) gave the base
class real defaults and removed the duplication that had let 48 backends drift apart, and
[#824](https://github.com/serapeum-org/earthlens/pull/824) bounded memory, pooled connections, and unified the
remaining duplicated contracts.

## What a backend must implement

Only **two** members of `AbstractDataSource` are abstract:

| Member | Role |
|---|---|
| `download()` | Perform the download and return the result. |
| `_check_input_dates()` | Validate the requested window and return a `TemporalExtent`. |

Everything else has a working default. `_create_grid()`, `_api()`, and `_initialize()` gained defaults in #804,
which removed 102 identical overrides across the tree — a new backend overrides them only when it genuinely
differs.

Three `TemporalExtent` factories cover the common window shapes, replacing 27 hand-rolled hooks:

| Factory | Use |
|---|---|
| `_whole_window_extent()` | The provider serves the whole requested window in one request. |
| `_cadence_extent()` | The window is stepped at a cadence (daily, monthly, …). |
| `_static_extent()` | The product has no time axis. |

## Class attributes that drive behaviour

| Attribute | Default | Meaning |
|---|---|---|
| `OUTPUT_KIND` | `"raster"` | What `download()` returns. Also the first half of the `aggregate=` gate. |
| `SUPPORTS_AGGREGATE` | `False` | The second half: `aggregate=` is forwarded only when this is `True` **and** `OUTPUT_KIND` is `raster` or `mixed`. |
| `REQUIRES_TIME_WINDOW` | `True` | Whether `start` / `end` are mandatory. |
| `SUPPORTS_POLYGON_AOI` | `False` | Whether a polygon `aoi=` is honoured, or reduced to its bounding box. |
| `ERROR_POLICIES` | `{"raise", "warn", "skip", "ignore"}` | The accepted `errors=` values. |

`SUPPORTS_POLYGON_AOI` exists because a polygon `aoi=` used to be silently reduced to its bounding box on 38 of 48
backends. A backend that cannot clip to a polygon now says so rather than quietly returning more data than asked
for.

## Bounded results

A vector or tabular request used to be bounded only by available RAM. The bounded-result contract makes the cap
explicit and **exact** — it stops the work rather than truncating at the end:

```python
lens = EarthLens(data_source="openaq", ...)

# stream, stopping after 500 records
for item in lens.datasource.iter_download(limit=500):
    ...
```

`check_limit(limit)` validates the value; `iter_download(limit=...)` yields at most that many items.

Streaming is **not** universal. The default `iter_download` composes the `_search` / `_fetch_one` split, so only
a backend implementing both gets it — twelve do, across every output kind: `openaq`, `firms`, `usgs-water`,
`argo`, `glaciers`, `climate-indices`, `sensor-community`, `gfw`, plus the raster `cop-dem`, `soilgrids`,
`nwm` and `nwp`. A backend whose fetch is one whole-batch server request, such as `gdacs`, raises rather than
pretending to stream.

## Error policy

Backends that process a batch honour `errors=`:

| Value | Behaviour |
|---|---|
| `"warn"` | Log the failure and continue. **The default on every backend that takes `errors=`.** |
| `"raise"` | Abort on the first failure. |
| `"ignore"` | Drop the failed item and continue, without logging. |
| `"skip"` | Accepted as an alias of `"ignore"`. |

`ERROR_POLICIES` lists four accepted spellings but there are three behaviours: `check_errors_policy` normalises
`"skip"` to `"ignore"`, so both take the same path.

**Service failures are exempt.** A backend may mark exception classes `fatal` when passing work to `_run_items`,
and those propagate whatever `errors=` says. The policy exists to absorb a *per-item* gap — this dataset has no
data for your window — not a refusal by the upstream to serve anything at all. Dropping the latter would report an
outage as every item being empty. `earthlens.ecmwf` marks
[`CadsUnavailableError`](../ecmwf/datastores.md#when-a-store-is-throttling) fatal for exactly that reason.

## Streaming and connection pooling

- **Downloads are streamed and atomic.** Six whole-body `.content` writes were replaced with streamed writes to a
  temporary file that is moved into place on success. This removed a ~4 GB peak on WorldPop and closed a
  truncated-file hazard where a partial download could satisfy an existence check. A failed download no longer
  deletes a pre-existing file at the destination.
- **HTTP connections are pooled.** 22 call sites dropped their per-call adapter, and the per-item helpers take a
  per-thread session instead of re-handshaking for every tile. See [HTTP client](http-client.md).
- **Aggregation streams.** `aggregate_netcdf` processes one window at a time rather than reading the whole cube,
  does not retain written windows, and closes every NetCDF handle in a `finally` — the last of which is what lets
  Windows delete the file afterwards.

## Re-runnable downloads

`_is_complete()` lets a re-run skip an artefact that is already finished, so an interrupted job resumes instead of
starting over.

Output directories are created when a download **starts**, not in the constructor. If the call raises — an
unsupported `aggregate=`, a bad dataset key — the base class unwinds exactly the directories that call created,
leaf-first, and only while each is still empty. A pre-existing tree and anything a partially successful download
wrote are both left alone.

## Catalog contract

All 48 catalog loaders route through `earthlens.base.catalog_source.load_catalog`, which owns the catalog glob,
the `(path, mtime_ns)` cache key, and the cache registry — none computes its own.

`AbstractCatalog.catalog` is a **read-only view**, and `load()` hands out a **fresh `Catalog` per call**, so one
caller mutating a catalog cannot corrupt the process-wide cache.

```python
from pathlib import Path
from earthlens.chc import Catalog

catalog = Catalog.load(Path("path/to/catalog.yaml"))   # ValueError if missing
```

Pass a `pathlib.Path`. A missing path raises `ValueError` (it raised `FileNotFoundError` before 0.12.0 — see the
[migration guide](../../migration.md)).

## Accepted date forms

`start` / `end` accept every documented form — a `datetime`, a `date`, or a `str` in either the plain `fmt`
shape or full ISO-8601. Before #804 this worked on only 8 of 48 backends despite the facade documenting them
all.

## See also

- [HTTP client](http-client.md) — the pooled, retrying `HttpClient`.
- [Region affinity](region-affinity.md) — choosing the endpoint or mirror nearest the AOI.
- [Architecture](../../overview/architecture.md) — how the contracts fit the facade and the registry.
