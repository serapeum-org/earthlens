# JAXA — available datasets

The bundled catalog ships **124 rows**: 118 `jaxa-earth` STAC collections plus 6 `gportal` mission products. Every row has a short, friendly canonical key; the long auto-derived slug and a few high-traffic English names also resolve via `cat.get(...)`.

The naming scheme for jaxa-earth is `<mission>-<product>[-<d|n>][-<cadence>][-norm]` where `d`/`n` mark daytime / nighttime variants, the cadence comes from the source (`daily`, `halfmonth`, `monthly`, `8day`, `hourly`, `yearly`), and `-norm` flags a climatological *normal*.

Resolve either form:

```python
>>> from earthlens.jaxa import Catalog
>>> cat = Catalog()
>>> cat.resolve('elevation')          # -> 'aw3d30'
>>> cat.resolve('aw3d30')             # -> 'aw3d30'
>>> cat.resolve('alos-prism-aw3d30-v3-2-global')   # -> 'aw3d30'
```

An unknown but close friendly name raises with a did-you-mean hint.

## jaxa-earth (STAC + COG via the official `jaxa.earth` API)

Open access, no credentials. The 14 mission families with their row counts:

| Family | Count | Description |
|---|---|---|
| `alos2` | 1 | ALOS-2 PALSAR-2 forest / non-forest |
| `amsr2` | 20 | GCOM-W AMSR2 L3 soil-moisture, sea-ice, SSW, SST + JASMES sea-ice |
| `amsre` | 6 | Aqua AMSR-E L3 soil-moisture re-host |
| `aw3d30` | 2 | ALOS PRISM AW3D30 global DSM (v3.2 + v4.1) |
| `gsmap` | 7 | GSMaP precipitation (standard hourly + climatological normals) |
| `hrlulc` | 1 | HRLULC high-resolution Japan land use / land cover |
| `mod11` | 8 | NASA EOSDIS Terra MODIS C1 LST |
| `mod11c3` | 2 | NASA EOSDIS Terra MODIS C3 LST |
| `modis` | 19 | JASMES Terra/Aqua MODIS family (NDVI, surface shortwave radiation, aerosol) |
| `myd11` | 4 | NASA EOSDIS Aqua MODIS C1 LST |
| `proba` | 1 | Copernicus C3S PROBA-V land cover (LCCS) |
| `sgli` | 39 | GCOM-C SGLI L3/L2 family (LST, NDVI, chlorophyll, aerosol, SST, ET, ETidx) |
| `spi` | 1 | GSMaP-derived SPI drought index |
| `temsm` | 7 | UTokyo TE-MSM Japan river / flood / outflow forecasts (7 variables) |

### Full list

| Key | Aliases | STAC collection | Default band |
|---|---|---|---|
| `alos2-fnf` | `alos-2-palsar-2-fnf-v2-1-0-global-yearly`, `fnf`, `forest-non-forest` | `JAXA.EORC_ALOS-2.PALSAR-2_FNF.v2.1.0_global_yearly` | `FNF` |
| `amsr2-seaice-north-daily` | `jasmes-gcom-w-amsr2-ic0-v201-north-daily` | `JAXA.JASMES_GCOM-W.AMSR2_ic0.v201_north_daily` | `IC0` |
| `amsr2-seaice-south-daily` | `jasmes-gcom-w-amsr2-ic0-v201-south-daily` | `JAXA.JASMES_GCOM-W.AMSR2_ic0.v201_south_daily` | `IC0` |
| `amsr2-smc-d-daily` | `gp-gcom-w-amsr2-l3-smc-daytime-v3-global-daily` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.daytime.v3_global_daily` | `SMC` |
| `amsr2-smc-d-halfmonth` | `gp-gcom-w-amsr2-l3-smc-daytime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.daytime.v3_global_half-monthly` | `SMC` |
| `amsr2-smc-d-halfmonth-norm` | `gp-gcom-w-amsr2-l3-smc-daytime-v3-global-half-monthly-normal` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.daytime.v3_global_half-monthly-normal` | `SMC_2012_2021` |
| `amsr2-smc-d-monthly` | `gp-gcom-w-amsr2-l3-smc-daytime-v3-global-monthly` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.daytime.v3_global_monthly` | `SMC` |
| `amsr2-smc-d-monthly-norm` | `gp-gcom-w-amsr2-l3-smc-daytime-v3-global-monthly-normal` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.daytime.v3_global_monthly-normal` | `SMC_2012_2021` |
| `amsr2-smc-n-daily` | `gp-gcom-w-amsr2-l3-smc-nighttime-v3-global-daily` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.nighttime.v3_global_daily` | `SMC` |
| `amsr2-smc-n-halfmonth` | `gp-gcom-w-amsr2-l3-smc-nighttime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.nighttime.v3_global_half-monthly` | `SMC` |
| `amsr2-smc-n-halfmonth-norm` | `gp-gcom-w-amsr2-l3-smc-nighttime-v3-global-half-monthly-normal` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.nighttime.v3_global_half-monthly-normal` | `SMC_2012_2021` |
| `amsr2-smc-n-monthly` | `gp-gcom-w-amsr2-l3-smc-nighttime-v3-global-monthly` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.nighttime.v3_global_monthly` | `SMC` |
| `amsr2-smc-n-monthly-norm` | `gp-gcom-w-amsr2-l3-smc-nighttime-v3-global-monthly-normal` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SMC.nighttime.v3_global_monthly-normal` | `SMC_2012_2021` |
| `amsr2-sst-d-daily` | `gp-gcom-w-amsr2-l3-sst-daytime-v4-global-daily` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SST.daytime.v4_global_daily` | `SST` |
| `amsr2-sst-n-daily` | `gp-gcom-w-amsr2-l3-sst-nighttime-v4-global-daily` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SST.nighttime.v4_global_daily` | `SST` |
| `amsr2-ssw-d-daily` | `gp-gcom-w-amsr2-l3-ssw-daytime-v4-global-daily` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SSW.daytime.v4_global_daily` | `SSW` |
| `amsr2-ssw-d-halfmonth` | `gp-gcom-w-amsr2-l3-ssw-daytime-v4-global-half-monthly` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SSW.daytime.v4_global_half-monthly` | `SSW` |
| `amsr2-ssw-d-monthly` | `gp-gcom-w-amsr2-l3-ssw-daytime-v4-global-monthly` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SSW.daytime.v4_global_monthly` | `SSW` |
| `amsr2-ssw-n-daily` | `gp-gcom-w-amsr2-l3-ssw-nighttime-v4-global-daily` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SSW.nighttime.v4_global_daily` | `SSW` |
| `amsr2-ssw-n-halfmonth` | `gp-gcom-w-amsr2-l3-ssw-nighttime-v4-global-half-monthly` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SSW.nighttime.v4_global_half-monthly` | `SSW` |
| `amsr2-ssw-n-monthly` | `gp-gcom-w-amsr2-l3-ssw-nighttime-v4-global-monthly` | `JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SSW.nighttime.v4_global_monthly` | `SSW` |
| `amsre-smc-d-daily` | `gp-aqua-amsr-e-l3-smc-daytime-v3-global-daily` | `JAXA.G-Portal_Aqua.AMSR-E_standard.L3-SMC.daytime.v3_global_daily` | `SMC` |
| `amsre-smc-d-halfmonth` | `gp-aqua-amsr-e-l3-smc-daytime-v3-global-half-monthly` | `JAXA.G-Portal_Aqua.AMSR-E_standard.L3-SMC.daytime.v3_global_half-monthly` | `SMC` |
| `amsre-smc-d-monthly` | `gp-aqua-amsr-e-l3-smc-daytime-v3-global-monthly` | `JAXA.G-Portal_Aqua.AMSR-E_standard.L3-SMC.daytime.v3_global_monthly` | `SMC` |
| `amsre-smc-n-daily` | `gp-aqua-amsr-e-l3-smc-nighttime-v3-global-daily` | `JAXA.G-Portal_Aqua.AMSR-E_standard.L3-SMC.nighttime.v3_global_daily` | `SMC` |
| `amsre-smc-n-halfmonth` | `gp-aqua-amsr-e-l3-smc-nighttime-v3-global-half-monthly` | `JAXA.G-Portal_Aqua.AMSR-E_standard.L3-SMC.nighttime.v3_global_half-monthly` | `SMC` |
| `amsre-smc-n-monthly` | `gp-aqua-amsr-e-l3-smc-nighttime-v3-global-monthly` | `JAXA.G-Portal_Aqua.AMSR-E_standard.L3-SMC.nighttime.v3_global_monthly` | `SMC` |
| `aw3d30` | `alos-prism-aw3d30-v3-2-global`, `elevation`, `dem`, `alos-aw3d30` | `JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global` | `DSM` |
| `aw3d30-v41` | `alos-prism-aw3d30-v4-1-global`, `elevation-v41`, `dem-v41` | `JAXA.EORC_ALOS.PRISM_AW3D30.v4.1_global` | `DSM` |
| `gsmap` | `gsmap-gauge-v6-hourly`, `precipitation`, `rainfall` | `JAXA.EORC_GSMaP_standard.Gauge.v6_hourly` | `PRECIP` |
| `gsmap-daily` | `gsmap-gauge-00z-23z-v6-daily` | `JAXA.EORC_GSMaP_standard.Gauge.00Z-23Z.v6_daily` | `PRECIP` |
| `gsmap-halfmonth` | `gsmap-gauge-00z-23z-v6-half-monthly` | `JAXA.EORC_GSMaP_standard.Gauge.00Z-23Z.v6_half-monthly` | `PRECIP` |
| `gsmap-halfmonth-norm` | `gsmap-gauge-00z-23z-v6-half-monthly-normal` | `JAXA.EORC_GSMaP_standard.Gauge.00Z-23Z.v6_half-monthly-normal` | `PRECIP_2012_2021` |
| `gsmap-monthly` | `gsmap-gauge-00z-23z-v6-monthly` | `JAXA.EORC_GSMaP_standard.Gauge.00Z-23Z.v6_monthly` | `PRECIP` |
| `gsmap-monthly-norm` | `gsmap-gauge-00z-23z-v6-monthly-normal` | `JAXA.EORC_GSMaP_standard.Gauge.00Z-23Z.v6_monthly-normal` | `PRECIP_2012_2021` |
| `gsmap-nrt` | `gsmap-nrt-gauge-ext-00z-23z-v6-daily` | `JAXA.EORC_GSMaP_NRT.Gauge.Ext.00Z-23Z.v6_daily` | `EXT` |
| `hrlulc` | `alos-hrlulc-v25-04-japan`, `lulc` | `JAXA.EORC_ALOS_HRLULC.v25.04_japan` | `HRLULC` |
| `mod11-lst-d-daily` | `eosdis-terra-modis-mod11c1-lst-daytime-v061-global-daily` | `NASA.EOSDIS_Terra.MODIS_MOD11C1-LST.daytime.v061_global_daily` | `LST` |
| `mod11-lst-d-halfmonth` | `eosdis-terra-modis-mod11c1-lst-daytime-v061-global-half-monthly` | `NASA.EOSDIS_Terra.MODIS_MOD11C1-LST.daytime.v061_global_half-monthly` | `LST` |
| `mod11-lst-d-halfmonth-norm` | `eosdis-terra-modis-mod11c1-lst-daytime-v061-global-half-monthly-normal` | `NASA.EOSDIS_Terra.MODIS_MOD11C1-LST.daytime.v061_global_half-monthly-normal` | `LST_2012_2021` |
| `mod11-lst-d-monthly-norm` | `eosdis-terra-modis-mod11c1-lst-daytime-v061-global-monthly-normal` | `NASA.EOSDIS_Terra.MODIS_MOD11C1-LST.daytime.v061_global_monthly-normal` | `LST_2012_2021` |
| `mod11-lst-n-daily` | `eosdis-terra-modis-mod11c1-lst-nighttime-v061-global-daily` | `NASA.EOSDIS_Terra.MODIS_MOD11C1-LST.nighttime.v061_global_daily` | `LST` |
| `mod11-lst-n-halfmonth` | `eosdis-terra-modis-mod11c1-lst-nighttime-v061-global-half-monthly` | `NASA.EOSDIS_Terra.MODIS_MOD11C1-LST.nighttime.v061_global_half-monthly` | `LST` |
| `mod11-lst-n-halfmonth-norm` | `eosdis-terra-modis-mod11c1-lst-nighttime-v061-global-half-monthly-normal` | `NASA.EOSDIS_Terra.MODIS_MOD11C1-LST.nighttime.v061_global_half-monthly-normal` | `LST_2012_2021` |
| `mod11-lst-n-monthly-norm` | `eosdis-terra-modis-mod11c1-lst-nighttime-v061-global-monthly-normal` | `NASA.EOSDIS_Terra.MODIS_MOD11C1-LST.nighttime.v061_global_monthly-normal` | `LST_2012_2021` |
| `mod11c3-lst-d-monthly` | `eosdis-terra-modis-mod11c3-lst-daytime-v061-global-monthly` | `NASA.EOSDIS_Terra.MODIS_MOD11C3-LST.daytime.v061_global_monthly` | `LST` |
| `mod11c3-lst-n-monthly` | `eosdis-terra-modis-mod11c3-lst-nighttime-v061-global-monthly` | `NASA.EOSDIS_Terra.MODIS_MOD11C3-LST.nighttime.v061_global_monthly` | `LST` |
| `modis-aqua-swr-daily` | `jasmes-aqua-modis-swr-v811-global-daily` | `JAXA.JASMES_Aqua.MODIS_swr.v811_global_daily` | `swr` |
| `modis-aqua-swr-halfmonth` | `jasmes-aqua-modis-swr-v811-global-half-monthly` | `JAXA.JASMES_Aqua.MODIS_swr.v811_global_half-monthly` | `swr` |
| `modis-aqua-swr-halfmonth-norm` | `jasmes-aqua-modis-swr-v811-global-half-monthly-normal` | `JAXA.JASMES_Aqua.MODIS_swr.v811_global_half-monthly-normal` | `swr_2012_2021` |
| `modis-aqua-swr-monthly` | `jasmes-aqua-modis-swr-v811-global-monthly` | `JAXA.JASMES_Aqua.MODIS_swr.v811_global_monthly` | `swr` |
| `modis-aqua-swr-monthly-norm` | `jasmes-aqua-modis-swr-v811-global-monthly-normal` | `JAXA.JASMES_Aqua.MODIS_swr.v811_global_monthly-normal` | `swr_2012_2021` |
| `modis-ndvi-halfmonth` | `jasmes-terra-modis-aqua-modis-ndvi-v811-global-half-monthly` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_ndvi.v811_global_half-monthly` | `ndvi` |
| `modis-ndvi-halfmonth-norm` | `jasmes-terra-modis-aqua-modis-ndvi-v811-global-half-monthly-normal` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_ndvi.v811_global_half-monthly-normal` | `ndvi_2012_2021` |
| `modis-ndvi-monthly` | `jasmes-terra-modis-aqua-modis-ndvi-v811-global-monthly` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_ndvi.v811_global_monthly` | `ndvi` |
| `modis-ndvi-monthly-norm` | `jasmes-terra-modis-aqua-modis-ndvi-v811-global-monthly-normal` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_ndvi.v811_global_monthly-normal` | `ndvi_2012_2021` |
| `modis-taua-daily` | `jasmes-terra-modis-aqua-modis-taua-v811-global-daily` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_taua.v811_global_daily` | `T500` |
| `modis-taua-halfmonth` | `jasmes-terra-modis-aqua-modis-taua-v811-global-half-monthly` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_taua.v811_global_half-monthly` | `T500` |
| `modis-taua-halfmonth-norm` | `jasmes-terra-modis-aqua-modis-taua-v811-global-half-monthly-normal` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_taua.v811_global_half-monthly-normal` | `taua_2012_2021` |
| `modis-taua-monthly` | `jasmes-terra-modis-aqua-modis-taua-v811-global-monthly` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_taua.v811_global_monthly` | `T500` |
| `modis-taua-monthly-norm` | `jasmes-terra-modis-aqua-modis-taua-v811-global-monthly-normal` | `JAXA.JASMES_Terra.MODIS-Aqua.MODIS_taua.v811_global_monthly-normal` | `taua_2012_2021` |
| `modis-terra-swr-daily` | `jasmes-terra-modis-swr-v811-global-daily` | `JAXA.JASMES_Terra.MODIS_swr.v811_global_daily` | `swr` |
| `modis-terra-swr-halfmonth` | `jasmes-terra-modis-swr-v811-global-half-monthly` | `JAXA.JASMES_Terra.MODIS_swr.v811_global_half-monthly` | `swr` |
| `modis-terra-swr-halfmonth-norm` | `jasmes-terra-modis-swr-v811-global-half-monthly-normal` | `JAXA.JASMES_Terra.MODIS_swr.v811_global_half-monthly-normal` | `swr_2012_2021` |
| `modis-terra-swr-monthly` | `jasmes-terra-modis-swr-v811-global-monthly` | `JAXA.JASMES_Terra.MODIS_swr.v811_global_monthly` | `swr` |
| `modis-terra-swr-monthly-norm` | `jasmes-terra-modis-swr-v811-global-monthly-normal` | `JAXA.JASMES_Terra.MODIS_swr.v811_global_monthly-normal` | `swr_2012_2021` |
| `myd11-lst-d-halfmonth-norm` | `eosdis-aqua-modis-myd11c1-lst-daytime-v061-global-half-monthly-normal` | `NASA.EOSDIS_Aqua.MODIS_MYD11C1-LST.daytime.v061_global_half-monthly-normal` | `LST_2012_2021` |
| `myd11-lst-d-monthly-norm` | `eosdis-aqua-modis-myd11c1-lst-daytime-v061-global-monthly-normal` | `NASA.EOSDIS_Aqua.MODIS_MYD11C1-LST.daytime.v061_global_monthly-normal` | `LST_2012_2021` |
| `myd11-lst-n-halfmonth-norm` | `eosdis-aqua-modis-myd11c1-lst-nighttime-v061-global-half-monthly-normal` | `NASA.EOSDIS_Aqua.MODIS_MYD11C1-LST.nighttime.v061_global_half-monthly-normal` | `LST_2012_2021` |
| `myd11-lst-n-monthly-norm` | `eosdis-aqua-modis-myd11c1-lst-nighttime-v061-global-monthly-normal` | `NASA.EOSDIS_Aqua.MODIS_MYD11C1-LST.nighttime.v061_global_monthly-normal` | `LST_2012_2021` |
| `proba-v-lccs` | `c3s-proba-v-lccs-global-yearly`, `lccs`, `landcover` | `Copernicus.C3S_PROBA-V_LCCS_global_yearly` | `LCCS` |
| `sgli-arot-d-daily` | `gp-gcom-c-sgli-l3-arot-daytime-v3-global-daily` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-AROT.daytime.v3_global_daily` | `AROT` |
| `sgli-arot-d-halfmonth` | `gp-gcom-c-sgli-l3-arot-daytime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-AROT.daytime.v3_global_half-monthly` | `AROT` |
| `sgli-arot-d-monthly` | `gp-gcom-c-sgli-l3-arot-daytime-v3-global-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-AROT.daytime.v3_global_monthly` | `AROT` |
| `sgli-chla-d-daily` | `gp-gcom-c-sgli-l3-chla-daytime-v3-global-daily` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-CHLA.daytime.v3_global_daily` | `CHLA` |
| `sgli-chla-d-halfmonth` | `gp-gcom-c-sgli-l3-chla-daytime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-CHLA.daytime.v3_global_half-monthly` | `CHLA` |
| `sgli-chla-d-monthly` | `gp-gcom-c-sgli-l3-chla-daytime-v3-global-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-CHLA.daytime.v3_global_monthly` | `CHLA` |
| `sgli-et-8day` | `jasmes-gcom-c-sgli-et-v1-global-8-day` | `JAXA.JASMES_GCOM-C.SGLI_ET.v1_global_8-day` | `ET` |
| `sgli-et-halfmonth` | `jasmes-gcom-c-sgli-et-v1-global-half-monthly` | `JAXA.JASMES_GCOM-C.SGLI_ET.v1_global_half-monthly` | `ET` |
| `sgli-et-monthly` | `jasmes-gcom-c-sgli-et-v1-global-monthly` | `JAXA.JASMES_GCOM-C.SGLI_ET.v1_global_monthly` | `ET` |
| `sgli-etidx-8day` | `jasmes-gcom-c-sgli-etidx-v1-global-8-day` | `JAXA.JASMES_GCOM-C.SGLI_ETidx.v1_global_8-day` | `ETidx` |
| `sgli-etidx-halfmonth` | `jasmes-gcom-c-sgli-etidx-v1-global-half-monthly` | `JAXA.JASMES_GCOM-C.SGLI_ETidx.v1_global_half-monthly` | `ETidx` |
| `sgli-etidx-monthly` | `jasmes-gcom-c-sgli-etidx-v1-global-monthly` | `JAXA.JASMES_GCOM-C.SGLI_ETidx.v1_global_monthly` | `ETidx` |
| `sgli-l2-lst-d-8day-jp` | `jasmes-gcom-c-sgli-l2-lst-daytime-v3-japan-8-day` | `JAXA.JASMES_GCOM-C.SGLI_standard.L2-LST.daytime.v3_japan_8-day` | `LST_AVE` |
| `sgli-l2-lst-d-8day-norm-jp` | `jasmes-gcom-c-sgli-l2-lst-daytime-v3-japan-8-day-normal` | `JAXA.JASMES_GCOM-C.SGLI_standard.L2-LST.daytime.v3_japan_8-day-normal` | `LST_2000_2022` |
| `sgli-l2-lst-d-monthly-norm-jp` | `jasmes-gcom-c-sgli-l2-lst-daytime-v3-japan-monthly-normal` | `JAXA.JASMES_GCOM-C.SGLI_standard.L2-LST.daytime.v3_japan_monthly-normal` | `LST_2000_2022` |
| `sgli-l2-lst-n-8day-jp` | `jasmes-gcom-c-sgli-l2-lst-nighttime-v3-japan-8-day` | `JAXA.JASMES_GCOM-C.SGLI_standard.L2-LST.nighttime.v3_japan_8-day` | `LST_AVE` |
| `sgli-l2-lst-n-8day-norm-jp` | `jasmes-gcom-c-sgli-l2-lst-nighttime-v3-japan-8-day-normal` | `JAXA.JASMES_GCOM-C.SGLI_standard.L2-LST.nighttime.v3_japan_8-day-normal` | `LST_2000_2022` |
| `sgli-l2-lst-n-monthly-norm-jp` | `jasmes-gcom-c-sgli-l2-lst-nighttime-v3-japan-monthly-normal` | `JAXA.JASMES_GCOM-C.SGLI_standard.L2-LST.nighttime.v3_japan_monthly-normal` | `LST_2000_2022` |
| `sgli-lst-d-8day` | `jasmes-gcom-c-sgli-l3-lst-daytime-v3-global-8-day` | `JAXA.JASMES_GCOM-C.SGLI_standard.L3-LST.daytime.v3_global_8-day` | `LST_AVE` |
| `sgli-lst-d-8day-norm` | `jasmes-gcom-c-sgli-l3-lst-daytime-v3-global-8-day-normal` | `JAXA.JASMES_GCOM-C.SGLI_standard.L3-LST.daytime.v3_global_8-day-normal` | `LST_2000_2022` |
| `sgli-lst-d-daily` | `gp-gcom-c-sgli-l3-lst-daytime-v3-global-daily` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-LST.daytime.v3_global_daily` | `LST` |
| `sgli-lst-d-halfmonth` | `gp-gcom-c-sgli-l3-lst-daytime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-LST.daytime.v3_global_half-monthly` | `LST` |
| `sgli-lst-d-monthly` | `gp-gcom-c-sgli-l3-lst-daytime-v3-global-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-LST.daytime.v3_global_monthly` | `LST` |
| `sgli-lst-d-monthly-norm` | `jasmes-gcom-c-sgli-l3-lst-daytime-v3-global-monthly-normal` | `JAXA.JASMES_GCOM-C.SGLI_standard.L3-LST.daytime.v3_global_monthly-normal` | `LST_2000_2022` |
| `sgli-lst-n-8day` | `jasmes-gcom-c-sgli-l3-lst-nighttime-v3-global-8-day` | `JAXA.JASMES_GCOM-C.SGLI_standard.L3-LST.nighttime.v3_global_8-day` | `LST_AVE` |
| `sgli-lst-n-8day-norm` | `jasmes-gcom-c-sgli-l3-lst-nighttime-v3-global-8-day-normal` | `JAXA.JASMES_GCOM-C.SGLI_standard.L3-LST.nighttime.v3_global_8-day-normal` | `LST_2000_2022` |
| `sgli-lst-n-daily` | `gp-gcom-c-sgli-l3-lst-nighttime-v3-global-daily` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-LST.nighttime.v3_global_daily` | `LST` |
| `sgli-lst-n-halfmonth` | `gp-gcom-c-sgli-l3-lst-nighttime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-LST.nighttime.v3_global_half-monthly` | `LST` |
| `sgli-lst-n-monthly` | `gp-gcom-c-sgli-l3-lst-nighttime-v3-global-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-LST.nighttime.v3_global_monthly` | `LST` |
| `sgli-lst-n-monthly-norm` | `jasmes-gcom-c-sgli-l3-lst-nighttime-v3-global-monthly-normal` | `JAXA.JASMES_GCOM-C.SGLI_standard.L3-LST.nighttime.v3_global_monthly-normal` | `LST_2000_2022` |
| `sgli-ndvi-d-daily` | `gp-gcom-c-sgli-l3-ndvi-daytime-v3-global-daily` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-NDVI.daytime.v3_global_daily` | `NDVI` |
| `sgli-ndvi-d-halfmonth` | `gp-gcom-c-sgli-l3-ndvi-daytime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-NDVI.daytime.v3_global_half-monthly` | `NDVI` |
| `sgli-ndvi-d-monthly` | `gp-gcom-c-sgli-l3-ndvi-daytime-v3-global-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-NDVI.daytime.v3_global_monthly` | `NDVI` |
| `sgli-sst-d-daily` | `gp-gcom-c-sgli-l3-sst-daytime-v3-global-daily` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-SST.daytime.v3_global_daily` | `SST` |
| `sgli-sst-d-halfmonth` | `gp-gcom-c-sgli-l3-sst-daytime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-SST.daytime.v3_global_half-monthly` | `SST` |
| `sgli-sst-d-monthly` | `gp-gcom-c-sgli-l3-sst-daytime-v3-global-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-SST.daytime.v3_global_monthly` | `SST` |
| `sgli-sst-n-daily` | `gp-gcom-c-sgli-l3-sst-nighttime-v3-global-daily` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-SST.nighttime.v3_global_daily` | `SST` |
| `sgli-sst-n-halfmonth` | `gp-gcom-c-sgli-l3-sst-nighttime-v3-global-half-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-SST.nighttime.v3_global_half-monthly` | `SST` |
| `sgli-sst-n-monthly` | `gp-gcom-c-sgli-l3-sst-nighttime-v3-global-monthly` | `JAXA.G-Portal_GCOM-C.SGLI_standard.L3-SST.nighttime.v3_global_monthly` | `SST` |
| `spi` | `gsmap-spi-climate-gnrt6-monthly` | `JAXA.EORC_GSMaP_SPI.climate.gnrt6_monthly` | `SPI` |
| `temsm-flddph` | `utokyo-te-msm-flddph-v2-japan-hourly` | `JAXA.UTokyo_TE_MSM.FLDDPH.v2_japan_hourly` | `FLDDPH` |
| `temsm-fldfrc` | `utokyo-te-msm-fldfrc-v2-japan-hourly` | `JAXA.UTokyo_TE_MSM.FLDFRC.v2_japan_hourly` | `FLDFRC` |
| `temsm-fldout` | `utokyo-te-msm-fldout-v2-japan-hourly` | `JAXA.UTokyo_TE_MSM.FLDOUT.v2_japan_hourly` | `FLDOUT` |
| `temsm-outflw` | `utokyo-te-msm-outflw-v2-japan-hourly` | `JAXA.UTokyo_TE_MSM.OUTFLW.v2_japan_hourly` | `OUTFLW` |
| `temsm-rivdph` | `utokyo-te-msm-rivdph-v2-japan-hourly` | `JAXA.UTokyo_TE_MSM.RIVDPH.v2_japan_hourly` | `RIVDPH` |
| `temsm-rivout` | `utokyo-te-msm-rivout-v2-japan-hourly` | `JAXA.UTokyo_TE_MSM.RIVOUT.v2_japan_hourly` | `RIVOUT` |
| `temsm-rprivdph` | `utokyo-te-msm-rprivdph-v2-japan-hourly` | `JAXA.UTokyo_TE_MSM.RPRIVDPH.v2_japan_hourly` | `RPRIVDPH` |

## gportal (SFTP via the community `gportal` SDK)

Free G-Portal account required for the SFTP download step; search is anonymous. See [Authentication](authentication.md) for the credential flow. The catalog ships a starter set of one representative product per major mission; the full G-Portal universe holds ~799 dataset ids — run `earthlens datasets refresh jaxa` to diff the bundled set against the live tree.

| Key | Aliases | G-Portal id | Description |
|---|---|---|---|
| `alos2-palsar2-uf-sp` | `palsar2`, `alos2-l1`, `alos2-palsar2` | `27004001` | ALOS-2/PALSAR-2 L1.1 ultra-fine 3 m single-pol. |
| `amsr2-l1r` | `amsr2-l1`, `gcom-w-l1r` | `11001002` | GCOM-W/AMSR2 L1R resampled brightness temperatures (TB). |
| `earthcare-cpr-eco` | `earthcare`, `cpr` | `16002000` | EarthCARE/CPR L2 Echo Product (CPR_ECO). |
| `gosat-gw-amsr3-l1b` | `gosat-gw`, `amsr3` | `31001001` | GOSAT-GW/AMSR3 L1B brightness temperatures (TBB). |
| `gpm-dpr-kupr-l1b` | `gpm`, `dpr`, `kupr` | `12001000` | GPM/DPR KuPR L1B received power. |
| `sgli-l3-nwlr` | `sgli-l380`, `sgli-ocean-l380`, `gcom-c-nwlr` | `10003001` | GCOM-C/SGLI L3 Normalized Water-Leaving Radiance (NWLR) — ocean. |

## Refreshing against the live SDK universes

The CLI command `earthlens datasets refresh jaxa` walks `jaxa.earth.ImageCollectionList()` for the STAC half and `gportal.datasets()` for the G-Portal half, then diffs the live id sets against the bundled catalog so a curator can see drift at a glance:

```bash
earthlens datasets refresh jaxa
earthlens datasets refresh jaxa --write     # rewrite the bundled YAML's available_datasets:
```

The `--write` form rewrites the YAML's `available_datasets:` index without touching the hand-curated `datasets:` rows, so the friendly aliases survive a refresh.
