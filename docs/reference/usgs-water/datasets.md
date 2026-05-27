# USGS Water — Available data

USGS NWIS filters its queries by 5-digit **parameter codes** (`00060`
discharge, `00065` gage height, …). For `variables=` you may pass either a raw
code or one of the curated **friendly names** below, which the bundled catalog
resolves to a code. The full USGS parameter-code table is far larger than this
curated set — **any valid 5-digit code passes through unmapped**, so you are
never limited to the names listed here.

## Curated parameter codes

| Friendly name | Code | Name | Units | Group | Services |
|---|---|---|---|---|---|
| `discharge` | `00060` | Discharge | ft3/s | Physical | daily, instantaneous, statistics, peaks |
| `dissolved_oxygen` | `00300` | Dissolved oxygen | mg/l | Inorganics | daily, instantaneous, samples |
| `gage_height` | `00065` | Gage height | ft | Physical | daily, instantaneous, statistics |
| `groundwater_level` | `72019` | Depth to water level | ft below land surface | Physical | gwlevels, daily, instantaneous, field-measurements |
| `nitrate` | `99133` | Nitrate plus nitrite | mg/l as N | Nutrients | daily, instantaneous, samples |
| `ph` | `00400` | pH | std units | Inorganics | daily, instantaneous, samples |
| `precipitation` | `00045` | Precipitation | in | Physical | daily, instantaneous |
| `reservoir_storage` | `00054` | Reservoir storage | ac-ft | Physical | daily, instantaneous |
| `specific_conductance` | `00095` | Specific conductance | uS/cm @25C | Physical | daily, instantaneous, samples |
| `suspended_sediment` | `80154` | Suspended sediment concentration | mg/l | Physical | daily, samples |
| `temperature` | `00010` | Water temperature | degC | Physical | daily, instantaneous, samples |
| `turbidity` | `63680` | Turbidity | FNU | Physical | daily, instantaneous, samples |

Resolve either form:

```python
from earthlens.usgs_water import Catalog
cat = Catalog()
cat.resolve("discharge")   # -> "00060"
cat.resolve("00060")       # -> "00060"  (raw code passes through)
cat.resolve("63680")       # -> "63680"  (uncurated code, still valid)
```

An unknown but close friendly name raises with a did-you-mean hint.

## The full parameter index (tooling)

The complete USGS parameter-code table (~25k codes) is not hand-curated.
`tools/usgs_water/refresh_usgs_catalog.py` builds an informational
`available_parameters` index from the live USGS reference table
(`waterdata.get_reference_table(collection="parameter-codes")` — the modern
replacement for the removed `nwis.get_pmcodes`):

```bash
# Rebuild the informational code index (needs API_USGS_PAT — the modern
# reference-table endpoint rate-limits anonymous requests).
pixi run -e dev python tools/usgs_water/refresh_usgs_catalog.py refresh

# Append a curated friendly-name row, then reload to validate it.
pixi run -e dev python tools/usgs_water/refresh_usgs_catalog.py \
    add-parameter chlorophyll 32209 --units "ug/l" --group Biological \
    --services daily samples

# Validate: every curated row's services are known, and (when the index
# exists) every curated code is a real USGS code.
pixi run -e dev python tools/usgs_water/refresh_usgs_catalog.py validate
```

## Services

The `service=` keyword selects the NWIS / Water Data plane; the `services`
column above lists where each curated code is typically reported. See
[Usage](usage.md#the-services) for each service's request shape and output
columns.
