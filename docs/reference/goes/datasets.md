# NOAA GOES-R ABI — products & domains

The catalog (`earthlens.goes.Catalog`, backed by the bundled
`goes_data_catalog.yaml`) curates a subset of the ABI product families, the
scan domains, the satellite→bucket map, and the 16 ABI spectral channels.

```python
from earthlens.goes import Catalog

cat = Catalog()
cat.products()                 # curated product keys
cat.get_product("abi-l2-mcmip")
cat.get_domain("M1")
cat.bucket_for("east")         # -> 'noaa-goes19'
cat.channels["C13"]            # ABI band 13 (Clean longwave window)
```

## Curated products

| `dataset=` | Product group | Level | Domains | Notes |
|------------|---------------|-------|---------|-------|
| `abi-l2-mcmip` | `ABI-L2-MCMIP` | L2 | C / F / M1 / M2 | Cloud & Moisture Imagery — one file, all 16 CMI bands |
| `abi-l2-cmip` | `ABI-L2-CMIP` | L2 | C / F / M1 / M2 | Cloud & Moisture Imagery — one file **per channel** |
| `abi-l1b-rad` | `ABI-L1b-Rad` | L1b | C / F / M1 / M2 | Radiances — one file **per channel** |
| `abi-l2-aod` | `ABI-L2-AOD` | L2 | C / F | Aerosol Optical Depth |
| `abi-l2-adp` | `ABI-L2-ADP` | L2 | C / F / M1 / M2 | Aerosol Detection (smoke / dust) |
| `abi-l2-lst` | `ABI-L2-LST` | L2 | C / F / M1 / M2 | Land Surface Temperature |
| `abi-l2-sst` | `ABI-L2-SST` | L2 | F | Sea Surface Temperature |
| `abi-l2-acha` | `ABI-L2-ACHA` | L2 | C / F / M1 / M2 | Cloud Top Height |
| `abi-l2-actp` | `ABI-L2-ACTP` | L2 | C / F / M1 / M2 | Cloud Top Phase |
| `abi-l2-acm` | `ABI-L2-ACM` | L2 | C / F / M1 / M2 | Clear Sky Mask |
| `abi-l2-cod` | `ABI-L2-COD` | L2 | C / F | Cloud Optical Depth |
| `abi-l2-cps` | `ABI-L2-CPS` | L2 | C / F / M1 / M2 | Cloud Particle Size |
| `abi-l2-dsi` | `ABI-L2-DSI` | L2 | C / F / M1 / M2 | Derived Stability Indices |
| `abi-l2-fdc` | `ABI-L2-FDC` | L2 | C / F / M1 / M2 | Fire / Hot Spot Characterization |
| `abi-l2-rrqpe` | `ABI-L2-RRQPE` | L2 | F | Rainfall Rate / QPE |
| `abi-l2-tpw` | `ABI-L2-TPW` | L2 | C / F / M1 / M2 | Total Precipitable Water |
| `abi-l2-dmw` | `ABI-L2-DMW` | L2 | C / F / M1 / M2 | Derived Motion Winds |

The full public catalog on the buckets is larger (100+ product prefixes per
satellite, including the space-weather instruments SUVI / EXIS / MAG / SEIS and
the GLM lightning mapper); the rows above are the curated imagery / science
subset. Point the backend at any other product by extending the YAML.

## Domains

| Key | Name | Cadence | Prefix suffix | Subsector filename token |
|-----|------|---------|---------------|--------------------------|
| `C` | CONUS | 5 min | `C` | — |
| `F` | Full Disk | 10 min | `F` | — |
| `M1` | Mesoscale 1 | 1 min | `M` (shared) | `…M1` |
| `M2` | Mesoscale 2 | 1 min | `M` (shared) | `…M2` |

Cadence is informational: enumeration lists the actual hour prefix and keeps
whatever granules are there, rather than computing an expected count.

## ABI spectral channels

The 16 ABI bands (`channels` in the catalog) — used to select granules for the
band-split products (`abi-l1b-rad`, `abi-l2-cmip`):

| Channel | µm | Name | | Channel | µm | Name |
|---------|----|------|-|---------|----|------|
| C01 | 0.47 | Blue (visible) | | C09 | 6.9 | Mid-level water vapour |
| C02 | 0.64 | Red (visible) | | C10 | 7.3 | Lower-level water vapour |
| C03 | 0.86 | Veggie (near-IR) | | C11 | 8.4 | Cloud-top phase |
| C04 | 1.37 | Cirrus (near-IR) | | C12 | 9.6 | Ozone |
| C05 | 1.6 | Snow/Ice (near-IR) | | C13 | 10.3 | Clean longwave window |
| C06 | 2.2 | Cloud particle size | | C14 | 11.2 | Longwave window |
| C07 | 3.9 | Shortwave window | | C15 | 12.3 | Dirty longwave window |
| C08 | 6.2 | Upper-level water vapour | | C16 | 13.3 | CO2 longwave |

## See also

* [Introduction](introduction.md) — satellites, products, domains, sizes.
* [Usage](usage.md) — the request shape and every keyword argument.
* [API reference](goes.md) — the rendered module API.
