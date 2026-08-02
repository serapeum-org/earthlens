# Caravan — inside a GRDC-Caravan archive

GRDC-Caravan publishes the **same data twice**: once with the per-catchment series as CSV, once as NetCDF. They
are separate Zenodo files on the same record, and they differ in exactly one thing — how a catchment's daily
series is encoded. Everything else, down to the attribute tables and the basin shapefile, is byte-for-byte the
same content under a differently-named root directory.

This page describes what is actually in each of them. Every number below was **measured on 2026-08-02** by
reading both archives in place over HTTP Range: the NetCDF inventory cost 6 requests / 2.91 MB, the CSV one
11 requests / 12.35 MB including all four attribute tables. Neither archive was downloaded.

The worked example throughout is **`GRDC_1159100`** — Orange River at Vioolsdrif, South Africa.

## The two publications

| | CSV | NetCDF |
|---|---|---|
| File | `GRDC_Caravan_extension_csv.zip` | `GRDC_Caravan_extension_nc.zip` |
| Size | 8.84 GB | 7.58 GB |
| Record | [15349031](https://doi.org/10.5281/zenodo.15349031) (v0.6) | same record |
| Root directory | `GRDC_Caravan_extension_csv/` | `GRDC_Caravan_extension_nc/` |
| Members | 5,367 | 5,367 |
| Series member | `timeseries/csv/grdc/<gauge_id>.csv` | `timeseries/netcdf/grdc/<gauge_id>.nc` |
| `dataset=` | `"grdc"` (the default) | not readable — see [below](#which-parts-earthlens-uses) |

Both are ZIPs, so both are range-readable: one catchment costs ~6 requests and ~3 MB.

## What is in the box

Identical in both archives — 5,367 members, of which 5,356 are the per-catchment series:

```
GRDC_Caravan_extension_{csv,nc}/
├── timeseries/{csv,netcdf}/grdc/      5356 files   one per catchment
├── attributes/grdc/                      4 files   other, caravan, hydroatlas, additional
├── shapefiles/grdc/                      5 files   grdc_basin_shapes.{shp,shx,dbf,prj,cpg}
└── licenses/grdc/                        2 files   license_grdc.md, LicensesCaravan.xlsx
```

The 5,356 timeseries files are the *only* thing that differs between the two archives. The four attribute tables
are **CSV in both** — the NetCDF archive does not convert them.

## The per-catchment series — CSV

`GRDC_1159100.csv` is **26,801 rows × 41 columns**, covering **1950-01-02 → 2023-05-19**. It inflates to 6.07 MB
from 2.10 MB on the wire.

- **One row per day**, no gaps in the date index — a missing observation is an **empty field**, not a missing
  row. This catchment has 369 empty `streamflow` values scattered between 1961-08-18 and 2021-04-13; the series
  still ends with real data on 2023-05-19.
- **`date` is a plain `YYYY-MM-DD` string.** Everything else parses as `float64`.
- **Columns are alphabetical** in this extension — but that is not a property of Caravan. `israel` and
  `base` v1.2 group theirs by statistic instead, which is why the backend selects columns **by name** and never
  by position.
- **No units, anywhere.** The CSV carries no header metadata; the unit list lives only in the NetCDF variant
  (see [Units](#units-and-where-they-are-documented)).

The 41 columns are `date`, four standalone series, and twelve ERA5-Land families in `{max, mean, min}` triples:

| Group | Columns |
|---|---|
| Index | `date` |
| Observed discharge | `streamflow` |
| Precipitation | `total_precipitation_sum` |
| Potential evaporation | `potential_evaporation_sum_ERA5_LAND`, `potential_evaporation_sum_FAO_PENMAN_MONTEITH` |
| ×3 (`_max`, `_mean`, `_min`) | `temperature_2m`, `dewpoint_temperature_2m`, `snow_depth_water_equivalent`, `surface_net_solar_radiation`, `surface_net_thermal_radiation`, `surface_pressure`, `u_component_of_wind_10m`, `v_component_of_wind_10m`, `volumetric_soil_water_layer_1` … `_4` |

That is 1 + 1 + 1 + 2 + (12 × 3) = **41**. `streamflow` is the only observed quantity; every other column is
ERA5-Land reanalysis aggregated to the catchment.

## The per-catchment series — NetCDF

`GRDC_1159100.nc` is **netCDF-4/HDF5** (magic `\x89HDF`) and inflates to 4.53 MB — about 25 % smaller than the
CSV, because values are stored as binary `float32` rather than decimal text.

It holds **41 variables**, one per CSV column, each 1-D along a single `date` dimension of length 26,801:

| | |
|---|---|
| `date` | `int64`, `units = "days since 1950-01-02 00:00:00"`, `calendar = "proleptic_gregorian"` |
| the other 40 | `float32`, `_FillValue = NaN` |

There are **no per-variable attributes** beyond `_FillValue` — no `units`, no `long_name`. Instead the file
carries three **global** attributes:

| Attribute | Value for this catchment | Why it matters |
|---|---|---|
| `Units` | a text block naming the unit of each variable family | The only place Caravan documents its units |
| `Timezone` | `Africa/Johannesburg` | The **local** timezone the daily aggregation is defined in — it is per catchment, and the CSV does not carry it |
| `_NCProperties` | `version=2,netcdf=4.8.1,hdf5=1.12.1` | The writer's library versions |

!!! note "The timezone is a real difference, not a formality"
    Caravan's daily windows are local-time, and the NetCDF is where that is recorded — per gauge. Two catchments
    in different countries do not share a day boundary. (Caravan **MultiMet**, by contrast, is deliberately
    UTC-0, which is one of several reasons it is [not wrapped by this backend](datasets.md#known-but-not-wrapped).)

## Units, and where they are documented

Transcribed from the NetCDF `Units` global attribute — this is the authoritative list, and it exists **only**
there:

| Variable family | Unit | Note |
|---|---|---|
| `streamflow` | **mm/d** | Observed streamflow — depth, not m³/s |
| `total_precipitation` | mm | |
| `potential_evaporation_sum_ERA5_LAND` | mm | ERA5-Land's own `potential_evaporation` |
| `potential_evaporation_sum_FAO_PENMAN_MONTEITH` | mm | FAO Penman-Monteith, computed from ERA5-Land inputs |
| `temperature_2m` | °C | |
| `snow_depth_water_equivalent` | mm | |
| `surface_net_solar_radiation` | W/m² | |
| `surface_net_thermal_radiation` | W/m² | |
| `surface_pressure` | **kPa** | Not Pa — ERA5's native unit is divided by 1000 |
| `u_component_of_wind_10m`, `v_component_of_wind_10m` | m/s | |
| `volumetric_soil_water_layer_1` … `_4` | m³/m³ | Depths 0–7, 7–28, 28–100, 100–289 cm |

Two things to know about that block:

- **The keys are family names**, without the `_max` / `_mean` / `_min` or `_sum` suffix the columns carry. The
  unit applies to all three statistics of a family.
- **`dewpoint_temperature_2m` is missing from it** — the only family Caravan leaves undocumented. Its values sit
  on the same scale as `temperature_2m` and below it, as dewpoint physically must: for this catchment
  −21.03 … 18.03 against 2.37 … 31.78 °C.

`streamflow` being **mm/d** is the unit that most often trips people up: it is a depth over the catchment, so
converting to volume needs the `area` column from `attributes_other_grdc.csv`. For `GRDC_1159100`, 0.04 mm/d
over 786,037 km² ≈ 364 m³/s, against the `lta_discharge` of 231 m³/s recorded in the additional table — the same
order, as it should be.

## The four attribute tables

All four are CSV in both archives, keyed by `gauge_id`, one row per catchment:

| File | Shape | Contents |
|---|---|---|
| `attributes_other_grdc.csv` | 5,356 × 6 | `gauge_id`, `area`, `country`, `gauge_lat`, `gauge_lon`, `gauge_name` — the location index |
| `attributes_caravan_grdc.csv` | 5,356 × 15 | Climate indices: `aridity_*`, `moisture_index_*`, `pet_mean_*`, `seasonality_*` (each for ERA5-Land and FAO-PM), `p_mean`, `frac_snow`, `high_prec_dur/freq`, `low_prec_dur/freq` |
| `attributes_hydroatlas_grdc.csv` | 5,356 × 197 | The HydroATLAS block — land cover, soil, climate, topography, socio-economic; 14 MB |
| `attributes_additional_grdc.csv` | 5,356 × 21 | **GRDC-only**: `d_start`, `d_end`, `d_yrs`, `d_miss`, `quality`, `lta_discharge`, `altitude`, `nat_id`, `wmo_reg`, `sub_reg`, `lat_pp`, `long_pp`, `dist_km`, `area_shp`, `type`, `source`, `comment`, ISO2 `country` |

A real row from `attributes_other_grdc.csv`:

```
gauge_id     GRDC_1159100
area         786037.24                              # km²
country      South Africa                           # full English name, not ISO2
gauge_lat    -28.7563
gauge_lon    17.7188
gauge_name   ORANGE RIVER, VIOOLSDRIF (27811003)
```

!!! warning "`country` is a name here and a code there"
    `attributes_other_*` spells the country out (`South Africa`); the GRDC-only `attributes_additional_grdc.csv`
    uses ISO2 (`ZA`). Only GRDC ships the second file, so `country=` matches full names everywhere and
    additionally accepts ISO2 for this extension. See [usage](usage.md#by-country).

The additional table is also where GRDC's own record-keeping lives: `d_start` 1950, `d_end` 2023, `d_yrs` 73,
`d_miss` 1.38 % and `quality` `Medium` for this gauge — plus `comment`, which explains how the Caravan catchment
boundary was matched to the GRDC station (*"Area difference 5-10% and distance <= 5 km"*).

## Basin polygons

One shapefile covers all 5,356 catchments, shipped as the five sidecars a shapefile needs:

```
shapefiles/grdc/grdc_basin_shapes.shp   .shx   .dbf   .prj   .cpg
```

`with_geometry=True` extracts **all five** — a `.shp` alone will not open.

## Licensing files

- **`license_grdc.md`** (928 characters) states the grant explicitly. It covers the streamflow series under
  `timeseries/{csv,netcdf}/grdc`, the catchment boundaries under `shapefiles/grdc/`, and the gauge latitude,
  longitude and name in `attributes_other_grdc.csv`, published under **CC-BY-4.0**. It also notes that "all
  hydrological data remain property of the owner" and that the per-country terms are listed separately.
- **`LicensesCaravan.xlsx`** is that per-country list — the upstream licence of each national service whose
  stations are included.

This file is the reason GRDC-Caravan is usable here at all: the raw GRDC portal prohibits redistribution, while
this subset is explicitly CC-BY. See [the GRDC route](introduction.md#why-this-backend-exists-the-grdc-route).

## Which parts earthlens uses

| Archive member | Used by |
|---|---|
| `timeseries/csv/grdc/<gauge_id>.csv` | every `download()` — parsed, column-selected by name, date-filtered |
| `attributes/grdc/attributes_other_grdc.csv` | `lat_lim` / `lon_lim` and `country=` resolution, and `with_attributes=True` |
| `attributes/grdc/attributes_caravan_grdc.csv` | `with_attributes=True` (joined onto the `other` table) |
| `shapefiles/grdc/grdc_basin_shapes.*` | `with_geometry=True` |
| `attributes/grdc/attributes_hydroatlas_grdc.csv` | **not merged** — 197 columns is a different kind of request; reachable through `CaravanArchive.attribute_member(source, kind="hydroatlas")` |
| `attributes/grdc/attributes_additional_grdc.csv` | **not merged** — same, `kind="additional"` |
| `timeseries/netcdf/grdc/<gauge_id>.nc` | **not read** — `timeseries_format="netcdf"` raises `NotImplementedError`; see [why](usage.md#why-there-is-no-netcdf-option) |
| `licenses/grdc/*` | not read — the licence is carried in the catalog row instead |

Since the two archives hold the same values, and the CSV variant is what the backend reads, the NetCDF file is
documented here for people who want to work with the raw archive directly — not because the backend needs it.

## Other extensions

The structure above is Caravan's, not GRDC's, so every extension follows it — with three variations worth
knowing:

- **The root directory differs per record**, and denmark and germany have none at all. See
  [archive layout](datasets.md#archive-layout).
- **`germany` adds two columns**, `streamflow_vol` and `water_level` (43 rather than 41), and **`base` v1.2 has
  40**, with a single `potential_evaporation_sum` instead of the ERA5-Land/FAO-PM pair.
- **Only GRDC ships `attributes_additional_*`** and the per-country licence workbook.
