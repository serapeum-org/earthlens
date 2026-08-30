# EUMETSAT Data Store — Data Tailor

**Data Tailor** is EUMETSAT's server-side customisation service
(`api.eumetsat.int/epcs/`) — the analog of NASA's Harmony. It **subsets**
(ROI crop), **reprojects**, and **reformats** products *on the server*
before delivery. earthlens reaches it through a dedicated `tailor=` knob on
`download()`.

## `tailor=` vs `aggregate=` — spatial vs temporal

These are two different operations and two different knobs:

| Knob | Operation | Where it runs |
|------|-----------|---------------|
| `tailor=TailorConfig(...)` | **spatial** — reproject + ROI crop + reformat | server-side (Data Tailor) |
| `aggregate=AggregationConfig(...)` | **temporal** — reduce a time axis (mean/sum/…) | client-side (pyramids) |

`aggregate=` is *not* implemented for EUMETSAT and raises
`NotImplementedError` (it is the temporal reducer, unrelated to Data
Tailor). Use `tailor=` for server-side spatial customisation. The two
compose: tailor a product server-side, then reduce the result client-side.

## Usage

```python
from earthlens.core import EarthLens
from earthlens.eumetsat import TailorConfig

el = EarthLens(
    data_source="eumetsat",
    start="2024-06-01",
    end="2024-06-01",
    variables={"s3-olci-l1-efr": ["OLL1EFR"]},
    lat_lim=[50.0, 52.0],
    lon_lim=[-1.0, 1.0],
    path="examples/data/eumetsat",
)

paths = el.download(
    tailor=TailorConfig(
        format="geotiff",       # -> Chain.format ("geotiff", "netcdf4", …)
        crs="geographic",       # -> Chain.projection
        bbox=(-1.0, 50.0, 1.0, 52.0),  # (west, south, east, north); optional
    ),
)
# paths -> the customised GeoTIFF(s), read with pyramids like any raster
```

`TailorConfig` fields:

- `format` — Data Tailor output format (default `"geotiff"`).
- `crs` — target projection (default `"geographic"`), or `None` to leave the
  grid unprojected. A handful of output formats carry their own fixed grid
  and reject any projection — `"msgnative"`, `"epsnative"`, `"hrit"`,
  `"hrit_compressed"` — and `TailorConfig` requires `crs=None` for those:

  ```python
  tailor=TailorConfig(
      format="msgnative",     # a native format: no reprojection allowed
      crs=None,                # -> Chain.projection is left unset
      bbox=(-5.0, 40.0, 15.0, 55.0),
  )
  ```

  Pairing a native `format` with a non-`None` `crs` raises at construction,
  before any request reaches Data Tailor.
- `bbox` — optional `(west, south, east, north)` crop. When omitted, the
  request's own `lat_lim` / `lon_lim` become the ROI.
- `filter` — optional list of band / layer names to keep.
- `quicklook` — request a quicklook rendering (default `False`).

The Data Tailor **product-type** is not set by the user — it comes from the
resolved catalog row's `tailor_product_type`.

## Eligibility — not every collection is tailorable

Data Tailor supports a fixed registry of product types. A catalog row is
Data-Tailor-eligible only when it carries a `tailor_product_type`; the
curated catalog marks **83** eligible collections (MSG SEVIRI, MTG FCI,
Metop ASCAT / IASI / AVHRR / GOME-2, Sentinel-3 OLCI / SLSTR / SRAL,
Sentinel-6, OSI SAF SST, …). Notably **Sentinel-5P TROPOMI and OSI-SAF sea
ice are *not* tailorable** by this service.

!!! note "Multi-channel / vector collections use a representative product type"
    A few eligible collections (e.g. `mtg-amv`, the ASCAT wind and Lightning
    Imager products) are `output_kind: vector` and/or span several Data Tailor
    per-channel products. Their `tailor_product_type` records one representative
    product from the registry — so eligibility is correct, but the default
    `format="geotiff"` may not be the meaningful output for a swath / vector
    collection (prefer `format="netcdf4"`), and a specific channel may need its
    own product type. Verify the mapping against your use case before relying on
    it.

A `tailor=` request against a non-eligible dataset raises a clear error:

```python
el = EarthLens(data_source="eumetsat", variables={"s5p-l2-no2": ["NO2"]}, ...)
el.download(tailor=TailorConfig())
# ValueError: EO:EUM:DAT:0076 not Data-Tailor-eligible; download native
# (no tailor=) and reduce client-side with pyramids.
```

## Lifecycle, quota, and reliability

Each matching product is customised independently: submit → poll to a
terminal state (`DONE` / `FAILED` / `KILLED`) with bounded backoff and a
timeout → stream the output(s) to `path` → **delete the customisation**.

!!! warning "Data Tailor quota"
    Every customisation is `delete()`d after streaming — including on
    failure (in a `finally`). The Data Tailor account quota is limited, and
    orphaned customisations exhaust it. If you see a quota error, delete
    stale customisations (`eumdac.DataTailor(token).customisations`) or wait.

Reliability notes:

- The EPCS endpoint intermittently returns `502 Bad Gateway`; a transient
  submit failure is retried a few times with backoff.
- A `FAILED` / `KILLED` customisation raises a `RuntimeError` carrying the
  tail of the server log; a job that never finishes raises `TimeoutError`.

!!! note "Account must be download-authorised"
    Data Tailor pulls the input product from the Data Store **server-side**
    using your token. If your account has not accepted a collection's
    licence (or lacks Data Store download entitlement), the customisation
    fails with a `403 Forbidden` at the download step — accept the licence
    in the [EUMETSAT portal](https://user.eumetsat.int) for that collection.

## Reading native SEVIRI / FCI

Tailoring to `geotiff` / `netcdf4` yields a pyramids-readable raster.
**Native** SEVIRI / FCI products (without `tailor=`) still need a satpy
reader bridge in `pyramids` (a separate cross-repo follow-on) before they
can be read client-side — tailoring to GeoTIFF sidesteps that.
