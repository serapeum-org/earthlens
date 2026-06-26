# PVGIS — available products

The backend's product catalog (`earthlens.pvgis.Catalog`, loaded from the
bundled `pvgis_data_catalog.yaml`) maps each product id to the PVGIS tool it
drives, the REST endpoint, the default query params, and the canonical value
columns. Select one with `variables=["<id>"]`.

```python
from earthlens.pvgis import Catalog

Catalog().available()        # -> ['seriescalc', 'tmy']
Catalog().get("seriescalc")  # -> Product(tool='seriescalc', endpoint='seriescalc', ...)
```

## `seriescalc` — hourly radiation / PV power

Hourly solar-radiation time series over the requested year window. Columns:

| column | description | units |
|--------|-------------|-------|
| `time` | timestamp (parsed to `datetime64`) | — |
| `G(i)` | global irradiance on the inclined plane | W/m² |
| `H_sun` | sun height | degree |
| `T2m` | 2-m air temperature | °C |
| `WS10m` | 10-m wind speed | m/s |
| `Int` | 1 ⇒ radiation values reconstructed | — |
| `P` | PV system power (only when `pvcalculation=1`) | W |

## `tmy` — typical meteorological year

An hourly multi-year synthesis (8760 hours). Columns:

| column | description | units |
|--------|-------------|-------|
| `time` | timestamp (normalised from PVGIS's `time(UTC)`) | — |
| `T2m` | 2-m air temperature | °C |
| `RH` | relative humidity | % |
| `G(h)` | global irradiance, horizontal plane | W/m² |
| `Gb(n)` | beam/direct irradiance, normal to sun | W/m² |
| `Gd(h)` | diffuse irradiance, horizontal | W/m² |
| `IR(h)` | surface infrared (thermal), horizontal | W/m² |
| `WS10m` | 10-m wind speed | m/s |
| `WD10m` | 10-m wind direction (0 = N, 90 = E) | degree |
| `SP` | surface (air) pressure | Pa |

Every returned frame also carries the `lat`, `lon`, and `product` tag columns
the backend adds per sampled point.

## Not in the MVP

PVGIS exposes more tools (`PVcalc` grid-connected PV sizing, `MRcalc` monthly
radiation, `DRcalc` daily radiation). The MVP ships the two `tabular`
time-series tools above; the rest are catalog follow-ons.
