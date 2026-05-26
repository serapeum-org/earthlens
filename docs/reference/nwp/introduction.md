# NWP forecasts — introduction

The `earthlens.nwp` backend fetches **open numerical-weather-prediction
(NWP) forecasts** from the public clouds and turns them into
bbox-cropped Cloud-Optimized GeoTIFFs. It is one subpackage with sibling
"centre" modules over the open forecast buckets:

| Centre | Models (MVP) | Protocol | Cost |
|--------|--------------|----------|------|
| **NOAA NODD** | GFS, GEFS, HRRR | [Herbie](https://herbie.readthedocs.io) `.idx` byte-range subset (S3 / GCS / Azure, unsigned) | free |
| **ECMWF Open Data** | IFS HRES | `ecmwf-opendata` (`s3://ecmwf-forecasts`, CC-BY-4.0) | free |
| **DWD Open Data** | ICON-global | plain HTTPS, per-variable `.grib2.bz2` | free |

Météo-France (`s3://mf-nwp-models`), ECCC (GDPS / RDPS / HRDPS), NWM,
and MRMS are planned follow-ons.

## A forecast time axis, not an observation one

Every other earthlens backend is indexed by a single **valid time**
(when an observation was taken). NWP is different: each datum is indexed
by a pair —

```
(cycle_datetime_utc, forecast_step_hours)
```

— "the 2024-06-01 **00Z run**, **+24 h** lead time". A model runs a
fixed set of cycles per day (`cycles_utc`, e.g. `[0, 6, 12, 18]`) out to
a maximum lead time (`horizon_h`). So a request selects:

* a **cycle date range** via `start` / `end` (which cycles to run), and
* a set of **forecast steps** via `steps=` / `horizon=` (which lead
  times within each cycle).

The backend expands `(model) × (cycles in range) × (requested steps)`
into one output COG per `(cycle, step)`. The valid time of any output is
simply `cycle + step`.

## Adopt Herbie, don't reimplement GRIB indexing

GRIB2 files are large, but each message is byte-addressable through a
sidecar `.idx`. [Herbie](https://herbie.readthedocs.io) already owns
~22 per-model templates that translate a variable selector into the
exact byte ranges to fetch, cutting **>99 %** of the download volume for
the NOAA and ECMWF models. earthlens contributes only a **thin adapter**:
it walks the cycle grid, maps each catalog variable to Herbie's `search`
regex, and fans the per-`(cycle, step)` downloads out. For centres
Herbie does not cover (DWD ICON), a small **direct module** downloads
the per-variable `.bz2` files itself.

GRIB **reading** is owned by [pyramids](https://github.com/serapeum-org/pyramids):
`pyramids.grib.open_grib` (GDAL's native GRIB driver) reads the
downloaded subset, the result is cropped to the request bbox, and
written as a COG — exactly like the other raster backends.

See [Usage](usage.md) for the request shape and [Catalog &
install](catalog.md) for the model list and the `[nwp]` extra.
