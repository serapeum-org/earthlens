# NASA Earthdata — catalog & tooling

The Earthdata backend ships a **curated** catalog: a small, vetted set
of flagship collections across all nine DAACs, plus an auto-generated
index of the long tail. A dataset key (e.g. `"GPM_3IMERGHHL_07"`)
resolves to an `EarthdataDataset` row carrying the `short_name` /
`version` / `provider` for the CMR search, the per-dataset
`output_kind`, the on-disk `format`, the cloud-hosting flags, and an
informational `bands` map.

## Curated datasets

| Dataset key | DAAC | CMR provider | Format | Output kind | Cloud-hosted |
|-------------|------|-------------|--------|-------------|--------------|
| `CER_SSF1deg-Day_Terra-MODIS_Edition4A` | ASDC | `LARC_CLOUD` | netcdf4 | raster | yes |
| `CER_SSF_Terra-FM1-MODIS_Edition4A` | ASDC | `LARC_CLOUD` | hdf4 | raster | yes |
| `CER_SYN1deg-Day_Terra-Aqua-MODIS_Edition4A` | ASDC | `LARC_CLOUD` | netcdf4 | raster | yes |
| `TEMPO_NO2_L3_V04` | ASDC | `LARC_CLOUD` | netcdf4 | raster | yes |
| `OPERA_L2_CSLC-S1_V1` | ASF | `ASF` | hdf5 | raster | yes |
| `OPERA_L2_RTC-S1_V1` | ASF | `ASF` | cog | raster | yes |
| `AIRX3STD_7` | GES DISC | `GES_DISC` | hdf-eos2 | raster | yes |
| `GLDAS_NOAH025_3H_21` | GES DISC | `GES_DISC` | netcdf4 | raster | yes |
| `GPM_3IMERGDF_07` | GES DISC | `GES_DISC` | hdf5 | raster | yes |
| `GPM_3IMERGHHL_07` | GES DISC | `GES_DISC` | hdf5 | raster | yes |
| `GPM_3IMERGM_07` | GES DISC | `GES_DISC` | hdf5 | raster | yes |
| `M2T1NXSLV_5124` | GES DISC | `GES_DISC` | netcdf4 | raster | yes |
| `M2TMNXSLV_5124` | GES DISC | `GES_DISC` | netcdf4 | raster | yes |
| `OCO2_L2_Lite_FP_112r` | GES DISC | `GES_DISC` | netcdf4 | **vector** | yes |
| `OMNO2d_004` | GES DISC | `GES_DISC` | hdf-eos5 | raster | yes |
| `MOD021KM_61` | LAADS | `LAADS` | hdf-eos2 | raster | yes |
| `MOD04_L2_61` | LAADS | `LAADS` | hdf-eos2 | raster | yes |
| `VNP46A1_2` | LAADS | `LAADS` | hdf-eos5 | raster | yes |
| `VNP46A2_2` | LAADS | `LAADS` | hdf-eos5 | raster | yes |
| `ASTGTM_003` | LP DAAC | `LPCLOUD` | geotiff | raster | yes |
| `ECO_L2T_LSTE_002` | LP DAAC | `LPCLOUD` | cog | raster | yes |
| `EMITL2ARFL_001` | LP DAAC | `LPCLOUD` | netcdf4 | raster | yes |
| `GEDI02_A_002` | LP DAAC | `LPCLOUD` | hdf5 | **vector** | yes |
| `HLSS30_20` | LP DAAC | `LPCLOUD` | cog | raster | yes |
| `MCD12Q1_061` | LP DAAC | `LPCLOUD` | hdf-eos2 | raster | yes |
| `MOD09GA_061` | LP DAAC | `LPCLOUD` | hdf-eos2 | raster | yes |
| `MOD11A1_061` | LP DAAC | `LPCLOUD` | hdf-eos2 | raster | yes |
| `MOD13Q1_061` | LP DAAC | `LPCLOUD` | hdf-eos2 | raster | yes |
| `NASADEM_HGT_001` | LP DAAC | `LPCLOUD` | zip | raster | yes |
| `ATL06_006` | NSIDC | `NSIDC_CPRD` | hdf5 | **vector** | yes |
| `ATL08_006` | NSIDC | `NSIDC_CPRD` | hdf5 | **vector** | yes |
| `ATL10_007` | NSIDC | `NSIDC_CPRD` | hdf5 | **vector** | yes |
| `MOD10A1_61` | NSIDC | `NSIDC_CPRD` | hdf-eos2 | raster | yes |
| `NSIDC-0051_2` | NSIDC | `NSIDC_CPRD` | netcdf4 | raster | yes |
| `SPL3SMP_E_006` | NSIDC | `NSIDC_CPRD` | hdf5 | raster | yes |
| `SPL4SMGP_008` | NSIDC | `NSIDC_CPRD` | hdf5 | raster | yes |
| `PACE_OCI_L2_AOP_32` | OB.DAAC | `OB_CLOUD` | netcdf4 | raster | yes |
| `PACE_OCI_L3M_CHL_31` | OB.DAAC | `OB_CLOUD` | netcdf4 | raster | yes |
| `Daymet_Daily_V4R1_2129` | ORNL DAAC | `ORNL_CLOUD` | netcdf4 | raster | yes |
| `FLUXNET_Canada_1335` | ORNL DAAC | `ORNL_CLOUD` | csv | **tabular** | yes |
| `GEDI_L4A_AGB_Density_V2_1_2056` | ORNL DAAC | `ORNL_CLOUD` | hdf5 | **vector** | yes |
| `GEDI_L4B_Gridded_Biomass_V2_1_2299` | ORNL DAAC | `ORNL_CLOUD` | geotiff | raster | yes |
| `MUR-JPL-L4-GLOB-v4.1` | PO.DAAC | `POCLOUD` | netcdf4 | raster | yes |
| `OISSS_L4_multimission_monthly_v2` | PO.DAAC | `POCLOUD` | netcdf4 | raster | yes |
| `SEA_SURFACE_HEIGHT_ALT_GRIDS_L4_2SATS_5DAY_6THDEG_V_JPL2205` | PO.DAAC | `POCLOUD` | netcdf4 | raster | yes |
| `SMAP_RSS_L3_SSS_SMI_8DAY-RUNNINGMEAN_V5` | PO.DAAC | `POCLOUD` | netcdf4 | raster | yes |

Inspect the curated rows programmatically:

```python
from earthlens.earthdata import Catalog

cat = Catalog()
cat.get_dataset("GPM_3IMERGHHL_07").output_kind   # 'raster'
cat.get_daac("POCLOUD").cloud_region              # 'us-west-2'
len(cat.datasets)                                  # 46 curated rows
```

## The auto long tail (every collection resolvable)

The catalog is a **hybrid**: the ~46 rows above are hand-vetted (correct
`output_kind`, `format`, representative `bands`, validated against live
CMR). The **rest of the ~8,000 collections** the DAACs serve are
machine-derived rows in `catalog/_auto.json` — real `short_name` /
`version` / `provider` / `daac` from a CMR walk, plus a **heuristic**
`output_kind` and **no band metadata**. They are *not* hand-vetted.

`Catalog.get_dataset` resolves the curated rows first, then falls back to
the auto map, so **all ~8,029 collections are usable** by short_name:

```python
cat.get_dataset("GPM_3IMERGHHL_07")   # curated (vetted, with bands)
cat.get_dataset("AA_L2A")             # auto (machine-derived fallback)
len(cat._auto_rows())                  # ~7,983 auto rows
```

The auto rows are read lazily and stored as JSON so the ~8k entries
parse in milliseconds and stay out of the curated YAML. To promote one
into a vetted curated row, use the `add-dataset` / `probe` tools below.

A dataset key outside both maps raises with a *did-you-mean* hint.

## Maintenance tooling

Three scripts under `tools/earthdata/` (the CMR analogs of the GEE
catalog tooling) keep the catalog honest. They lazy-import
`earthaccess`, so `--help` works without the extra; the live
subcommands need `earthlens[earthdata]` (Python ≥ 3.12).

- **`refresh_earthdata_catalog.py`** — `refresh` walks CMR per provider
  and rewrites the `available_datasets:` index; `add-dataset` emits a
  ready-to-paste curated stanza with an inferred `output_kind` /
  `format` (vet by hand).
- **`audit_earthdata_datasets.py`** — diffs the curated rows against
  live CMR (gone collections, version drift); `--strict` for CI.
- **`probe_earthdata_granule.py`** — fetches one sample granule for a
  collection and writes a JSON sidecar seeding `format` / `output_kind`.

## Deferred features

The MVP fetches **whole granules**. Two capabilities are intentionally
**out of scope** for now and tracked by informational catalog flags:

- **Harmony server-side subsetting** (`harmony-py`) — spatial / variable
  / reprojected subsetting for the DAACs that support it. The catalog
  rows carry `requires_harmony_for_subset` and `supports_harmony` flags
  so a future `harmony.py` helper knows which collections qualify. Until
  then, band names in a request are informational and you receive the
  full granule.
- **ASF `asf_search` stack search** — the richer InSAR / burst stack
  semantics ASF offers. ASF collections are still reachable here through
  `earthaccess` + `daac="ASF"` for whole-granule fetch; a dedicated
  `earthlens.asf` spin-off is post-MVP.
