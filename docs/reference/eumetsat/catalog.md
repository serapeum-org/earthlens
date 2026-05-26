# EUMETSAT Data Store — catalog & tooling

The EUMETSAT catalog ships as package data: a directory of per-group YAML
files at `src/earthlens/eumetsat/catalog/` (`mtg.yaml`, `msg.yaml`,
`metop.yaml`, `sentinel3.yaml`, `sentinel5p.yaml`, `sentinel6.yaml`,
`osisaf.yaml`) plus a single `_index.yaml` holding the informational
`available_collections:` index. A duplicate-key-rejecting loader merges
them into one `Catalog`.

Each row maps a **friendly key** (what you pass in `variables=`) to the
real Data Store `EO:EUM:DAT:…` collection id, plus the group, output
kind, on-disk format, native selectors, the Data Tailor product type (for
the deferred server-side path), and the spatial / temporal coverage.

```python
from earthlens.eumetsat import Catalog

cat = Catalog()
col = cat.get_collection("msg-hrseviri")
col.collection_id      # 'EO:EUM:DAT:MSG:HRSEVIRI'
col.group.value        # 'MSG'
col.format             # 'native'
```

An unknown key raises `ValueError` with a did-you-mean hint; `resolve(key,
group=...)` additionally asserts the row's Data Store group.

## Curated collections

MTG and Metop-SG are curated in full; the other groups carry a high-value
subset. The full `available_collections` index (all ~180 Data Store
collections) is regenerated from the public browse endpoint by the refresh
tool. All curated ids were verified against
`api.eumetsat.int/data/browse/collections` on 2026-05-26.

### MTG

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

### MSG

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `msg-cloud-mask` | `EO:EUM:DAT:MSG:CLM` | raster | grib | 15min | 2004-01-19 | CLM |
| `msg-cloud-mask-iodc` | `EO:EUM:DAT:MSG:CLM-IODC` | raster | grib | 15min | 2017-02-01 | CLM |
| `msg-hrseviri` | `EO:EUM:DAT:MSG:HRSEVIRI` | raster | native | 15min | 2004-01-19 | HRSEVIRI |
| `msg-hrseviri-iodc` | `EO:EUM:DAT:MSG:HRSEVIRI-IODC` | raster | native | 15min | 2017-02-01 | HRSEVIRI |
| `msg-rss` | `EO:EUM:DAT:MSG:MSG15-RSS` | raster | native | 5min | 2008-05-13 | HRSEVIRI |

### Metop

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `metop-amsua-l1` | `EO:EUM:DAT:METOP:AMSUL1` | raster | native | subdaily | 2006-10-19 | AMSUL1 |
| `metop-ascat-l1-szf` | `EO:EUM:DAT:METOP:ASCSZF1B` | raster | native | subdaily | 2007-01-01 | ASCSZF1B |
| `metop-ascat-l1-szr` | `EO:EUM:DAT:METOP:ASCSZR1B` | raster | native | subdaily | 2007-01-01 | ASCSZR1B |
| `metop-ascat-soil-moisture-12km` | `EO:EUM:DAT:METOP:SOMO12` | raster | bufr | subdaily | 2007-01-01 | SOMO12 |
| `metop-ascat-soil-moisture-25km` | `EO:EUM:DAT:METOP:SOMO25` | raster | bufr | subdaily | 2007-01-01 | SOMO25 |
| `metop-avhrr-l1` | `EO:EUM:DAT:METOP:AVHRRL1` | raster | native | subdaily | 2006-10-19 | AVHRRL1 |
| `metop-gome2-l1` | `EO:EUM:DAT:METOP:GOMEL1` | raster | native | subdaily | 2007-01-09 | GOMEL1 |
| `metop-iasi-l1c` | `EO:EUM:DAT:METOP:IASIL1C-ALL` | raster | native | subdaily | 2007-05-29 | IASIL1C |
| `metop-iasi-l2` | `EO:EUM:DAT:METOP:IASSND02` | raster | bufr | subdaily | 2008-01-01 | IASSND02 |
| `metop-mhs-l1` | `EO:EUM:DAT:METOP:MHSL1` | raster | native | subdaily | 2006-10-19 | MHSL1 |

### Metop-SG

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `metopsg-aux` | `EO:EUM:DAT:0520` | raster | other | irregular | 2026-04-28 | AUX |
| `metopsg-bufr` | `EO:EUM:DAT:0892` | raster | bufr | subdaily | 2026-02-26 | BUFR |
| `metopsg-gras2-l1b` | `EO:EUM:DAT:0452` | raster | netcdf | subdaily | 2026-02-26 | GRAS-L1B |

### Sentinel-3

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `s3-olci-l1-efr` | `EO:EUM:DAT:0409` | raster | netcdf | subdaily | 2016-04-25 | OL_1_EFR |
| `s3-olci-l2-wfr` | `EO:EUM:DAT:0407` | raster | netcdf | subdaily | 2016-04-25 | OL_2_WFR |
| `s3-olci-l2-wrr` | `EO:EUM:DAT:0408` | raster | netcdf | subdaily | 2016-04-25 | OL_2_WRR |
| `s3-slstr-l1-rbt` | `EO:EUM:DAT:0411` | raster | netcdf | subdaily | 2016-05-01 | SL_1_RBT |
| `s3-slstr-l2-wst` | `EO:EUM:DAT:0412` | raster | netcdf | subdaily | 2016-05-01 | SL_2_WST |
| `s3-sral-l2-wat` | `EO:EUM:DAT:0415` | raster | netcdf | subdaily | 2016-03-01 | SR_2_WAT |

### Sentinel-5P

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `s5p-l2-ch4` | `EO:EUM:DAT:1101` | raster | netcdf | subdaily | 2018-04-30 | L2__CH4___ |
| `s5p-l2-co` | `EO:EUM:DAT:0073` | raster | netcdf | subdaily | 2018-04-30 | L2__CO____ |
| `s5p-l2-no2` | `EO:EUM:DAT:0076` | raster | netcdf | subdaily | 2018-04-30 | L2__NO2___ |
| `s5p-l2-o3` | `EO:EUM:DAT:0077` | raster | netcdf | subdaily | 2018-04-30 | L2__O3____ |

### Sentinel-6

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `s6-p4-l2-hr` | `EO:EUM:DAT:0855` | raster | netcdf | subdaily | 2020-12-17 | P4_2__HR_ |
| `s6-p4-l2-lr` | `EO:EUM:DAT:0854` | raster | netcdf | subdaily | 2020-12-17 | P4_2__LR_ |

### OSI-SAF

| Key | Collection id | Kind | Format | Cadence | Coverage start | Selectors |
|-----|---------------|------|--------|---------|----------------|-----------|
| `osi-saf-metop-sst` | `EO:EUM:DAT:METOP:GLB-SST-NC` | raster | netcdf | subdaily | 2016-01-01 | GLB-SST-NC |
| `osi-saf-sea-ice-conc` | `EO:EUM:DAT:DMSP:OSI-401-B` | raster | netcdf | daily | 2015-06-01 | OSI-401-b |

_Total curated collections: 59 across 8 groups (MTG and Metop-SG fully curated; others are high-value subsets); the informational `available_collections` index lists all 180 Data Store collections (walked from the public browse endpoint)._

## Catalog tooling

The scripts under `tools/eumetsat/` (not shipped in the wheel) use the
**public** Data Store browse endpoint, so they need **no credentials**.

### Refresh the index

```bash
# Rebuild available_collections from the public browse endpoint (all ~180)
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
