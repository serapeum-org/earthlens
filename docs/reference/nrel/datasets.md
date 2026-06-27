# NREL — available products

The backend's product catalog (`earthlens.nrel.Catalog`, loaded from the bundled
`nrel_data_catalog.yaml`) maps each product id to the source archive, the keyed
CSV download endpoint, the default attribute list, the data resolution, the CSV
metadata-row count, and the canonical value columns. Select one with
`product="<id>"` (or the `nsrdb` / `wind-toolkit` facade aliases).

```python
from earthlens.nrel import Catalog

Catalog().available()        # -> ['nsrdb-psm3', 'nsrdb-tmy', 'wtk']
Catalog().get("nsrdb-psm3")  # -> Product(source='nsrdb', endpoint='/api/nsrdb/...', ...)
```

## `nsrdb-psm3` — NSRDB GOES Aggregated PSM v4 (hourly solar)

Hourly satellite-derived solar resource for one point and one year (valid years
1998–2024), the current replacement for the retired PSM v3.2.2 product. Request
attributes from the NSRDB vocabulary (`ghi`, `dni`, `dhi`, `air_temperature`,
`wind_speed`, `dew_point`, `surface_pressure`, `relative_humidity`, …); the
default set is `ghi, dni, dhi, air_temperature, wind_speed`. Typical columns:

| column | description | units |
|--------|-------------|-------|
| `time` | timestamp (assembled from Year/Month/Day/Hour/Minute) | — |
| `GHI` | global horizontal irradiance | W/m² |
| `DNI` | direct normal irradiance | W/m² |
| `DHI` | diffuse horizontal irradiance | W/m² |
| `Temperature` | air temperature | °C |
| `Wind Speed` | wind speed | m/s |

## `nsrdb-tmy` — NSRDB GOES TMY v4 (typical meteorological year)

A Typical Meteorological Year: an hourly multi-year synthesis (`names=tmy`) of
solar irradiance and temperature for the location. Same NSRDB attribute
vocabulary as `nsrdb-psm3`; one call regardless of the requested year window.

## `wtk` — WIND Toolkit (hourly wind)

Hourly modelled wind resource for one point and one year, CONUS + offshore
(the CONUS WTK archive covers roughly **2007–2014** — a year outside the
archive's range returns the generic coverage error). Attributes are per-height
(`windspeed_<H>m`, `winddirection_<H>m`,
`temperature_<H>m`, `pressure_<H>m`, for hub heights such as 10 / 40 / 80 / 100 /
120 / 160 m); the default set is `windspeed_100m, winddirection_100m,
temperature_100m`. The WTK CSV uses a **single** metadata header row (NSRDB uses
two); the parser detects this automatically. Typical columns:

| column | description | units |
|--------|-------------|-------|
| `time` | timestamp | — |
| `wind speed at 100m (m/s)` | wind speed at the requested height | m/s |
| `wind direction at 100m (deg)` | wind direction (0 = N, 90 = E) | degree |
| `air temperature at 100m (C)` | air temperature at the requested height | °C |

Every returned frame also carries the `lat`, `lon`, `year`, and `product` tag
columns the backend adds per sampled `(point, year)`.

## Not in the MVP

NREL exposes more NSRDB products (GOES Conus / Full Disc, NSRDB Polar, Himawari
for Asia/Pacific, METEOSAT for Europe/Africa) and the gridded HSDS/`rex` archive
path. The MVP ships the three keyed-CSV `tabular` products above; the rest are
catalog / transport follow-ons.
