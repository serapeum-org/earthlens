# Troubleshooting

The failure modes you are most likely to hit, what the error actually means, and what to change. Errors are
quoted as earthlens raises them.

## Import and installation

### `from earthlens import EarthLens` fails

```
ImportError: cannot import name 'EarthLens' from 'earthlens'
```

`earthlens` is a namespace package with no top-level `__init__.py`. Import from `earthlens.core`:

```python
from earthlens.core import EarthLens
```

See the [migration guide](migration.md). `earthlens.earthlens` also resolves, but it is the internal module path —
use `earthlens.core`.

### A backend raises `ImportError` about an extra

```
... SDK is not installed. Install with `pip install earthlens[cmems]`.
```

Each backend's SDK is optional and imported lazily. Install the extra named in the message, or `earthlens[all]`
for everything. The [provider matrix](reference/providers.md) lists the extra for every backend.

### `uv sync --all-extras` fails with a conflict

```
Extras `openeo` and `argo` are incompatible with the declared conflicts
```

`argopy` needs `xarray>=2025.7` and `openeo` needs `xarray<2025.1.2` — mutually unsatisfiable. Use the curated
extra instead:

```bash
uv sync --extra all --group dev      # includes openeo; omits argo / osm / osm-pbf
```

To work on the argo side, prune the other: `uv sync --all-extras --no-extra openeo --no-extra osm --no-extra osm-pbf`.

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
... (OUTPUT_KIND='vector'). The aggregator only handles gridded raster outputs;
vector / tabular backends emit GeoDataFrame / DataFrame rows that have no meaningful gridded reduction.
```

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

Credentials are missing, wrong, or expired. Credentials are **not** constructor arguments — they go to
`authenticate()` or environment variables:

```python
lens = EarthLens(data_source="cmems", ...).authenticate(
    service_username="…", service_password="…",
)
```

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
lens.download(errors="warn")     # log and continue
lens.download(errors="skip")     # drop silently and continue
lens.download(errors="raise")    # default — abort on first failure
```

### A vector or tabular request exhausts memory

Cap it. `limit=` is an exact cap that stops the work rather than truncating at the end:

```python
for item in lens.datasource.iter_download(limit=500):
    ...
```

See [Base contracts](reference/base/contracts.md).

## Output

### Where did my files go?

If you omit `path=`, earthlens logs where it wrote:

```
No `path` given; download() writes 'chc' output under earthlens-data\chc/ (load() uses a temp dir).
```

Pass `path=` to control it.

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

The docs build does **not** execute notebooks — it renders stored outputs. Commit notebooks with their outputs
saved, or the page publishes as code with no results.

## Still stuck

Open an issue at [github.com/serapeum-org/earthlens/issues](https://github.com/serapeum-org/earthlens/issues) with
the provider key, the full traceback, and your earthlens version:

```python
from earthlens.core import __version__; print(__version__)
```
