# WorldPop — Introduction

`earthlens.worldpop` downloads gridded population and demographic data from
the **WorldPop open population data hub** ([`hub.worldpop.org`](https://hub.worldpop.org)).
It queries the documented WorldPop REST API for the matching per-country
GeoTIFF URLs, downloads them over anonymous HTTPS, and uses
[`pyramids`](https://github.com/Serapieum-of-alex/pyramids) to mosaic +
crop (and, on request, reproject) the rasters to your area of interest. For
demographic products it additionally writes a tidy age/sex table.

WorldPop data is **open, CC-BY-4.0** (attribution only) — there are no
credentials, no API key, and no account. The default access path needs only
the core dependencies (`requests` + `pyramids`); see
[the WorldPopPy path](worldpoppy.md) for the optional SDK.

## What it covers

A request names one or more **product aliases** via `variables=[…]`; each
product resolves to a concrete REST sub-alias through the
`constrained` / `unadjusted` / `resolution` / `scope` / `generation` /
`level` selectors. earthlens curates the population & demographic families:

| Product alias | What it is | Output |
|---|---|---|
| `pop` | population counts (constrained + unconstrained; 100 m / 1 km; per-country + global) | raster |
| `pop_density` | population density (1 km) | raster |
| `pwd` | population-weighted density (national / subnational) | raster |
| `age_structures` | age/sex-disaggregated population (the "pyramids") | rasters **+ table** |
| `births` | annual live births | raster |
| `pregnancies` | annual pregnancies | raster |
| `dependency_ratios` | youth / old-age dependency | raster |
| `urban_change` | urban extent change | raster |
| `gbsg` | global built-settlement growth | raster |
| `dug` | degree of urbanisation (Global-2) | raster |
| `future_pop` | SSP population projections to 2100 | raster |

The heterogeneous `covariates` family (nightlights, slope, distances, …) is
listed by the catalog refresh tool but is **not** part of the curated
selector catalog — its 50+ named layers don't fit the
`(constrained × resolution × scope × generation)` selector model. See
[Available datasets](datasets.md) for the full sub-alias matrix.

## Output kind

`WorldPop.OUTPUT_KIND` is `"mixed"`: population products yield AOI-cropped
GeoTIFFs, while `age_structures` additionally emits a per-cohort table. The
`EarthLens` facade therefore **forwards** `aggregate=` (it reduces the
per-year raster stack across years — see [Usage](usage.md)).

## How it maps onto the facade

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="worldpop",          # or the "world-pop" alias
    variables=["pop"],               # one or more product aliases
    start="2020", end="2020", fmt="%Y",
    lat_lim=[-4.7, 5.0], lon_lim=[33.9, 41.9],
    aoi="KEN",                       # ISO3 / bbox / GeoDataFrame
    path="out/",
).download()
```

## Install

The default REST path needs no extra:

```bash
pip install earthlens
```

The optional WorldPopPy path adds the SDK (MPL-2.0, Python ≥3.10):

```bash
pip install earthlens[worldpop]
```

## See also

- [Usage](usage.md) — the request shape, every selector, and `aggregate=`.
- [Available datasets](datasets.md) — the product / sub-alias matrix.
- [WorldPopPy path](worldpoppy.md) — the optional SDK + the no-xarray design.
- [API reference](worldpop.md) — the rendered module API.
