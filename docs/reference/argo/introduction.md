# Argo float profiles — introduction

[Argo](https://argo.ucsd.edu/) is a global array of ~4000 autonomous
profiling floats that drift with the ocean and surface roughly every ten
days to report a vertical profile — temperature, salinity, and pressure
for the core array, plus biogeochemical parameters (oxygen, chlorophyll,
nitrate, pH, …) for the BGC floats. earthlens ships a single `argo`
backend that reads these profiles through the official
[`argopy`](https://argopy.readthedocs.io/) SDK (open data, no
credentials).

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md); the rendered API is the [Reference](argo.md) page.

## Why it matters here

Argo departs from the gridded backends (CHC rainfall, ERA5, GEE imagery)
in two ways that shape it:

- **The output is an irregular table, not a grid.** A float reports one
  profile per ~10-day cycle wherever it happens to be, so the natural
  shape is a long-format table — one row per measured level. Argo is
  therefore a **`tabular`** backend (`ARGO.OUTPUT_KIND == "tabular"`),
  and `download()` returns a `pandas.DataFrame` rather than writing a
  raster. Because there is no meaningful gridded reduction of irregular
  point profiles, the `EarthLens` facade rejects an `aggregate=`
  argument for this backend with `NotImplementedError` — gridded ocean
  fields are the [CMEMS](../cmems/introduction.md) path.

- **There is no xarray in earthlens.** `argopy`'s headline output is an
  `xarray.Dataset`, but earthlens never imports xarray (pyramids owns
  the GIS data types). The backend calls only `argopy`'s **pandas**
  accessor (`.to_dataframe()`), so xarray stays entirely inside
  `argopy`.

## Selecting data

The `variables=` argument carries either parameter names (a **region**
selection) or a single selector token:

| `variables=[...]` | Selection | `argopy` call |
|---|---|---|
| `["TEMP", "PSAL"]` (or `[]`) | region (bbox + time + depth) | `.region([...])` |
| `["float:6902746"]` / `["float:6902746,6902747"]` | one or more floats by WMO id | `.float([...])` |
| `["profile:6902746/12"]` | one float's cycle | `.profile(6902746, 12)` |

For a region selection the parameter names are **validated** against the chosen
family but do not subset the returned columns — `argopy` returns the whole
family (`PRES`/`TEMP`/`PSAL` plus QC/error columns for `phy`), so naming a
parameter asserts intent rather than filtering columns.

The dataset family is chosen with `dataset=` (`"phy"` default core
physical, or `"bgc"` biogeochemical); the data backend with `source=`
(`"erddap"` default / `"gdac"` / `"argovis"`); the QC mode with `mode=`
(`"standard"` / `"expert"` / `"research"`); and the depth envelope with
`depth=(min, max)` dbar (default `0–2000`).

## Acknowledgement

Argo data are freely usable. On every successful fetch the backend logs
the standard Argo data-acknowledgement statement, which you should
reproduce when you publish work built on these data:

> These data were collected and made freely available by the
> International Argo Program and the national programs that contribute to
> it (https://argo.ucsd.edu, https://www.ocean-ops.org). The Argo Program
> is part of the Global Ocean Observing System.
