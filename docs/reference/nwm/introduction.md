# National Water Model — introduction

The `earthlens.nwm` backend fetches **NOAA National Water Model (NWM)**
output — the United States' operational hydrologic forecast. NWM takes a
land-surface model forced by atmospheric analyses/forecasts and routes
the resulting water budget down the NHDPlus river network, producing
per-reach streamflow alongside gridded land-surface and routing states.

## Products

A configuration publishes several NetCDF products per cycle/step:

| Product | Shape | Contents |
|---------|-------|----------|
| `channel_rt` | per-reach (indexed by `feature_id`, **not** a lat/lon grid) | streamflow, velocity on the river network |
| `land` | gridded (continental) | soil moisture, snow, evapotranspiration |
| `reservoir` | per-reservoir | inflow / outflow |
| `terrain_rt` | gridded | ponded water depth, surface routing |

Because `channel_rt` is keyed by river-reach id rather than a regular
grid, the backend's output is a **`tabular`** inventory, not a raster.

## Source

NWM is published on the unsigned `noaa-nwm-pds` AWS bucket (anonymous
list + GET), laid out as:

```
nwm.{YYYYMMDD}/{configuration}/nwm.t{HH}z.{token}.{product}.f{NNN}.{domain}.nc
```

The configurations follow a forecast `(cycle, step)` axis, like the
[NWP](../nwp/introduction.md) backend — a configuration runs on a set of
UTC cycles and publishes forecast steps (`fNNN`). Their file names are
**not** uniform across configurations (an ensemble member can ride on the
product token — `channel_rt_1`; regional domains use sub-hourly 5-digit
steps; analyses use `tmNN`), so every catalog row carries a full
`key_template` that pins its exact S3 key.

Two clean CONUS forecast configurations are curated today:

| Configuration | Cycles (UTC) | Horizon | Note |
|---------------|--------------|---------|------|
| `short_range` | hourly (00–23) | 18 h | deterministic |
| `medium_range_mem1` | 00 / 06 / 12 / 18 | 240 h | GFS-forced ensemble member 1 |

## What the backend does

For each requested configuration + cycle/step window it formats the exact
S3 keys, downloads the NetCDF files into the output directory, and returns
a `pandas.DataFrame` cataloguing them (configuration, cycle, step,
valid-time, product, domain, local path). **Decoding** `channel_rt`
streamflow into a tidy `feature_id × time` table (via `xarray`) is a
downstream follow-on — this backend fetches and inventories; it does not
decode.

See [Usage](usage.md) for the request shape.
