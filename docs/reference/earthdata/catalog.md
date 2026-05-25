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
