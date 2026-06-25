# ERDDAP — Available data

ERDDAP has no single global "all datasets" universe — it is a protocol spoken by hundreds of independent servers —
so the `earthlens` catalog is a **curated** map of concrete `(server_url, dataset_id, protocol)` rows rather than
one provider's crawl. You select a row with `dataset=<id>`; the row's `protocol` fixes the output shape
(`griddap` → raster NetCDF, `tabledap` → tabular `DataFrame`). An unknown id raises a `ValueError` that lists the
closest curated match.

The shipped catalog is a starting set on the public **NOAA CoastWatch ERDDAP**
(`https://coastwatch.pfeg.noaa.gov/erddap`); more servers (NCEI, PacIOOS, OOI, IOOS regional associations) and a
catalog-refresh tool are follow-ons.

## Curated datasets

| `dataset=` | Protocol | Output | Default variables | Description |
|---|---|---|---|---|
| `NOAA_DHW` | griddap | raster | `CRW_SSTANOMALY`, `CRW_DHW` | NOAA Coral Reef Watch daily global 5 km SST anomaly + Degree Heating Weeks (current) |
| `erdMH1chla8day` | griddap | raster | `chlorophyll` | Chlorophyll-a, Aqua MODIS, global 4 km, 8-day composite — **historical, 2003–2022 only** |
| `nceiPH53sstd1day` | griddap | raster | `sea_surface_temperature` | NCEI AVHRR Pathfinder v5.3 L3-collated SST, daytime, daily (current) |
| `cwwcNDBCMet` | tabledap | tabular | `station`, `time`, `wtmp` | NDBC standard meteorological buoy time series (current) |

`erdMH1chla8day` is a **historical** record (the Aqua MODIS product stopped updating in mid-2022); request a
date inside 2003–2022, or a recent-date request returns a clear out-of-coverage error.

All four are U.S. Government public-domain datasets on a public, no-auth server.

## Variables

`variables=` is a flat list of variable / column names. For a **griddap** dataset these are grid variable names
(e.g. `CRW_SSTANOMALY`); for a **tabledap** dataset they are table column names (e.g. `wtmp`). Omit `variables=`
to use the row's default set shown above. Each ERDDAP dataset's full variable list is on its own server's dataset
page (the `/info/<dataset_id>/index.html` endpoint).

## Adding a server

The catalog ships as a sharded directory of per-slice YAML files
(`src/earthlens/erddap/catalog/*.yaml`) plus an `_index.yaml`. A new public dataset is one row:

```yaml
datasets:
  <dataset_id>:
    server_url: https://<host>/erddap
    dataset_id: <dataset_id>
    protocol: griddap          # or tabledap
    dim_names: [time, latitude, longitude]   # griddap only
    variables: [<default variable>, ...]
    flux_variables: [<accumulation variable>, ...]   # optional; op="auto" -> "sum"
    title: <human-readable title>
    license_note: <licence / attribution>
```

Mirror the new key into `_index.yaml`; the loader rejects a curated key that is absent from it. Only add public
(no-auth) servers.
