# EUMETSAT Data Store — usage

This page covers the request shape, every backend-specific keyword, what
`download()` returns, and the behaviours worth knowing (output kind,
`aggregate=`, native vs NetCDF formats, quotas, and gotchas).

## The request shape

```python
from earthlens.core import EarthLens

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

### `variables` — `{dataset_key: [selector, ...]}`

Each key is a **curated dataset key** (e.g. `"msg-hrseviri"`,
`"s3-olci-l2-wfr"`; see the [catalog reference](catalog.md) for the full
list). The list holds **selectors** that are *informational* — EUMETSAT
delivers whole products, so you cannot band-subset a native download
without Data Tailor. The selectors seed catalog metadata; a band subset
uses the `tailor=` `filter` ([Data Tailor](data-tailor.md)).

A request may name several datasets at once, but they must all share one
`output_kind` — most datasets are `raster`, but the point/vector products
(Atmospheric Motion Vectors, ASCAT winds, the Lightning Imager
event/flash/group products) are `vector`, so a mixed request is rejected.

### Bounding box and time window

* `lat_lim` / `lon_lim` are WGS84 degrees. The backend converts them to
  the `eumdac` `W,S,E,N` comma-string the Data Store's OpenSearch
  endpoint expects.
* The bbox is a **search filter** — it selects which products *intersect*
  it. It is **not** a pixel crop; you receive whole products. For a
  server-side pixel crop / reproject / reformat, use the `tailor=` knob
  ([Data Tailor](data-tailor.md)).
* `start` / `end` are inclusive dates parsed with `fmt` (default
  `"%Y-%m-%d"`). A **date-only** `end` covers its whole calendar day; an `end` that names a
  time of day means that instant, so a request bounded at `19:00` stops there rather than
  running on to midnight.

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

## Output kind, `tailor=`, and `aggregate=`

The backend sets `OUTPUT_KIND` from the resolved dataset row (`G1`).
Most datasets are `"raster"`; the Atmospheric Motion Vector, ASCAT wind,
and Lightning Imager event/flash/group datasets are `"vector"`.

Two customisation knobs, doing different things:

* `tailor=TailorConfig(...)` — **spatial**, server-side subset / reproject /
  reformat via [Data Tailor](data-tailor.md). Returns the customised
  GeoTIFF / NetCDF paths. Only catalog rows with a `tailor_product_type`
  are eligible.
* `aggregate=` — **temporal** reducer, *not* implemented for EUMETSAT. A
  non-`None` `aggregate=` raises `NotImplementedError`:

```python
el.download(aggregate=some_config)   # NotImplementedError (temporal reducer)
```

To reduce a time axis, download the products (optionally with `tailor=`)
and reduce the NetCDF ones client-side with `pyramids` (see the format tags
below). The two knobs compose: tailor server-side, then aggregate
client-side.

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
print(Catalog().get_dataset("s3-olci-l2-wfr").format)   # 'netcdf'
```

## A few runnable snippets

### Browse the catalog (no network)

```python
from earthlens.eumetsat import Catalog

cat = Catalog()
print(len(cat.datasets), "curated collections")
for key, col in sorted(cat.datasets.items()):
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

## Sentinel-5P timeliness

The EUMETSAT Data Store does **not** publish offline (OFFL) Sentinel-5P
collections. The curated rows therefore mix timeliness, recorded on each
row's `timeliness` field:

| Key | Collection | Timeliness |
|-----|------------|------------|
| `s5p-l2-no2` | TROPOMI L2 NO2 | `nrt` |
| `s5p-l2-co` | TROPOMI L2 CO | `nrt` |
| `s5p-l2-o3` | TROPOMI L2 O3 | `nrt` |
| `s5p-l2-ch4` | TROPOMI CH4 | `reprocessed` |

The NRT collections have a rolling retention (~recent data only, low
latency); CH4 is the consolidated reprocessed archive. For a deeper
offline NO2/CO/O3 archive, use the Data Tailor or the Copernicus Data
Space. Inspect the value with:

```python
from earthlens.eumetsat import Catalog
print(Catalog().get_dataset("s5p-l2-no2").timeliness)   # 'nrt'
```

## Quotas, rate limits, and gotchas

* **Whole-product download**: you receive entire products. Keep the bbox
  and window small to limit how many products match.
* **Search is lazily paginated** — the backend iterates the
  `SearchResults`; a huge window can match thousands of products.
* **Data Tailor quota**: `tailor=` customisations are deleted after
  streaming (including on failure), but the account quota is limited —
  see [Data Tailor](data-tailor.md).
* **Native reading**: a `native`-format product is fetched but not yet
  readable through `pyramids`; tailor it to GeoTIFF, or use satpy
  externally for now.

## Catalog tooling

Three scripts under `tools/eumetsat/` keep the catalog honest. They use
the **public** browse endpoint, so they need **no credentials**:

```bash
# Rebuild the available_datasets index from the public browse endpoint
uv run python tools/eumetsat/refresh_eumetsat_catalog.py refresh

# Diff the curated catalog + index against live (CI: --strict)
uv run python tools/eumetsat/audit_eumetsat_catalog.py --strict

# Print one collection's public metadata (by id or curated key)
uv run python tools/eumetsat/probe_eumetsat_product.py msg-hrseviri
```

See the [catalog reference](catalog.md) for details.
