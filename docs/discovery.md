# Discovering datasets

With 61 providers and thousands of datasets between them, the hard part is often not downloading — it is working
out *which* provider serves what you want, and what it is called there. earthlens exposes three functions for
that, all importable from `earthlens.core`:

```python
from earthlens.core import sources, find, search
```

| Function | Question it answers | Hits the network |
|---|---|---|
| `sources()` | Which backends exist? | no |
| `find(text)` | Which backends have a dataset matching this word? | no |
| `search(...)` | What exactly would this request download? | yes |

Everything here is also available from the command line — see the
[CLI reference](reference/cli.md) for `earthlens datasets`.

## `sources()` — what backends exist

Returns every registered `data_source` key, resolved from the entry points each provider distribution publishes.

```python
from earthlens.core import sources

sources()
# ['admin', 'airnow', 'amazon-s3', 'argo', 'asf', 'bathymetry', 'chc', 'climate-indices', ...]
```

That is 48 canonical keys. Aliases (`chirps` → `chc`, `google-earth-engine` → `gee`, `nexrad` → `radar`, …) also
resolve when passed as `data_source=`, but are not listed here.

Because the registry is lazy, calling `sources()` does **not** import any backend or require its SDK.

## `find(text)` — which provider has this?

A best-effort, offline, cross-provider search. It runs a case-insensitive substring match (then a fuzzy pass)
against every registered provider's catalog and collects the hits, returning `{provider: [dataset, ...]}`.

```python
from earthlens.core import find

matches = find("precipitation")
len(matches)          # 37 providers matched

matches["chc"]        # ['prelim-global-pentad', 'chirp-daily', 'chirtsdaily-tmin', ...]
matches["cmip6"]      # ['tasmin', 'sfcWind']
```

This is the fastest way to answer "who has rainfall?" without reading 48 reference pages. Use it to narrow down,
then read that provider's page for the details.

A provider whose optional SDK is not installed — or that has no free-text catalog — is **skipped rather than
raising**, so `find` works on a bare `pip install earthlens`. That also means a sparse result may reflect what you
have installed rather than what exists; install the extra and re-run if a provider you expected is missing.

`find` is a discovery aid, not an authority. It matches dataset identifiers, so a dataset whose name does not
contain your word will not appear even when it carries the variable you want. Confirm against the provider's
catalog page before relying on a hit.

## `search(...)` — what would this actually download?

The dry-run half of the search→fetch split. It takes the **same arguments as a download** and returns one
`RemoteProduct` per item the download *would* fetch — without fetching anything.

Not every backend implements it. 38 of the 48 do; the rest — CHC among them — raise `NotImplementedError` and
expect you to call `download()` directly. The message names the backend when that happens.

```python
from earthlens.core import search

products = search(
    data_source="amazon-s3",              # unsigned public bucket, no credentials
    temporal_resolution="monthly",
    start="2020-01-01",
    end="2020-02-01",
    variables=["air_temperature_at_2_metres"],
    lat_lim=[30.0, 35.0],
    lon_lim=[28.0, 35.0],
)

len(products)          # how many granules this request resolves to
products[0]            # inspect one before committing
```

Use it to check the size and shape of a job before starting it — how many granules a date range expands to,
whether your AOI actually intersects the coverage, whether the dataset key resolved the way you expected.

Unlike `sources()` and `find()`, this **queries the provider**, so it needs the backend's SDK installed and any
credentials it requires.

The same thing is available as a method when you already hold a facade:

```python
from earthlens.core import EarthLens

lens = EarthLens(data_source="amazon-s3", ...)
products = lens.search()      # dry run
paths = lens.download()       # then fetch
```

## Choosing between them

A typical path from "I need rainfall over Colombia" to a download:

1. **`find("precipitation")`** — narrow 61 providers to a handful.
2. Read the candidates' [provider pages](reference/providers.md) — coverage, resolution, licence, auth.
3. **`search(...)`** — confirm the request resolves to the granules you expect, on the backends that support it.
4. **`download()`** — fetch. On a backend without `search()`, go straight here.

## See also

- [Supported providers](reference/providers.md) — the full matrix of keys, output kinds, auth, and extras.
- [Core functions](reference/core-functions.md) — the rendered API for these three functions.
- [CLI reference](reference/cli.md) — `earthlens datasets where` / `search` / `list` / `show` / `facets`.
