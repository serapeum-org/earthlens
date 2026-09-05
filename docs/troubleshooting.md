# Troubleshooting

The failure modes you are most likely to hit, what the error actually means, and what to change. Errors are
quoted as earthlens raises them.

## Import and installation

### `from earthlens import EarthLens` fails

```
ImportError: cannot import name 'EarthLens' from 'earthlens' (unknown location)
```

`earthlens` is a namespace package with no top-level `__init__.py`. Import from `earthlens.core`:

```python
from earthlens.core import EarthLens
```

See the [migration guide](migration.md). `earthlens.earthlens` also resolves, but it is the internal module path —
use `earthlens.core`.

### A backend raises `ImportError` about an extra

```
ImportError: backend 'cmems' is unavailable — its runtime dependency is not
installed. Install with `pip install earthlens[cmems]`.
```

Each backend's SDK is optional and imported lazily. Install the extra named in the message, or `earthlens[all]`
for everything. The [provider matrix](reference/providers.md) lists the extra for every backend.

### `uv sync --all-extras` fails with a conflict

```
Extras `openeo` and `argo` are incompatible with the declared conflicts
```

(That one is uv's message, not earthlens'.)

`argopy` needs `xarray>=2025.7` and `openeo` needs `xarray<2025.1.2` — mutually unsatisfiable. Use the curated
extra instead:

```bash
uv sync --extra all --group dev      # includes openeo and osm; omits argo / osm-pbf
```

To work on the argo side, prune the other: `uv sync --all-extras --no-extra openeo --no-extra osm-pbf`.

## Request construction

### `'x' is not a supported data source`

```
ValueError: 'not-a-provider' is not a supported data source. Known: ['admin', 'admin-boundaries', ...]
```

The key is wrong. `sources()` lists the 48 canonical keys; aliases also work. See
[Discovering datasets](discovery.md).

### `variables= is required`

```
ValueError: variables= is required (or pass dataset= for a dataset-keyed backend),
e.g. EarthLens('chc', variables=['precipitation']).
```

Catalog-driven backends (ECMWF, GEE, STAC, NWP, …) want either a `{dataset: [variable, …]}` mapping or an
explicit `dataset=`. A bare list only works for backends with a single implicit dataset.

### `temporal_resolution 'hourly' is not supported`

```
ValueError: temporal_resolution 'hourly' is not supported by the list-shape `variables` API.
Either pass a dict like `variables={'<dataset-key>': [...]}` or use one of ['daily', 'monthly'].
```

Before 0.12.0 this silently downloaded **daily** data. If this started failing after an upgrade, the old run was
giving you daily data under a label that claimed otherwise — see the [migration guide](migration.md).

### `NotImplementedError` on `aggregate=`

```
NotImplementedError: aggregate= is not supported by GDACS (OUTPUT_KIND='vector'):
disaster alerts are vector features, not gridded rasters, so there is no meaningful
gridded reduction. Call download() without aggregate= and post-process the returned
FeatureCollection.
```

The wording after the colon is the backend's own `AGGREGATE_REFUSAL_REASON`, so it names why *that* provider
refuses rather than repeating a generic sentence.

`aggregate=` is forwarded only when the backend's `OUTPUT_KIND` is `raster` or `mixed` **and** it declares
`SUPPORTS_AGGREGATE`. A raster backend that has not wired the reducer is refused too. For `vector` / `tabular`
results, reduce the returned
`GeoDataFrame` / `DataFrame` with pandas instead.

### A polygon AOI came back as a rectangle

You will see a `PolygonAoiWarning`. Not every backend can clip server-side; those that cannot reduce the polygon
to its bounding box and say so. Check `SUPPORTS_POLYGON_AOI`, and clip the result yourself with
[pyramids](https://github.com/serapeum-org/pyramids) if you need the exact shape.

## Authentication

### `AuthenticationError`

Credentials are missing, wrong, or expired. **Where they go is backend-specific** — there is no single rule:

```python
# CMEMS takes them as constructor keywords, forwarded to the backend
lens = EarthLens(
    data_source="cmems", ...,
    service_username="…", service_password="…",
)

# GEE and FIRMS take theirs in authenticate()
lens = EarthLens(data_source="gee", ...).authenticate(
    service_account="my-sa@my-project.iam.gserviceaccount.com",
    service_key="/path/to/key.json",
)
```

`authenticate(**credentials)` forwards its keywords verbatim to the backend's own `authenticate`, so passing one
the backend does not accept raises `TypeError` rather than `AuthenticationError`:

```
TypeError: AbstractDataSource.authenticate() got an unexpected keyword argument 'service_username'
```

If you see that, the credential belongs in the constructor instead. Most backends also read an environment
variable when the keyword is omitted.

Per-provider setup is on each backend's page; see also [Authentication examples](examples/authentication.md) and
the [auth API](reference/base/auth-api.md).

### ECMWF / CDS fails to authenticate

`cdsapi` reads `~/.cdsapirc` (on Windows, `C:\Users\<you>\.cdsapirc`). Confirm the file exists and holds the
`url:` and `key:` from your CDS profile. A dataset also requires you to have accepted its licence in the CDS web
UI — an unaccepted licence fails at retrieve time, not at auth time.

### EUMETSAT returns 403 on download

Auth succeeded but the product is not entitled to your account. Accept the collection's licence in the EUMETSAT
portal, then retry.

## Network and rate limits

### `429 Too Many Requests`, or a download that stalls then recovers

Expected and handled. `HttpClient` retries `429` and `5xx` automatically, honouring `Retry-After` when present
and otherwise backing off exponentially:

| Setting | Default |
|---|---|
| Timeout | 60 s |
| Max retries | 5 |
| Backoff factor | 1.0 s |
| Max backoff | 300 s |
| Retried statuses | 429, 500, 502, 503, 504 |

If a provider still rate-limits you, request less per call (a shorter window, a smaller AOI) rather than looping
harder.

### A batch dies on one bad item

Backends that process a batch honour `errors=`:

```python
lens.download(errors="warn")     # default — log the failure and continue
lens.download(errors="ignore")   # drop it and continue, without logging
lens.download(errors="raise")    # abort on the first failure
```

`"skip"` is accepted as an alias of `"ignore"`.

A failure of the *service* rather than of one item is exempt from all three: it propagates even under
`"ignore"`, because continuing would report an upstream outage as every item having no data. On the ECMWF
backend that is `CadsUnavailableError` — see
[when a store is throttling](reference/ecmwf/datastores.md#when-a-store-is-throttling).

### A vector or tabular request exhausts memory

Cap it. `limit=` is an exact cap that stops the work rather than truncating at the end:

```python
for item in lens.datasource.iter_download(limit=500):
    ...
```

Only backends implementing the `_search` / `_fetch_one` split can stream — twelve do: `openaq`, `firms`,
`usgs-water`, `argo`, `glaciers`, `climate-indices`, `sensor-community`, `gfw`, `cop-dem`, `soilgrids`, `nwm`
and `nwp`. On one whose fetch is a single whole-batch request, such as `gdacs`, this raises
`NotImplementedError`; narrow the request instead.

See [Base contracts](reference/base/contracts.md).

## Output

### Where did my files go?

If you omit `path=`, earthlens logs where it wrote:

```
No `path` given; download() writes 'chc' output under ~/.earthlens/data/chc/ (load() uses a temp dir).
```

An omitted `path=` resolves to the configured output directory — `set_output_dir()`, else `EARTHLENS_DATA_DIR`,
else `~/.earthlens/data` — and the facade adds a per-source subdirectory, with a qualified `source:topic`
key flattening its colon (`jrc:coastal-forecast` writes to `jrc_coastal-forecast/`). Pass `path=` to control it per call, or
see [Configuration](reference/configuration.md) to set it once for the whole process.

If you are upgrading and a script that globbed `./earthlens-data/**` now finds nothing, that is why: the default
moved off the working directory. Pass `path=""` to write to the working directory as before. Nothing was moved for
you, so files an earlier version wrote are still where it put them.

### Where did my cached downloads go?

Backends that download an archive or index on the way to their output cache it under the shared cache directory —
`set_cache_dir()`, else `EARTHLENS_CACHE`, else the per-platform user cache — each in its own subdirectory. Nine
backends used to cache elsewhere (inside your output `path=`, or under `~/.earthlens/cache/`); anything they left
behind is regenerable and safe to delete. [Configuration](reference/configuration.md) lists the old locations.

### `download()` returned an object, not file paths

That is by design and depends on the backend's `OUTPUT_KIND`: `raster` returns `list[Path]`; `vector` returns a
`FeatureCollection`; `tabular` returns a `DataFrame`. It never returns `None`. The matrix is in
[Supported providers](reference/providers.md).

### A re-run downloads everything again

Backends implement `_is_complete()` so a re-run skips finished artefacts — but only when it can identify them,
which means writing to the **same `path=`**. A different output directory is a fresh job.

### An output directory appeared and then vanished

Deliberate. Output directories are created when a download starts, not at construction; if the call raises, the
base class removes exactly the directories that call created, leaf-first, and only while still empty. A
pre-existing tree and any partially written data are left alone.

## Docs and development

### `mkdocs build --strict` fails

Warnings are errors. The usual causes are a nav entry pointing at a missing file, a dangling `#anchor`, or a
griffe docstring complaint (a missing `->` annotation, or continuation lines indented off the 4-space grid). Build
locally before pushing:

```bash
uv run --group docs --extra all mkdocs build --strict
```

### A notebook renders with no output

Expected for most example pages. The docs build does **not** execute notebooks — it renders whatever outputs are
stored — and the `nbstripout` pre-commit hook clears outputs from everything under `docs/examples/` except
`showcases/`. So a page showing code with no results is the designed state, not a broken build.

Run the notebook yourself to see its output. If you are authoring one whose rendered result is the point, put it
under `docs/examples/showcases/`, which is excluded from the hook.

## Still stuck

Open an issue at [github.com/serapeum-org/earthlens/issues](https://github.com/serapeum-org/earthlens/issues) with
the provider key, the full traceback, and your earthlens version:

```python
from earthlens.core import __version__; print(__version__)
```
