# EUMETSAT Data Store — catalog & tooling

The EUMETSAT catalog ships as package data: a directory of per-group YAML
files at `src/earthlens/eumetsat/catalog/` (`mtg.yaml`, `msg.yaml`,
`mfg.yaml`, `metop.yaml`, `metopsg.yaml`, `sentinel3.yaml`,
`sentinel5p.yaml`, `sentinel6.yaml`, `osisaf.yaml`, `other.yaml`) plus a
single `_index.yaml` holding the informational `available_datasets:`
index. A duplicate-key-rejecting loader merges them into one `Catalog`.
Mirrors the `earthlens.earthdata` / `earthlens.gee` / `earthlens.cmems`
catalogs: `datasets` (the curated map) + `available_datasets` (the index).

Each row maps a **friendly key** (what you pass in `variables=`) to the
real Data Store `EO:EUM:DAT:…` collection id, plus the group, output
kind, on-disk format, native selectors, the Data Tailor product type (for
the `tailor=` server-side path), and the spatial / temporal coverage.

```python
from earthlens.eumetsat import Catalog

cat = Catalog()
ds = cat.get_dataset("msg-hrseviri")
ds.collection_id      # 'EO:EUM:DAT:MSG:HRSEVIRI'
ds.group.value        # 'MSG'
ds.format             # 'native'
```

An unknown key raises `ValueError` with a did-you-mean hint; `resolve(key,
group=...)` additionally asserts the row's Data Store group.

## Curated datasets

**Every** live EUMETSAT Data Store collection is curated (all ~180),
grouped by mission family below. Ids, titles and start dates were walked
from the public browse endpoint on 2026-05-26. Hand-vetted rows (HRSEVIRI,
the Sentinel-5P gases, ASCAT soil moisture, the MTG suite, ...) carry
tuned metadata; the remainder carry browse-derived `format` /
`output_kind` / `cadence` heuristics (advisory for the whole-product
download), refined per product as needed and reconciled by the audit tool.

### MTG (27)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `mtg-active-fire` | `EO:EUM:DAT:0682` | raster | netcdf | subdaily | 2025-07-03 | FIR |
| `mtg-active-fire-cap` | `EO:EUM:DAT:0801` | raster | cap | subdaily | 2025-07-03 | FIR |
| `mtg-amv` | `EO:EUM:DAT:0676` | vector | netcdf | subdaily | 2025-01-27 | AMV |
| `mtg-amv-bufr` | `EO:EUM:DAT:0998` | vector | bufr | subdaily | 2025-01-27 | AMV |
| `mtg-asr` | `EO:EUM:DAT:0677` | raster | netcdf | subdaily | 2025-01-27 | ASR |
| `mtg-asr-bufr` | `EO:EUM:DAT:0799` | raster | bufr | subdaily | 2025-01-27 | ASR |
| `mtg-clear-sky-reflectance` | `EO:EUM:DAT:0679` | raster | netcdf | subdaily | 2025-07-03 | CRM |
| `mtg-cloud-mask` | `EO:EUM:DAT:0678` | raster | netcdf | 10min | 2025-01-27 | CLM |
| `mtg-cloud-mask-grib` | `EO:EUM:DAT:0800` | raster | grib | 10min | 2025-01-24 | CLM |
| `mtg-cloud-type` | `EO:EUM:DAT:0680` | raster | netcdf | subdaily | 2025-12-10 | CT |
| `mtg-ctth` | `EO:EUM:DAT:0681` | raster | netcdf | subdaily | 2025-12-10 | CTTH |
| `mtg-fci-l1c` | `EO:EUM:DAT:0662` | raster | native | 10min | 2024-09-24 | FCI-L1C-NR |
| `mtg-fci-l1c-hr` | `EO:EUM:DAT:0665` | raster | native | 10min | 2024-09-24 | FCI-L1C-HR |
| `mtg-fci-sst` | `EO:EUM:DAT:0694` | raster | netcdf | subdaily | 2025-08-20 | SST |
| `mtg-instability-indices` | `EO:EUM:DAT:0683` | raster | netcdf | subdaily | 2025-01-27 | GII |
| `mtg-li-accumulated-flash-area` | `EO:EUM:DAT:0687` | raster | netcdf | subdaily | 2024-07-04 | LI-AFA |
| `mtg-li-accumulated-flash-radiance` | `EO:EUM:DAT:0688` | raster | netcdf | subdaily | 2024-07-04 | LI-AFR |
| `mtg-li-accumulated-flashes` | `EO:EUM:DAT:0686` | raster | netcdf | subdaily | 2024-07-04 | LI-AF |
| `mtg-li-events-filtered` | `EO:EUM:DAT:0690` | vector | netcdf | subdaily | 2024-07-04 | LI-LEF |
| `mtg-li-flashes` | `EO:EUM:DAT:0691` | vector | netcdf | subdaily | 2024-07-04 | LI-LFL |
| `mtg-li-groups` | `EO:EUM:DAT:0782` | vector | netcdf | subdaily | 2024-07-04 | LI-LGR |
| `mtg-lst` | `EO:EUM:DAT:1088` | raster | netcdf | subdaily | 2025-08-14 | LST |
| `mtg-oca` | `EO:EUM:DAT:0684` | raster | netcdf | subdaily | 2025-01-27 | OCA |
| `mtg-olr` | `EO:EUM:DAT:0685` | raster | netcdf | subdaily | 2025-01-27 | OLR |
| `mtg-precip-accumulated` | `EO:EUM:DAT:1087` | raster | netcdf | subdaily | 2025-08-14 | PA |
| `mtg-precip-rate` | `EO:EUM:DAT:1086` | raster | netcdf | subdaily | 2025-08-14 | PR |
| `mtg-snow-mask` | `EO:EUM:DAT:1091` | raster | netcdf | subdaily | 2025-08-15 | SNOW |

### MSG (14)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `msg-cloud-mask` | `EO:EUM:DAT:MSG:CLM` | raster | grib | 15min | 2004-01-19 | CLM |
| `msg-cloud-mask-iodc` | `EO:EUM:DAT:MSG:CLM-IODC` | raster | grib | 15min | 2017-02-01 | CLM |
| `msg-cloud-top-height` | `EO:EUM:DAT:MSG:CTH` | raster | netcdf | subdaily | 2005-06-21 | — |
| `msg-cloud-top-height-2` | `EO:EUM:DAT:MSG:CTH-IODC` | raster | netcdf | subdaily | 2017-02-01 | — |
| `msg-fire-risk-map` | `EO:EUM:DAT:0398` | raster | netcdf | subdaily | 2023-09-21 | — |
| `msg-hrseviri` | `EO:EUM:DAT:MSG:HRSEVIRI` | raster | native | 15min | 2004-01-19 | HRSEVIRI |
| `msg-hrseviri-iodc` | `EO:EUM:DAT:MSG:HRSEVIRI-IODC` | raster | native | 15min | 2017-02-01 | HRSEVIRI |
| `msg-land-surface-temperature-record` | `EO:EUM:DAT:0088` | raster | netcdf | irregular | 2004-01-21 | — |
| `msg-land-surface-temperature-with-directional-effects` | `EO:EUM:DAT:0394` | raster | netcdf | subdaily | 2023-09-18 | — |
| `msg-optimal-cloud-analysis-climate-record-release` | `EO:EUM:DAT:0617` | raster | netcdf | irregular | 2004-01-19 | — |
| `msg-rapid-scan-cloud-mask` | `EO:EUM:DAT:MSG:RSS-CLM` | raster | netcdf | 5min | 2008-10-16 | — |
| `msg-rss` | `EO:EUM:DAT:MSG:MSG15-RSS` | raster | native | 5min | 2008-05-13 | HRSEVIRI |
| `msg-seviri-rapid-scan-atmospheric-motion-vectors` | `EO:EUM:DAT:1083` | vector | netcdf | irregular | 2008-05-06 | — |
| `msg-seviri-rapid-scan-high-rate-1` | `EO:EUM:DAT:0962` | raster | native | irregular | 2008-05-06 | — |

### MFG (9)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `mfg-atmospheric-motion-vectors-climate-record-release` | `EO:EUM:DAT:0405` | vector | netcdf | irregular | 1981-09-03 | — |
| `mfg-atmospheric-motion-vectors-climate-record-release-2` | `EO:EUM:DAT:0894` | vector | netcdf | irregular | 2006-11-28 | — |
| `mfg-atmospheric-motion-vectors-climate-record-release-3` | `EO:EUM:DAT:0895` | vector | netcdf | irregular | 1998-07-01 | — |
| `mfg-gsa-2-climate-record-release-2` | `EO:EUM:DAT:0300` | raster | netcdf | irregular | 1982-02-10 | — |
| `mfg-gsa-2-climate-record-release-2-2` | `EO:EUM:DAT:0301` | raster | netcdf | irregular | 2006-12-07 | — |
| `mfg-gsa-2-climate-record-release-2-3` | `EO:EUM:DAT:0302` | raster | netcdf | irregular | 1998-07-10 | — |
| `mfg-mviri-1-5-climate-record-release` | `EO:EUM:DAT:0880` | raster | native | irregular | 1982-08-11 | — |
| `mfg-mviri-1-5-climate-record-release-2` | `EO:EUM:DAT:0881` | raster | native | irregular | 2006-11-01 | — |
| `mfg-mviri-1-5-climate-record-release-3` | `EO:EUM:DAT:0882` | raster | native | irregular | 1998-07-01 | — |

### Metop (40)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `metop-amsu-b-microwave-humidity-sounder-climate` | `EO:EUM:DAT:0303` | raster | netcdf | irregular | 1999-01-01 | — |
| `metop-amsua-l1` | `EO:EUM:DAT:METOP:AMSUL1` | raster | native | subdaily | 2006-10-19 | AMSUL1 |
| `metop-ascat-1-sigma0-resampled-25-km` | `EO:EUM:DAT:METOP:ASCSZO1B` | raster | netcdf | subdaily | 2013-01-01 | — |
| `metop-ascat-1-szf-climate-record-release` | `EO:EUM:CM:METOP:ASCSZFR02` | raster | netcdf | irregular | 2007-01-01 | — |
| `metop-ascat-1-szo-climate-record-release` | `EO:EUM:CM:METOP:ASCSZOR02` | raster | netcdf | irregular | 2007-01-01 | — |
| `metop-ascat-1-szr-climate-record-release` | `EO:EUM:CM:METOP:ASCSZRR02` | raster | netcdf | irregular | 2007-01-01 | — |
| `metop-ascat-coastal-winds-12-5-km` | `EO:EUM:DAT:METOP:OSI-104` | vector | netcdf | subdaily | 2013-06-03 | — |
| `metop-ascat-l1-szf` | `EO:EUM:DAT:METOP:ASCSZF1B` | raster | native | subdaily | 2007-01-01 | ASCSZF1B |
| `metop-ascat-l1-szr` | `EO:EUM:DAT:METOP:ASCSZR1B` | raster | native | subdaily | 2007-01-01 | ASCSZR1B |
| `metop-ascat-l2-12-5-km-winds` | `EO:EUM:DAT:METOP:OSI-150-B` | vector | netcdf | irregular | 2007-01-01 | — |
| `metop-ascat-l2-25-km-winds-record` | `EO:EUM:DAT:METOP:OSI-150-A` | vector | netcdf | irregular | 2007-01-01 | — |
| `metop-ascat-soil-moisture-12km` | `EO:EUM:DAT:METOP:SOMO12` | raster | bufr | subdaily | 2007-01-01 | SOMO12 |
| `metop-ascat-soil-moisture-25km` | `EO:EUM:DAT:METOP:SOMO25` | raster | bufr | subdaily | 2007-01-01 | SOMO25 |
| `metop-ascat-winds-soil-moisture-25-km` | `EO:EUM:DAT:METOP:OAS025` | vector | netcdf | subdaily | 2011-02-28 | — |
| `metop-avhrr-fundamental-record` | `EO:EUM:DAT:0862` | raster | netcdf | irregular | 1978-11-05 | — |
| `metop-avhrr-gac-atmospheric-motion-vectors-climate` | `EO:EUM:DAT:0558` | vector | netcdf | irregular | 1979-01-01 | — |
| `metop-avhrr-global-atmospheric-motion-vectors-climate` | `EO:EUM:DAT:0151` | vector | netcdf | irregular | 2013-04-24 | — |
| `metop-avhrr-l1` | `EO:EUM:DAT:METOP:AVHRRL1` | raster | native | subdaily | 2006-10-19 | AVHRRL1 |
| `metop-avhrr-polar-atmospheric-motion-vectors-climate` | `EO:EUM:DAT:0152` | vector | netcdf | irregular | 2007-03-01 | — |
| `metop-clara-a3-cm-saf-cloud-albedo` | `EO:EUM:DAT:0874` | raster | netcdf | subdaily | 1979-01-01 | — |
| `metop-eps-daily-land-surface-temperature` | `EO:EUM:DAT:METOP:LSA-002` | raster | netcdf | subdaily | 2023-08-09 | — |
| `metop-gome-2-1b-fundamental-record-release` | `EO:EUM:DAT:0533` | raster | netcdf | irregular | 2007-04-01 | — |
| `metop-gome2-l1` | `EO:EUM:DAT:METOP:GOMEL1` | raster | native | subdaily | 2007-01-09 | GOMEL1 |
| `metop-hirs-1b` | `EO:EUM:DAT:MULT:HIRSL1` | raster | netcdf | subdaily | 2007-03-02 | — |
| `metop-iasi-1-principal-component-scores` | `EO:EUM:DAT:METOP:IASPCS01` | raster | netcdf | subdaily | 2019-07-01 | — |
| `metop-iasi-1c-climate-record-release-2` | `EO:EUM:DAT:0590` | raster | native | irregular | 2007-07-10 | — |
| `metop-iasi-all-sky-temperature-humidity-profiles` | `EO:EUM:DAT:0576` | raster | netcdf | irregular | 2007-07-10 | — |
| `metop-iasi-carbon-monoxide-profiles-forli-co` | `EO:EUM:DAT:0959` | raster | netcdf | irregular | 2007-07-10 | — |
| `metop-iasi-carbon-monoxide-profiles-forli-co-2` | `EO:EUM:DAT:METOP:IASIL2COX` | raster | netcdf | subdaily | 2024-01-01 | — |
| `metop-iasi-l1c` | `EO:EUM:DAT:METOP:IASIL1C-ALL` | raster | native | subdaily | 2007-05-29 | IASIL1C |
| `metop-iasi-l2` | `EO:EUM:DAT:METOP:IASSND02` | raster | bufr | subdaily | 2008-01-01 | IASSND02 |
| `metop-iasi-noise-covariance-matrix` | `EO:EUM:DAT:1099` | raster | netcdf | subdaily | 2025-09-01 | — |
| `metop-iasi-ozone-climate-record-release-1` | `EO:EUM:DAT:1027` | raster | netcdf | irregular | 2007-07-10 | — |
| `metop-iasi-principal-components-scores-fundamental-record` | `EO:EUM:DAT:0758` | raster | netcdf | irregular | 2007-07-10 | — |
| `metop-iasi-so2-climate-record-release-1` | `EO:EUM:DAT:0960` | raster | netcdf | irregular | 2007-07-10 | — |
| `metop-mhs-l1` | `EO:EUM:DAT:METOP:MHSL1` | raster | native | subdaily | 2006-10-19 | MHSL1 |
| `metop-mhs-microwave-humidity-sounder-climate-record` | `EO:EUM:DAT:0305` | raster | netcdf | irregular | 2005-08-30 | — |
| `metop-mhs-microwave-humidity-sounder-climate-record-2` | `EO:EUM:DAT:0344` | raster | netcdf | irregular | 2006-10-31 | — |
| `metop-near-real-time-total-column` | `EO:EUM:DAT:METOP:NTO` | raster | netcdf | subdaily | 2024-07-15 | — |
| `metop-polar-multi-sensor-aerosol-optical-properties` | `EO:EUM:DAT:0579` | raster | netcdf | irregular | 2007-07-10 | — |

### Metop-SG (3)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `metopsg-aux` | `EO:EUM:DAT:0520` | raster | other | irregular | 2026-04-28 | AUX |
| `metopsg-bufr` | `EO:EUM:DAT:0892` | raster | bufr | subdaily | 2026-02-26 | BUFR |
| `metopsg-gras2-l1b` | `EO:EUM:DAT:0452` | raster | netcdf | subdaily | 2026-02-26 | GRAS-L1B |

### Sentinel-3 (28)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `s3-olci-1b-full-resolution` | `EO:EUM:DAT:0885` | raster | netcdf | subdaily | 2016-04-06 | — |
| `s3-olci-1b-reduced-resolution` | `EO:EUM:DAT:0410` | raster | netcdf | subdaily | 2024-07-26 | — |
| `s3-olci-1b-reduced-resolution-2` | `EO:EUM:DAT:0886` | raster | netcdf | subdaily | 2016-04-06 | — |
| `s3-olci-2-ocean-colour-full-resolution` | `EO:EUM:DAT:0556` | raster | netcdf | subdaily | 2016-04-25 | — |
| `s3-olci-2-ocean-colour-reduced-resolution` | `EO:EUM:DAT:0557` | raster | netcdf | subdaily | 2016-04-25 | — |
| `s3-olci-l1-efr` | `EO:EUM:DAT:0409` | raster | netcdf | subdaily | 2016-04-25 | OL_1_EFR |
| `s3-olci-l2-wfr` | `EO:EUM:DAT:0407` | raster | netcdf | subdaily | 2016-04-25 | OL_2_WFR |
| `s3-olci-l2-wrr` | `EO:EUM:DAT:0408` | raster | netcdf | subdaily | 2016-04-25 | OL_2_WRR |
| `s3-slstr-1b-radiances-brightness-temperatures` | `EO:EUM:DAT:0581` | raster | netcdf | subdaily | 2016-04-19 | — |
| `s3-slstr-1b-radiances-brightness-temperatures-2` | `EO:EUM:DAT:0615` | raster | netcdf | subdaily | 2018-05-01 | — |
| `s3-slstr-2-aerosol-optical-depth` | `EO:EUM:DAT:0416` | raster | netcdf | subdaily | 2021-01-01 | — |
| `s3-slstr-2-atmospheric-motion-vectors` | `EO:EUM:DAT:1003` | vector | netcdf | subdaily | 2025-11-23 | — |
| `s3-slstr-2-fire-radiative-power` | `EO:EUM:DAT:0417` | raster | netcdf | subdaily | 2021-01-01 | — |
| `s3-slstr-2-sea-surface-temperature` | `EO:EUM:DAT:0582` | raster | netcdf | subdaily | 2016-04-18 | — |
| `s3-slstr-l1-rbt` | `EO:EUM:DAT:0411` | raster | netcdf | subdaily | 2016-05-01 | SL_1_RBT |
| `s3-slstr-l2-wst` | `EO:EUM:DAT:0412` | raster | netcdf | subdaily | 2016-05-01 | SL_2_WST |
| `s3-sral-1a-unpacked-l0-complex-echoes` | `EO:EUM:DAT:0583` | raster | netcdf | subdaily | 2016-03-01 | — |
| `s3-sral-1a-unpacked-l0-complex-echoes-2` | `EO:EUM:DAT:0836` | raster | netcdf | subdaily | 2016-05-05 | — |
| `s3-sral-1a-unpacked-l0-complex-echos` | `EO:EUM:DAT:0413` | raster | netcdf | subdaily | 2023-03-10 | — |
| `s3-sral-1b` | `EO:EUM:DAT:0406` | raster | netcdf | subdaily | 2023-03-10 | — |
| `s3-sral-1b-2` | `EO:EUM:DAT:0584` | raster | netcdf | subdaily | 2016-03-01 | — |
| `s3-sral-1b-3` | `EO:EUM:DAT:0833` | raster | netcdf | subdaily | 2016-05-05 | — |
| `s3-sral-1b-stack-echoes` | `EO:EUM:DAT:0414` | raster | netcdf | subdaily | 2023-03-10 | — |
| `s3-sral-1b-stack-echoes-2` | `EO:EUM:DAT:0585` | raster | netcdf | subdaily | 2016-03-01 | — |
| `s3-sral-1b-stack-echoes-3` | `EO:EUM:DAT:0835` | raster | netcdf | subdaily | 2016-05-05 | — |
| `s3-sral-2-altimetry-global` | `EO:EUM:DAT:0586` | raster | netcdf | subdaily | 2016-03-01 | — |
| `s3-sral-2-altimetry-global-2` | `EO:EUM:DAT:0834` | raster | netcdf | subdaily | 2016-05-05 | — |
| `s3-sral-l2-wat` | `EO:EUM:DAT:0415` | raster | netcdf | subdaily | 2016-03-01 | SR_2_WAT |

### Sentinel-5P (13)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `s5p-l2-ch4` | `EO:EUM:DAT:1101` | raster | netcdf | subdaily | 2018-04-30 | L2__CH4___ |
| `s5p-l2-co` | `EO:EUM:DAT:0073` | raster | netcdf | subdaily | 2018-04-30 | L2__CO____ |
| `s5p-l2-no2` | `EO:EUM:DAT:0076` | raster | netcdf | subdaily | 2018-04-30 | L2__NO2___ |
| `s5p-l2-o3` | `EO:EUM:DAT:0077` | raster | netcdf | subdaily | 2018-04-30 | L2__O3____ |
| `s5p-tropomi-1b-consolidated-earth-radiances-measurements` | `EO:EUM:DAT:0069` | raster | netcdf | subdaily | 2025-03-11 | — |
| `s5p-tropomi-1b-solar-irradiance-measurements` | `EO:EUM:DAT:0070` | raster | netcdf | subdaily | 2025-03-11 | — |
| `s5p-tropomi-1b-solar-irradiance-measurements-2` | `EO:EUM:DAT:0071` | raster | netcdf | subdaily | 2025-03-11 | — |
| `s5p-tropomi-2-aerosol-index-nrt` | `EO:EUM:DAT:0072` | raster | netcdf | subdaily | 2025-05-09 | — |
| `s5p-tropomi-2-aerosol-layer-height-nrt` | `EO:EUM:DAT:0103` | raster | netcdf | subdaily | 2025-05-09 | — |
| `s5p-tropomi-2-cloud-fraction-albedo-top` | `EO:EUM:DAT:0074` | raster | netcdf | subdaily | 2025-05-09 | — |
| `s5p-tropomi-2-formaldehyde-total-column-nrt` | `EO:EUM:DAT:0075` | raster | netcdf | subdaily | 2025-05-09 | — |
| `s5p-tropomi-2-ozone-profile-nrt` | `EO:EUM:DAT:0602` | raster | netcdf | subdaily | 2025-03-11 | — |
| `s5p-tropomi-2-sulphur-dioxide-total-column` | `EO:EUM:DAT:0078` | raster | netcdf | subdaily | 2025-03-12 | — |

### Sentinel-6 (27)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `s6-advanced-microwave-radiometer-2` | `EO:EUM:DAT:1073` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-advanced-microwave-radiometer-for-climate-2` | `EO:EUM:DAT:0856` | raster | netcdf | irregular | 2025-04-20 | — |
| `s6-climate-quality-advanced-microwave-radiometer-2` | `EO:EUM:DAT:0837` | raster | netcdf | irregular | 2020-12-18 | — |
| `s6-p4-l2-hr` | `EO:EUM:DAT:0855` | raster | netcdf | subdaily | 2020-12-17 | P4_2__HR_ |
| `s6-p4-l2-lr` | `EO:EUM:DAT:0854` | raster | netcdf | subdaily | 2020-12-17 | P4_2__LR_ |
| `s6-poseidon-4-2p-altimetry-high-resolution` | `EO:EUM:DAT:0858` | raster | netcdf | subdaily | 2022-12-06 | — |
| `s6-poseidon-4-2p-altimetry-low-resolution` | `EO:EUM:DAT:0857` | raster | netcdf | subdaily | 2022-12-06 | — |
| `s6-poseidon-4-2p-wind-wave-low` | `EO:EUM:DAT:0142` | vector | netcdf | subdaily | 2022-04-06 | — |
| `s6-poseidon-4-3-altimetry-high-resolution` | `EO:EUM:DAT:0859` | raster | netcdf | subdaily | 2022-04-06 | — |
| `s6-poseidon-4-3-altimetry-low-resolution` | `EO:EUM:DAT:0601` | raster | netcdf | subdaily | 2022-08-05 | — |
| `s6-poseidon-4-3-altimetry-low-resolution-2` | `EO:EUM:DAT:1015` | raster | netcdf | subdaily | 2021-12-29 | — |
| `s6-poseidon-4-3-wind-wave-low` | `EO:EUM:DAT:0143` | vector | netcdf | subdaily | 2022-04-06 | — |
| `s6-poseidon-4-altimetry-1a-high-resolution` | `EO:EUM:DAT:0838` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-1a-high-resolution-2` | `EO:EUM:DAT:0850` | raster | netcdf | subdaily | 2025-04-20 | — |
| `s6-poseidon-4-altimetry-1a-high-resolution-3` | `EO:EUM:DAT:1069` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-1b-high-resolution` | `EO:EUM:DAT:0839` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-1b-high-resolution-2` | `EO:EUM:DAT:0851` | raster | netcdf | subdaily | 2025-04-20 | — |
| `s6-poseidon-4-altimetry-1b-high-resolution-3` | `EO:EUM:DAT:1070` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-1b-low-resolution` | `EO:EUM:DAT:0840` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-1b-low-resolution-2` | `EO:EUM:DAT:0852` | raster | netcdf | subdaily | 2025-04-20 | — |
| `s6-poseidon-4-altimetry-1b-low-resolution-3` | `EO:EUM:DAT:1071` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-2-high-resolution` | `EO:EUM:DAT:0841` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-2-high-resolution-2` | `EO:EUM:DAT:1072` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-2-low-resolution` | `EO:EUM:DAT:0842` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-poseidon-4-altimetry-2-low-resolution-2` | `EO:EUM:DAT:1074` | raster | netcdf | subdaily | 2020-12-17 | — |
| `s6-radio-occultation-1b` | `EO:EUM:DAT:0853` | raster | netcdf | subdaily | 2021-11-29 | — |
| `s6-rinex-auxiliary` | `EO:EUM:DAT:0274` | raster | rinex | irregular | 2020-04-16 | — |

### OSI-SAF (4)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `osi-saf-amsr2-global-sea-ice-concentration-interim` | `EO:EUM:DAT:1064` | raster | netcdf | irregular | 2021-01-01 | — |
| `osi-saf-global-sea-ice-concentration-interim-climate` | `EO:EUM:DAT:0645` | raster | netcdf | irregular | 2023-09-03 | — |
| `osi-saf-metop-sst` | `EO:EUM:DAT:METOP:GLB-SST-NC` | raster | netcdf | subdaily | 2016-01-01 | GLB-SST-NC |
| `osi-saf-sea-ice-conc` | `EO:EUM:DAT:DMSP:OSI-401-B` | raster | netcdf | daily | 2015-06-01 | OSI-401-b |

### Other (15)

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `eum-atms-1c-climate-record-release-1` | `EO:EUM:DAT:0345` | raster | native | irregular | 2011-12-10 | — |
| `eum-auxiliary-tle-aws-pfm` | `EO:EUM:DAT:0908` | raster | other | irregular | 2025-02-22 | — |
| `eum-cm-saf-passive-microwave-upper-tropospheric` | `EO:EUM:DAT:0910` | raster | netcdf | irregular | 1994-07-05 | — |
| `eum-commercial-radio-occultation-1b` | `EO:EUM:DAT:0374` | raster | netcdf | subdaily | 2022-02-15 | — |
| `eum-deep-convective-cloud-system-monitoring-database` | `EO:EUM:DAT:0986` | raster | netcdf | subdaily | 1981-08-01 | — |
| `eum-deep-convective-cloud-system-monitoring-database-2` | `EO:EUM:DAT:0987` | raster | netcdf | subdaily | 1981-01-01 | — |
| `eum-girafe-v1-cm-saf-global-interpolated` | `EO:EUM:DAT:0921` | raster | netcdf | subdaily | 2002-01-01 | — |
| `eum-hirs-1c-fundamental-record-release-2` | `EO:EUM:DAT:0961` | raster | native | irregular | 1978-11-03 | — |
| `eum-mwhs-1-1c-climate-record-release` | `EO:EUM:DAT:0348` | raster | native | irregular | 2008-07-01 | — |
| `eum-mwhs-2-1c-climate-record-release` | `EO:EUM:DAT:0349` | raster | native | irregular | 2013-09-30 | — |
| `eum-mwr-1b-arctic-weather-satellite-proto` | `EO:EUM:DAT:0905` | raster | netcdf | subdaily | 2000-01-01 | — |
| `eum-si-1-fundamental-record-release-1` | `EO:EUM:DAT:0963` | raster | netcdf | irregular | 1977-07-05 | — |
| `eum-ssm-t-1c-fundamental-record-release` | `EO:EUM:DAT:0964` | raster | native | irregular | 1991-07-15 | — |
| `eum-ssm-t-2-microwave-humidity-sounder` | `EO:EUM:DAT:0343` | raster | netcdf | irregular | 1994-07-05 | — |
| `eum-surface-radiation-set` | `EO:EUM:DAT:0863` | raster | netcdf | subdaily | 1983-01-01 | — |

_Total curated datasets: 180 across 10 groups — the **entire** EUMETSAT Data Store catalog is curated. The `available_datasets` index lists the same 180 ids (walked from the public browse endpoint)._

## Catalog tooling

The scripts under `tools/eumetsat/` (not shipped in the wheel) use the
**public** Data Store browse endpoint, so they need **no credentials**.

### Refresh the index

```bash
# Rebuild available_datasets from the public browse endpoint (all ~180)
pixi run -e dev python tools/eumetsat/refresh_eumetsat_catalog.py refresh

# Emit a curated stanza for one collection (paste into a per-group file)
pixi run -e dev python tools/eumetsat/refresh_eumetsat_catalog.py \
    add-collection msg-hrseviri EO:EUM:DAT:MSG:HRSEVIRI --group MSG
```

### Audit curated vs live

```bash
pixi run -e dev python tools/eumetsat/audit_eumetsat_catalog.py --strict
```

The audit reports **GONE** (a curated id the live store no longer lists),
**INDEX-GONE** (an index id no longer live), and **NEW** (a live id absent
from the index). `--strict` exits non-zero on any drift, for CI.

### Probe one collection

```bash
# Print a collection's public metadata (title, abstract, date range);
# accepts a real id or a curated key. --out writes the full JSON.
pixi run -e dev python tools/eumetsat/probe_eumetsat_product.py msg-hrseviri
```
