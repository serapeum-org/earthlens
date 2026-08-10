# WRI Aqueduct riverine flood risk — introduction

The [WRI Aqueduct](https://www.wri.org/aqueduct) Global Flood Analyzer measures
**river flood risk** — the expected exposure of GDP, population, and urban area
to riverine flooding — aggregated by administrative unit across the globe.
earthlens ships a single `aqueduct` backend that reads the 2015 Analyzer data
directly from WRI's public file host (`files.wri.org`, CC-BY-4.0, no
credentials) and returns the admin polygons carrying the risk attributes.

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md); the rendered API is the [Reference](aqueduct.md) page.

## What it is (and is not)

Aqueduct ships **two distinct products**, and this backend covers only one:

- **The flood *impact / exposure* tables** — "how much GDP / population / urban
  area sits in the flood zone" summed per admin unit. **This is what the
  `aqueduct` backend fetches.**
- **The flood *hazard* depth rasters** — the gridded inundation-depth maps. Those
  are a separate product, already reachable through the `gee` backend as the
  `WRI/Aqueduct_Flood_Hazard_Maps/V2` asset. The `aqueduct` backend does **not**
  duplicate them.

## Why it matters here

Like the GDACS and risk-indicators backends, Aqueduct departs from the gridded
backends (CHC rainfall, ERA5, GEE imagery) in two ways:

- **The output is a vector table, not a grid.** A query returns the admin
  polygons — country, state, or river basin — each carrying one exposure column
  per flood return period. So Aqueduct is a **`vector`** backend
  (`Aqueduct.OUTPUT_KIND == "vector"`), and `download()` returns a
  [pyramids](https://github.com/serapeum-org/pyramids) `FeatureCollection` (a
  `geopandas.GeoDataFrame` subclass) rather than writing a raster. A
  `geometry=False` request returns a geometry-dropped `pandas.DataFrame`
  instead. Because there is no meaningful gridded reduction of an
  admin-aggregated exposure table, the facade **rejects an `aggregate=`
  argument**.
- **It is a static snapshot, not a time series.** The 2015 Analyzer has no time
  axis, so `start` / `end` are optional; the "time" dimension is instead the
  choice of a **2010** baseline or a **2030** projection.

## What is available

| Dimension | Choices |
|---|---|
| `admin_level` | `country`, `state`, `basin` |
| `hazard` | `riverine` only (coastal is the paywalled 2020 product) |
| `metric` | `gdp_affected`, `population_affected`, `urban_damage` |
| `year` | `2010` (baseline) or `2030` (projection) |
| `scenario` | `baseline` (2010); or 2030 climate × socio-economic combinations |
| `return_period` | 2, 5, 10, 25, 50, 100, 250, 500, 1000 yr |

The 2030 scenarios combine a socio-economic pathway (SSP2 / SSP3 / baseline) with
a climate pathway (RCP4.5 / RCP8.5 / baseline): `ssp2-rcp4p5`, `ssp2-rcp8p5`,
`ssp3-rcp8p5`, `base-rcp4p5`, `base-rcp8p5`, `ssp2-basehydro`, `ssp3-basehydro`.

The values are **per-return-period exposure**, not a single pre-integrated
expected-annual-damage figure.

## Scope note — the 2020 product

The richer 2020 Aqueduct Floods product (coastal flooding, 2050 / 2080 horizons,
city level, pre-computed expected-annual damage) is **not freely downloadable**:
its aggregated tables sit in a WRI S3 bucket that denies anonymous reads. Only
the 2015 riverine Analyzer used here is public, so coastal / 2050 / 2080 / city
selections are out of scope.

## Licence

The data is licensed **CC-BY-4.0**. Attribution: *World Resources Institute 2015;
Winsemius et al. 2013; Ward et al. 2013*.
