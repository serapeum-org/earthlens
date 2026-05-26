# EUMETSAT Data Store — usage

This page covers the request shape, every backend-specific keyword, what
`download()` returns, and the behaviours worth knowing (output kind,
`aggregate=`, native vs NetCDF formats, quotas, and gotchas).

## The request shape

```python
from earthlens.earthlens import EarthLens

el = EarthLens(
    data_source="eumetsat",
    start="2024-06-01",          # inclusive, parsed with fmt
    end="2024-06-02",            # inclusive
    variables={"msg-hrseviri": ["HRSEVIRI"]},
    lat_lim=[0.0, 10.0],         # [lat_min, lat_max] degrees
    lon_lim=[0.0, 10.0],         # [lon_min, lon_max] degrees
    path="eumetsat_output",
)
paths = el.download()
```

### `variables` — `{collection_key: [selector, ...]}`

Each key is a **curated collection key** (e.g. `"msg-hrseviri"`,
`"s3-olci-l2-wfr"`; see the [catalog reference](catalog.md) for the full
list). The list holds **selectors** that are *informational* — EUMETSAT
delivers whole products, so you cannot band-subset a download without
Data Tailor. The selectors seed catalog metadata and the future Data
Tailor request.

A request may name several collections at once, but they must all share
one `output_kind` (all curated rows are `raster`, so this is rarely a
constraint).

### Bounding box and time window

* `lat_lim` / `lon_lim` are WGS84 degrees. The backend converts them to
  the `eumdac` `W,S,E,N` comma-string the Data Store's OpenSearch
  endpoint expects.
* The bbox is a **search filter** — it selects which products *intersect*
  it. It is **not** a pixel crop; you receive whole products. (Pixel
  cropping is the deferred [Data Tailor](data-tailor.md) path.)
* `start` / `end` are inclusive dates parsed with `fmt` (default
  `"%Y-%m-%d"`).

## Backend-specific keyword arguments

| Keyword | Purpose |
|---------|---------|
| `consumer_key` / `consumer_secret` | OAuth2 credentials (else env / file — see [Authentication](authentication.md)). |
| `credentials_file` | Explicit path to a `key,secret` credentials file. |
| `group` | A `DataStoreGroup` (or its string, e.g. `"MSG"`) asserting which group the requested collection(s) belong to. |

Everything passed to `EarthLens(...)` that the facade does not name is
forwarded verbatim to the backend constructor.

## Return value

`download()` returns a `list[pathlib.Path]` — one path per fetched native
product, written into `path`. The file name is the product id
(`str(product)`). An empty list means the search matched nothing in the
window.

## Output kind and `aggregate=`

The backend sets `OUTPUT_KIND` from the resolved collection row (`G1`).
For the curated MVP collections this is `"raster"`.

`aggregate=` is the server-side subset / reduce path, which on the Data
Store is **Data Tailor** — not part of the MVP. A non-`None`
`aggregate=` raises `NotImplementedError` naming Data Tailor:

```python
el.download(aggregate=some_config)   # NotImplementedError → see data-tailor.md
```

To aggregate, download the native products and reduce the NetCDF ones
client-side with `pyramids` (see the format tags below).

## Product formats — native vs NetCDF

Each catalog row carries a `format`:

* `native` — SEVIRI `.nat`, MTG FCI, EPS products. Reading these
  client-side needs a satpy reader bridge in `pyramids` (deferred); the
  MVP fetches them whole.
* `netcdf` — Sentinel-3 / -5P / -6 mirrors, OSI SAF. Readable with
  `pyramids.netcdf.NetCDF.read_file` **today**.
* `grib` / `bufr` — MSG cloud mask (GRIB), ASCAT soil moisture / IASI L2
  (BUFR).

Inspect a row's format before assuming it is pyramids-readable:

```python
from earthlens.eumetsat import Catalog
print(Catalog().get_collection("s3-olci-l2-wfr").format)   # 'netcdf'
```

## A few runnable snippets

### Browse the catalog (no network)

```python
from earthlens.eumetsat import Catalog

cat = Catalog()
print(len(cat.collections), "curated collections")
for key, col in sorted(cat.collections.items()):
    print(f"{key:32s} {col.group.value:12s} {col.format}")
```

### Fetch a Sentinel-3 OLCI L2 product (NetCDF)

```python
el = EarthLens(
    data_source="eumetsat",
    start="2024-06-01", end="2024-06-01",
    variables={"s3-olci-l2-wfr": ["OL_2_WFR"]},
    lat_lim=[40.0, 45.0], lon_lim=[0.0, 5.0],
    path="eumetsat_output",
)
paths = el.download()
```

### Disambiguate by group

```python
el = EarthLens(
    data_source="eumetsat",
    start="2024-06-01", end="2024-06-01",
    variables={"msg-hrseviri": ["HRSEVIRI"]},
    lat_lim=[0, 10], lon_lim=[0, 10],
    group="MSG",            # asserts the collection's Data Store group
    path="eumetsat_output",
)
```

## Quotas, rate limits, and gotchas

* **Whole-product download**: you receive entire products. Keep the bbox
  and window small to limit how many products match.
* **Search is lazily paginated** — the backend iterates the
  `SearchResults`; a huge window can match thousands of products.
* **Data Tailor quota** (when that path lands): customisations must be
  deleted after streaming or the account fills up.
* **Native reading**: a `native`-format product is fetched but not yet
  readable through `pyramids`; use Data Tailor or satpy externally for
  now.

## Catalog tooling

Two scripts under `tools/eumetsat/` keep the catalog honest (they need
credentials):

```bash
# Rebuild the available_collections index from the live Data Store
pixi run -e dev python tools/eumetsat/refresh_eumetsat_catalog.py refresh

# Diff the curated catalog + index against live (CI: --strict)
pixi run -e dev python tools/eumetsat/audit_eumetsat_catalog.py --strict
```

See the [catalog reference](catalog.md) for details.
