# Solar & Wind Atlas — available layers

The `solar-wind-atlas` backend ships 16 curated layers across the two atlases.
Pick one or more with `variables=`. Every layer is a static global climatology
grid in EPSG:4326.

## Global Solar Atlas (`download_zip` transport)

Distributed as DEFLATE GeoTIFF ZIP archives (downloaded once, cropped locally).
Values are long-term-average daily totals.

| `variables=` id | Layer | Units |
|---|---|---|
| `ghi` | Global horizontal irradiation | kWh/m²/day |
| `dni` | Direct normal irradiation | kWh/m²/day |
| `dif` | Diffuse horizontal irradiation | kWh/m²/day |
| `gti` | Global irradiation, optimally tilted surface | kWh/m²/day |
| `pvout` | Photovoltaic power potential (specific PV output) | kWh/kWp/day |
| `opta` | Optimum tilt of PV modules | degree |

## Global Wind Atlas v3 (`vsicurl` transport)

Range-accessible COGs on figshare, read windowed over `/vsicurl`.

| `variables=` id | Layer | Units |
|---|---|---|
| `wind_100m` | Mean wind speed at 100 m | m/s |
| `weibull_k_10m` | Weibull shape parameter `k` at 10 m | dimensionless |
| `weibull_k_50m` | Weibull shape parameter `k` at 50 m | dimensionless |
| `weibull_k_100m` | Weibull shape parameter `k` at 100 m | dimensionless |
| `weibull_k_150m` | Weibull shape parameter `k` at 150 m | dimensionless |
| `weibull_k_200m` | Weibull shape parameter `k` at 200 m | dimensionless |
| `capacity_factor_iec1` | Turbine capacity factor, IEC Class 1 | fraction |
| `capacity_factor_iec2` | Turbine capacity factor, IEC Class 2 | fraction |
| `capacity_factor_iec3` | Turbine capacity factor, IEC Class 3 | fraction |
| `air_density_100m` | Mean air density at 100 m | kg/m³ |

Per-height mean *wind speed* at 10/50/150/200 m is not published as a public
windowable COG (only the 100 m speed is), so it is out of scope for now; the
Weibull parameters above let you reconstruct the speed distribution at each
height.

## Facade aliases

Four friendly aliases route to the same backend:

- `data_source="global-solar-atlas"` / `data_source="gsa"`
- `data_source="global-wind-atlas"` / `data_source="gwa"`

`data_source="solar-wind-atlas"` works with any layer id (solar or wind).

## Listing the ids programmatically

```python
from earthlens.solar_wind_atlas import Catalog

catalog = Catalog()
catalog.available()                 # all 16 layer ids, sorted
catalog.get("ghi").atlas            # 'gsa'
catalog.get("ghi").transport        # 'download_zip'
catalog.get("wind_100m").transport  # 'vsicurl'
catalog.get("wind_100m").units      # 'm/s'
```
