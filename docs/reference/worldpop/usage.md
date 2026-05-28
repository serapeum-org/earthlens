# WorldPop — Usage

A WorldPop request is **an AOI + a time window + one or more product
aliases**, plus selectors that pin the concrete WorldPop variant. No
credentials are needed.

## The request shape

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="worldpop",
    variables=["pop"],               # product aliases (canonical or friendly)
    start="2020", end="2020", fmt="%Y",
    lat_lim=[-4.7, 5.0], lon_lim=[33.9, 41.9],
    aoi="KEN",                       # ISO3 / list / bbox / GeoDataFrame
    path="out/",
    # selectors (all optional):
    constrained=False,               # settlement-masked vs unconstrained
    unadjusted=True,                 # raw vs UN-adjusted
    resolution="100m",               # "100m" | "1km"
    scope="countries",               # "countries" | "global"
    generation="R2021",              # "R2021" | "R2025A" | "R2024B" | "2024"
    level="national",                # "national" | "subnational" (pwd only)
    crs="EPSG:4326",                 # output CRS; native WGS84 = no reproject
    api="rest",                      # "rest" (default) | "worldpoppy"
).download(progress_bar=True)
```

`download()` returns the list of written paths — one cropped GeoTIFF per
`(product, year, cohort)`, plus a `.csv` table for demographic products.

## `variables` — product aliases

`variables` is a `list[str]` of WorldPop product keys. Canonical aliases
(`"pop"`, `"age_structures"`, `"births"`, …) or friendly ones
(`"population"`, `"age_sex"`, `"demographics"`, …) both resolve. See
[Available datasets](datasets.md) for the full list.

## Selectors → a concrete sub-alias

Each product is published in several variants; the selectors pick exactly
one REST sub-alias:

| Selector | Values | Notes |
|---|---|---|
| `constrained` | `False` (default) / `True` | settlement-masked vs not |
| `unadjusted` | `True` (default) / `False` | raw vs UN-adjusted |
| `resolution` | `"100m"` (default) / `"1km"` | not every product offers both |
| `scope` | `"countries"` (default) / `"global"` | global = a single mosaic — **not yet fetchable** (see note) |
| `generation` | `"R2021"` (default) / `"R2025A"` / … | classic vs Global-2 lines |
| `level` | `"national"` (default) / `"subnational"` | `pwd` only |

A selector tuple that matches no sub-alias raises a `ValueError` listing the
product's available variants (did-you-mean). Single-variant products
(`births`, …) ignore the selectors.

!!! warning "Global / continent products are not yet fetchable"
    The download path queries WorldPop per **ISO3 country**. Global mosaics
    (`scope="global"`) and the global / continent-only products
    `future_pop` and `dependency_ratios` are **not** ISO3-keyed — their REST
    listing has no per-file URLs and needs a separate drill-down that is not
    implemented yet. Requesting them raises `NotImplementedError` at
    construction (rather than failing mid-download). Use a country-scoped
    product / `scope="countries"` for now; global support is a follow-up.

```python
# constrained Global-2 2020 100 m population:
EarthLens(data_source="worldpop", variables=["pop"], aoi="RWA",
          start="2020", end="2020", fmt="%Y", lat_lim=[-3,-1], lon_lim=[29,31],
          constrained=True, generation="R2025A", path="out/").download()
```

## The AOI

`aoi` accepts any of:

- an **ISO3 string** — `aoi="KEN"` (used directly);
- a **list of ISO3 strings** — `aoi=["KEN", "UGA"]`;
- a **bbox** `[w, s, e, n]` — intersected with the bundled country-bbox
  table to find the countries;
- a **`GeoDataFrame`** — its total bounds are intersected likewise;
- **`None`** (default) — derive the countries from `lat_lim` / `lon_lim`.

Per-country rasters are mosaicked and cropped to the AOI bbox. The bbox →
country table uses each country's mainland extent, so a bbox over mainland
France will not match France's overseas territories — pass `aoi="<ISO3>"`
explicitly to target an overseas territory.

## Years

`start`/`end` (parsed with `fmt`) select every year in range; `year=` picks
one; `years=[…]` picks an explicit set (it wins over the window). A year
outside a product's availability raises listing the valid years.

## Multi-year reduction — `aggregate=`

Because `OUTPUT_KIND="mixed"`, the facade forwards an
`AggregationConfig`. It reduces the per-year raster stack across years,
bucketed by `freq`, with `op` ∈ `mean` / `sum` / `min` / `max` / `std`
(`auto` → `mean` for population):

```python
from earthlens import AggregationConfig

EarthLens(
    data_source="worldpop", variables=["pop"], aoi="KEN",
    start="2000", end="2020", fmt="%Y",
    lat_lim=[-4.7, 5.0], lon_lim=[33.9, 41.9], path="out/",
).download(aggregate=AggregationConfig(freq="100YS", op="mean"))
# -> one window raster: pop_100YS_20000101_mean.tif
```

!!! note "Population change (delta)"
    A population-*change* (`delta`) reduction is **not** currently available:
    the shared `AggregationConfig.op` is a fixed `Literal`
    (`mean`/`sum`/`min`/`max`/`std`/`auto`) that does not include `delta`.
    Compute change from two single-year pulls, or open an issue to extend the
    shared aggregator.

## Demographic tables

For `age_structures`, each `(sex, age_band)` cohort raster is written **and**
its AOI population total is summed into a tidy table
`{aoi, year, sex, age_low, population}` at `age_structures_{year}.csv` — the
input for a population-pyramid plot.

## Output CRS

WorldPop is delivered in WGS84 (EPSG:4326), so the default `crs="EPSG:4326"`
performs **no reproject**. Set `crs="EPSG:3857"` (etc.) to reproject the
localised raster; the AOI crop bbox stays WGS84 regardless.
