# JAXA — available datasets

The bundled catalog ships **918 rows**: 118 `jaxa-earth` STAC collections, 799 `gportal` mission products, and
one `ptree` Himawari entry. Every row has a short, friendly canonical key; the long auto-derived slug and a few high-traffic English names also resolve via `cat.get(...)`.

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
| `adeos-avnir-l1a-mu` | — | `22011000` | ADEOS — L1A MU |
| `adeos-avnir-l1a-pan` | — | `22011002` | ADEOS — L1A PAN |
| `adeos-avnir-l1b2-mu` | — | `22011001` | ADEOS — L1B2 MU |
| `adeos-avnir-l1b2-pan` | — | `22011003` | ADEOS — L1B2 PAN |
| `adeos-octs-gac-ocean-color` | — | `22003000` | ADEOS — L3B.GACOC/GAC Ocean Color |
| `adeos-octs-gac-ocean-color-3007` | — | `22003007` | ADEOS — L3BM.GACOCL/GAC Ocean Color |
| `adeos-octs-gac-ocean-color-chlorophyll-a` | — | `22003009` | ADEOS — L3BM.GACOCC/GAC Ocean Color-Chlorophyll-a concentration |
| `adeos-octs-gac-ocean-color-czcs-like-pigm` | — | `22003008` | ADEOS — L3BM.GACOCP/GAC Ocean Color-CZCS like pigment concentration |
| `adeos-octs-gac-ocean-color1` | — | `22002000` | ADEOS — L2.GACOC1/GAC Ocean Color1 |
| `adeos-octs-gac-ocean-color2` | — | `22002001` | ADEOS — L2.GACOC2/GAC Ocean Color2 |
| `adeos-octs-gac-sea-surface-temperatur` | — | `22002003` | ADEOS — L2.GACSST/GAC Sea Surface Temperatur |
| `adeos-octs-gac-sea-surface-temperture` | — | `22003012` | ADEOS — L3BM.GACSST/GAC Sea Surface Temperture |
| `adeos-octs-gac-seasurface-temperatur` | — | `22003002` | ADEOS — L3B.GACSST/GAC SeaSurface Temperatur |
| `adeos-octs-gac-thermal-infrared` | — | `22001001` | ADEOS — L1A.GACTI/GAC Thermal infrared |
| `adeos-octs-gac-vegetation-index` | — | `22002002` | ADEOS — L2.GACVI/GAC Vegetation Index |
| `adeos-octs-gac-vegetation-index-3001` | — | `22003001` | ADEOS — L3B.GACVI/GAC Vegetation Index |
| `adeos-octs-gac-vegetation-index-3011` | — | `22003011` | ADEOS — L3BM.GACVI/GAC Vegetation Index |
| `adeos-octs-gac-visible-and-near-infrared` | — | `22001000` | ADEOS — L1A.GACVNI/GAC Visible and near infrared |
| `adeos-octs-k490` | — | `22003005` | ADEOS — L3M.RTCOCK/RTC Ocean Color-Diffuse attenuation coefficient at 490nm(K490) |
| `adeos-octs-k490-3010` | — | `22003010` | ADEOS — L3BM.GACOCK/GAC Ocean Color-Diffuse attenuation coefficient at 490nm(K490) |
| `adeos-octs-rtc-ocean-color-chlorophyll-a` | — | `22003004` | ADEOS — L3M.RTCOCC/RTC Ocean Color-Chlorophyll-a concentration |
| `adeos-octs-rtc-ocean-color-czcs-like-pigm` | — | `22003003` | ADEOS — L3M.RTCOCP/RTC Ocean Color-CZCS like pigment concentration |
| `adeos-octs-rtc-ocean-color1` | — | `22002004` | ADEOS — L2.RTCOC1/RTC Ocean Color1 |
| `adeos-octs-rtc-ocean-color2` | — | `22002005` | ADEOS — L2.RTCOC2/RTC Ocean Color2 |
| `adeos-octs-rtc-sea-surface-temperatur` | — | `22002007` | ADEOS — L2.RTCSST/RTC Sea Surface Temperatur |
| `adeos-octs-rtc-sea-surface-temperture` | — | `22003006` | ADEOS — L3M.RTCSST/RTC Sea Surface Temperture |
| `adeos-octs-rtc-thermal-infrared` | — | `22001003` | ADEOS — L1A.RTCTI/RTC Thermal infrared |
| `adeos-octs-rtc-vegetation-index` | — | `22002006` | ADEOS — L2.RTCVI/RTC Vegetation Index |
| `adeos-octs-rtc-visible-and-near-infrared` | — | `22001002` | ADEOS — L1A.RTCVNI/RTC Visible and near infrared |
| `adeos2-amsr-amsr-l1a` | — | `23001000` | ADEOS-II — L1A/AMSR L1A |
| `adeos2-amsr-amsr-l1b` | — | `23001001` | ADEOS-II — L1B/AMSR L1B |
| `adeos2-amsr-amsr-l2-amount-of-precipitatio` | — | `23002002` | ADEOS-II — L2.AP/AMSR L2 Amount of Precipitation |
| `adeos2-amsr-amsr-l2-cloud-liquid-water` | — | `23002001` | ADEOS-II — L2.CLW/AMSR L2 Cloud Liquid Water |
| `adeos2-amsr-amsr-l2-ice-concentration` | — | `23002005` | ADEOS-II — L2.IC/AMSR L2 Ice Concentration |
| `adeos2-amsr-amsr-l2-sea-surface-temperture` | — | `23002004` | ADEOS-II — L2.SST/AMSR L2 Sea Surface Temperture |
| `adeos2-amsr-amsr-l2-sea-surface-wind-speed` | — | `23002003` | ADEOS-II — L2.SSW/AMSR L2 Sea Surface Wind Speed |
| `adeos2-amsr-amsr-l2-snow-water-equivalence` | — | `23002007` | ADEOS-II — L2.SWE/AMSR L2 Snow Water Equivalence |
| `adeos2-amsr-amsr-l2-soil-moisture` | — | `23002006` | ADEOS-II — L2.SM/AMSR L2 Soil Moisture |
| `adeos2-amsr-amsr-l2-water-vapor` | — | `23002000` | ADEOS-II — L2.WV/AMSR L2 Water Vapor |
| `adeos2-amsr-amsr-l3-amount-of-precipitatio` | — | `23003002` | ADEOS-II — L3.AP/AMSR L3 Amount of Precipitation |
| `adeos2-amsr-amsr-l3-brightness-temperature` | — | `23003008` | ADEOS-II — L3.BT/AMSR L3 Brightness Temperature |
| `adeos2-amsr-amsr-l3-cloud-liquid-water` | — | `23003001` | ADEOS-II — L3.CLW/AMSR L3 Cloud Liquid Water |
| `adeos2-amsr-amsr-l3-ice-concentration` | — | `23003005` | ADEOS-II — L3.IC/AMSR L3 Ice Concentration |
| `adeos2-amsr-amsr-l3-sea-surface-temperatur` | — | `23003004` | ADEOS-II — L3.SST/AMSR L3 Sea Surface Temperature |
| `adeos2-amsr-amsr-l3-sea-surface-wind-speed` | — | `23003003` | ADEOS-II — L3.SSW/AMSR L3 Sea Surface Wind Speed |
| `adeos2-amsr-amsr-l3-snow-water-equivalence` | — | `23003007` | ADEOS-II — L3.SWE/AMSR L3 Snow Water Equivalence |
| `adeos2-amsr-amsr-l3-soil-moisture` | — | `23003006` | ADEOS-II — L3.SM/AMSR L3 Soil Moisture |
| `adeos2-amsr-amsr-l3-water-vapor` | — | `23003000` | ADEOS-II — L3.WV/AMSR L3 Water Vapor |
| `adeos2-gli-1km-absorption-of-colored-dissolve` | — | `23013036` | ADEOS-II — L3STAMAP.CDOM0/Absorption of colored dissolved organic matter |
| `adeos2-gli-1km-add-data-averaged` | — | `23013039` | ADEOS-II — L3STAMAP.STALL/Sea Surface Temperature(add data averaged) |
| `adeos2-gli-1km-aerosol` | — | `23013013` | ADEOS-II — L3B.LA000/Aerosol |
| `adeos2-gli-1km-aerosol-3033` | — | `23013033` | ADEOS-II — L3STAMAP.LA000/Aerosol |
| `adeos2-gli-1km-aerosol-angstrom-exponent` | — | `23012001` | ADEOS-II — L2.ARAEO/Aerosol Angstrom Exponent |
| `adeos2-gli-1km-aerosol-angstrom-exponent-3000` | — | `23013000` | ADEOS-II — L3B.ARAEO/Aerosol Angstrom Exponent |
| `adeos2-gli-1km-aerosol-angstrom-exponent-3020` | — | `23013020` | ADEOS-II — L3STAMAP.ARAEO/Aerosol Angstrom Exponent |
| `adeos2-gli-1km-aerosol-optical-thickness` | — | `23012002` | ADEOS-II — L2.AROP0/Aerosol Optical Thickness |
| `adeos2-gli-1km-aerosol-optical-thickness-3001` | — | `23013001` | ADEOS-II — L3B.AROP0/Aerosol Optical Thickness |
| `adeos2-gli-1km-aerosol-optical-thickness-3021` | — | `23013021` | ADEOS-II — L3STAMAP.AROP0/Aerosol Optical Thickness |
| `adeos2-gli-1km-atmospheric-corrected-data-for` | — | `23012018` | ADEOS-II — L2.ACLC0/Atmospheric Corrected Data For Land And Cryosphere |
| `adeos2-gli-1km-atmospheric-correction` | — | `23012013` | ADEOS-II — L2.NLLR0/Atmospheric Correction |
| `adeos2-gli-1km-attenuation-coefficent-at-490m` | — | `23013037` | ADEOS-II — L3STAMAP.K4900/Attenuation coefficent at 490mm |
| `adeos2-gli-1km-chlorophyll-a` | — | `23013034` | ADEOS-II — L3STAMAP.CHLA0/Chlorophyll-a |
| `adeos2-gli-1km-cloud-effective-particle-radiu` | — | `23012004` | ADEOS-II — L2.CERWR/Cloud Effective Particle Radius of water cloud by reflection method |
| `adeos2-gli-1km-cloud-effective-particle-radiu-2005` | — | `23012005` | ADEOS-II — L2.CERIE/Cloud Effective Particle Radius of ice cloud by emission method |
| `adeos2-gli-1km-cloud-effective-particle-radiu-3003` | — | `23013003` | ADEOS-II — L3B.CERWR/Cloud Effective Particle Radius of water cloud by reflection method |
| `adeos2-gli-1km-cloud-effective-particle-radiu-3004` | — | `23013004` | ADEOS-II — L3B.CERIE/Cloud Effective Particle Radius of ice cloud by emission method |
| `adeos2-gli-1km-cloud-effective-particle-radiu-3023` | — | `23013023` | ADEOS-II — L3STAMAP.CERWR/Cloud Effective Particle Radius of water cloud by reflection method |
| `adeos2-gli-1km-cloud-effective-particle-radiu-3024` | — | `23013024` | ADEOS-II — L3STAMAP.CERIE/Cloud Effective Particle Radius of ice cloud by emission method |
| `adeos2-gli-1km-cloud-flag` | — | `23012000` | ADEOS-II — L2.CLFLG/Cloud Flag |
| `adeos2-gli-1km-cloud-fraction` | — | `23012003` | ADEOS-II — L2.CLFR0/Cloud fraction |
| `adeos2-gli-1km-cloud-fraction-3002` | — | `23013002` | ADEOS-II — L3B.CLFR0/Cloud fraction |
| `adeos2-gli-1km-cloud-fraction-3022` | — | `23013022` | ADEOS-II — L3STAMAP.CLFR0/Cloud fraction |
| `adeos2-gli-1km-cloud-liquid-water-path-of-wat` | — | `23012012` | ADEOS-II — L2.CWPWR/Cloud Liquid Water Path of water cloud by reflaction method |
| `adeos2-gli-1km-cloud-liquid-water-path-of-wat-3011` | — | `23013011` | ADEOS-II — L3B.CWPWR/Cloud Liquid Water Path of water cloud by reflaction method |
| `adeos2-gli-1km-cloud-liquid-water-path-of-wat-3031` | — | `23013031` | ADEOS-II — L3STAMAP.CWPWR/Cloud Liquid Water Path of water cloud by reflaction method |
| `adeos2-gli-1km-cloud-optical-thickness-of-ice` | — | `23012007` | ADEOS-II — L2.COPIR/Cloud Optical Thickness of ice cloud by reflection method |
| `adeos2-gli-1km-cloud-optical-thickness-of-ice-2008` | — | `23012008` | ADEOS-II — L2.COPIE/Cloud Optical Thickness of ice cloud by emission method |
| `adeos2-gli-1km-cloud-optical-thickness-of-ice-3006` | — | `23013006` | ADEOS-II — L3B.COPIR/Cloud Optical Thickness of ice  cloud by reflection method |
| `adeos2-gli-1km-cloud-optical-thickness-of-ice-3007` | — | `23013007` | ADEOS-II — L3B.COPIE/Cloud Optical Thickness of ice cloud by emission method |
| `adeos2-gli-1km-cloud-optical-thickness-of-ice-3026` | — | `23013026` | ADEOS-II — L3STAMAP.COPIR/Cloud Optical Thickness of ice  cloud by reflection method |
| `adeos2-gli-1km-cloud-optical-thickness-of-ice-3027` | — | `23013027` | ADEOS-II — L3STAMAP.COPIE/Cloud Optical Thickness of ice cloud by emission method |
| `adeos2-gli-1km-cloud-optical-thickness-of-wat` | — | `23012006` | ADEOS-II — L2.COPWR/Cloud Optical Thickness of water cloud by reflection method |
| `adeos2-gli-1km-cloud-optical-thickness-of-wat-3005` | — | `23013005` | ADEOS-II — L3B.COPWR/Cloud Optical Thickness of water cloud by reflection method |
| `adeos2-gli-1km-cloud-optical-thickness-of-wat-3025` | — | `23013025` | ADEOS-II — L3STAMAP.COPWR/Cloud Optical Thickness of water cloud by reflection method |
| `adeos2-gli-1km-cloud-top-height-of-water-clou` | — | `23012011` | ADEOS-II — L2.CHTWR/Cloud Top Height of water cloude by emmission method |
| `adeos2-gli-1km-cloud-top-height-of-water-clou-3010` | — | `23013010` | ADEOS-II — L3B.CHTWR/Cloud Top Height of water cloude by emmission method |
| `adeos2-gli-1km-cloud-top-height-of-water-clou-3030` | — | `23013030` | ADEOS-II — L3STAMAP.CHTWR/Cloud Top Height of water cloude by emmission method |
| `adeos2-gli-1km-cloud-top-temperature-of-ice-c` | — | `23012010` | ADEOS-II — L2.CTTIE/Cloud Top Temperature of ice cloud by emission method |
| `adeos2-gli-1km-cloud-top-temperature-of-ice-c-3009` | — | `23013009` | ADEOS-II — L3B.CTTIE/Cloud Top Temperature of ice cloud by emission method |
| `adeos2-gli-1km-cloud-top-temperature-of-ice-c-3029` | — | `23013029` | ADEOS-II — L3STAMAP.CTTIE/Cloud Top Temperature of ice cloud by emission method |
| `adeos2-gli-1km-cloud-top-temperature-of-water` | — | `23012009` | ADEOS-II — L2.CTTWR/Cloud Top Temperature of water cloud by reflection method |
| `adeos2-gli-1km-cloud-top-temperature-of-water-3008` | — | `23013008` | ADEOS-II — L3B.CTTWR/Cloud Top Temperature of water cloud by reflection method |
| `adeos2-gli-1km-cloud-top-temperature-of-water-3028` | — | `23013028` | ADEOS-II — L3STAMAP.CTTWR/Cloud Top Temperature of water cloud by reflection method |
| `adeos2-gli-1km-day-night-separately-averaged` | — | `23013038` | ADEOS-II — L3STAMAP.STDN0/Sea Surface Temperature(day/night separately averaged) |
| `adeos2-gli-1km-in-water-particles` | — | `23012014` | ADEOS-II — L2.CSLR0/In-water Particles |
| `adeos2-gli-1km-in-water-particles-3014` | — | `23013014` | ADEOS-II — L3B.CS000/In-water Particles |
| `adeos2-gli-1km-land-and-cryosphere` | — | `23012021` | ADEOS-II — L2A.L2ALC/Land and Cryosphere |
| `adeos2-gli-1km-middle-and-thermal-infrared` | — | `23011002` | ADEOS-II — L1A.MTIR/Middle and thermal infrared |
| `adeos2-gli-1km-middle-and-thermal-infrared-1006` | — | `23011006` | ADEOS-II — L1B.MTIR/Middle and thermal infrared |
| `adeos2-gli-1km-normalized-water-leaving-radia` | — | `23013012` | ADEOS-II — L3B.NW000/Normalized water-leaving radiance |
| `adeos2-gli-1km-normalized-water-leaving-radia-3032` | — | `23013032` | ADEOS-II — L3STAMAP.NW000/Normalized water-leaving radiance |
| `adeos2-gli-1km-ocean-and-atmosphere` | — | `23012020` | ADEOS-II — L2A.L2AOA/Ocean and Atmosphere |
| `adeos2-gli-1km-precise-geometric-corrected-pa` | — | `23012017` | ADEOS-II — L2.PGCP0/Precise Geometric Corrected Parameter |
| `adeos2-gli-1km-sea-surface-temperature` | — | `23012015` | ADEOS-II — L2.STLR0/Sea Surface Temperature |
| `adeos2-gli-1km-sea-surface-temperature-3015` | — | `23013015` | ADEOS-II — L3B.ST000/Sea Surface Temperature |
| `adeos2-gli-1km-short-wavelength-infrared` | — | `23011001` | ADEOS-II — L1A.SWIR/Short-wavelength infrared |
| `adeos2-gli-1km-short-wavelength-infrared-1005` | — | `23011005` | ADEOS-II — L1B.SWIR/Short-wavelength infrared |
| `adeos2-gli-1km-slpt` | — | `23011007` | ADEOS-II — L1B.SLPT/ SLPT |
| `adeos2-gli-1km-snow-grain-size-retrieved-with` | — | `23013016` | ADEOS-II — L3B.SNWG0/Snow grain size retrieved with 865nm band |
| `adeos2-gli-1km-snow-grain-size-retrieved-with-3018` | — | `23013018` | ADEOS-II — L3B.SNWGS/Snow grain size retrieved with 1640nm band |
| `adeos2-gli-1km-snow-grain-size-retrieved-with-3041` | — | `23013041` | ADEOS-II — L3STAMAP.SNWG0/Snow grain size retrieved with 865nm band |
| `adeos2-gli-1km-snow-grain-size-retrieved-with-3043` | — | `23013043` | ADEOS-II — L3STAMAP.SNWGS/Snow grain size retrieved with 1640nm band |
| `adeos2-gli-1km-snow-impurities` | — | `23013017` | ADEOS-II — L3B.SNWI0/Snow impurities |
| `adeos2-gli-1km-snow-impurities-3042` | — | `23013042` | ADEOS-II — L3STAMAP.SNWI0/Snow impurities |
| `adeos2-gli-1km-snow-property` | — | `23012019` | ADEOS-II — L2.SNGI0/Snow Property |
| `adeos2-gli-1km-snow-surface-temperature` | — | `23013019` | ADEOS-II — L3B.SNWTS/Snow surface temperature |
| `adeos2-gli-1km-snow-surface-temperature-3044` | — | `23013044` | ADEOS-II — L3STAMAP.SNWTS/Snow surface temperature |
| `adeos2-gli-1km-suspended-solid-weight` | — | `23013035` | ADEOS-II — L3STAMAP.SS000/Suspended solid weight |
| `adeos2-gli-1km-vegetation-index` | — | `23012016` | ADEOS-II — L2.VGI00/Vegetation Index |
| `adeos2-gli-1km-vegetation-index-3040` | — | `23013040` | ADEOS-II — L3STAMAP.VGI00/Vegetation index |
| `adeos2-gli-1km-visible-and-near-infrared` | — | `23011000` | ADEOS-II — L1A.VNIR/Visible and near infrared |
| `adeos2-gli-1km-visible-and-near-infrared-1004` | — | `23011004` | ADEOS-II — L1B.VNIR/Visible and near infrared |
| `adeos2-gli-250m-l1a` | — | `23021000` | ADEOS-II — L1A |
| `adeos2-gli-250m-l1b` | — | `23021001` | ADEOS-II — L1B |
| `alos-avnir-2-observation` | — | `26014000` | ALOS — Observation |
| `alos-palsar-burst-mode-1` | — | `26024002` | ALOS — Wide Area Observation Mode (Burst mode 1) |
| `alos-palsar-burst-mode-2` | — | `26024003` | ALOS — Wide Area Observation Mode (Burst mode 2) |
| `alos-palsar-direct-downlink-mode` | — | `26024004` | ALOS — Direct Downlink Mode |
| `alos-palsar-fine-resolution-mode-dual-pola` | — | `26024001` | ALOS — Fine Resolution Mode, dual polarization |
| `alos-palsar-fine-resolution-mode-single-po` | — | `26024000` | ALOS — Fine Resolution Mode, Single polarization |
| `alos-palsar-polarimetry-mode` | — | `26024005` | ALOS — Polarimetry Mode |
| `alos-prism-35km` | — | `26004001` | ALOS — Nadir (70km) + Backward (35km) |
| `alos-prism-35km-4003` | — | `26004003` | ALOS — Nadir (35km) + Forward (35km) |
| `alos-prism-35km-4004` | — | `26004004` | ALOS — Nadir (35km) + Backward (35km) |
| `alos-prism-35km-4005` | — | `26004005` | ALOS — Forward (35km) + Backward (35km) |
| `alos-prism-35km-4006` | — | `26004006` | ALOS — Nadir (35km) |
| `alos-prism-35km-4007` | — | `26004007` | ALOS — Forward (35km) |
| `alos-prism-35km-4008` | — | `26004008` | ALOS — Backward (35km) |
| `alos-prism-70km` | — | `26004002` | ALOS — Nadir (70km) |
| `alos-prism-triplet-observation-mode` | — | `26004000` | ALOS — Triplet observation mode |
| `alos2-palsar2-uf-sp` | `palsar2`, `alos2-l1`, `alos2-palsar2` | `27004001` | ALOS-2 — NA/Ultra-fine[3m] SP |
| `amsr2-l1-tb` | — | `11001001` | GCOM-W/AMSR2 — L1B-Brightness temperature（TB） |
| `amsr2-l1r` | `amsr2-l1`, `gcom-w-l1r` | `11001002` | GCOM-W/AMSR2 — L1R-Brightness temperature（TB） |
| `amsr2-l2-clw` | — | `11002010` | GCOM-W/AMSR2 — L2-Cloud Liquid Water（CLW） |
| `amsr2-l2-clw-2001` | — | `11002001` | GCOM-W/AMSR2 — L2-Cloud Liquid Water（CLW） |
| `amsr2-l2-prc` | — | `11002003` | GCOM-W/AMSR2 — L2-Precipitation（PRC） |
| `amsr2-l2-prc-2002` | — | `11002002` | GCOM-W/AMSR2 — L2-Precipitation（PRC） |
| `amsr2-l2-sic` | — | `11002013` | GCOM-W/AMSR2 — L2-Sea Ice Concentration（SIC） |
| `amsr2-l2-sic-2006` | — | `11002006` | GCOM-W/AMSR2 — L2-Sea Ice Concentration（SIC） |
| `amsr2-l2-smc` | — | `11002015` | GCOM-W/AMSR2 — L2-Soil Moisture Content（SMC） |
| `amsr2-l2-smc-2008` | — | `11002008` | GCOM-W/AMSR2 — L2-Soil Moisture Content（SMC） |
| `amsr2-l2-snd` | — | `11002014` | GCOM-W/AMSR2 — L2-Snow Depth（SND） |
| `amsr2-l2-snd-2007` | — | `11002007` | GCOM-W/AMSR2 — L2-Snow Depth（SND） |
| `amsr2-l2-sst` | — | `11002011` | GCOM-W/AMSR2 — L2-Sea Surface Temperture（SST） |
| `amsr2-l2-sst-2004` | — | `11002004` | GCOM-W/AMSR2 — L2-Sea Surface Temperture（SST） |
| `amsr2-l2-ssw` | — | `11002012` | GCOM-W/AMSR2 — L2-Sea Surface Wind Speed（SSW） |
| `amsr2-l2-ssw-2005` | — | `11002005` | GCOM-W/AMSR2 — L2-Sea Surface Wind Speed（SSW） |
| `amsr2-l2-tpw` | — | `11002009` | GCOM-W/AMSR2 — L2-Integrated Water Vapor（TPW） |
| `amsr2-l2-tpw-2000` | — | `11002000` | GCOM-W/AMSR2 — L2-Integrated Water Vapor（TPW） |
| `amsr2-l3-0-1deg-l3-brightness-temperatu` | — | `11003017` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_6GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3016` | — | `11003016` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_6GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3018` | — | `11003018` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_7GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3019` | — | `11003019` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_7GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3020` | — | `11003020` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_10GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3021` | — | `11003021` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_10GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3022` | — | `11003022` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_18GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3023` | — | `11003023` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_18GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3024` | — | `11003024` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_23GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3025` | — | `11003025` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_23GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3026` | — | `11003026` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_36GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3027` | — | `11003027` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_36GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3028` | — | `11003028` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_89GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-brightness-temperatu-3029` | — | `11003029` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Brightness temperature（TB）_89GHz_0.1deg |
| `amsr2-l3-0-1deg-l3-cloud-liquid-water-c` | — | `11003003` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Cloud Liquid Water（CLW）_0.1deg |
| `amsr2-l3-0-1deg-l3-cloud-liquid-water-c-3002` | — | `11003002` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Cloud Liquid Water（CLW）_0.1deg |
| `amsr2-l3-0-1deg-l3-ice-concentration-si` | — | `11003011` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Ice Concentration（SIC）_0.1deg |
| `amsr2-l3-0-1deg-l3-ice-concentration-si-3010` | — | `11003010` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Ice Concentration（SIC）_0.1deg |
| `amsr2-l3-0-1deg-l3-integrated-water-vap` | — | `11003001` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Integrated Water Vapor（TPW）_0.1deg |
| `amsr2-l3-0-1deg-l3-integrated-water-vap-3000` | — | `11003000` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Integrated Water Vapor（TPW）_0.1deg |
| `amsr2-l3-0-1deg-l3-precipitation-prc-0` | — | `11003005` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Precipitation（PRC）_0.1deg |
| `amsr2-l3-0-1deg-l3-precipitation-prc-0-3004` | — | `11003004` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Precipitation（PRC）_0.1deg |
| `amsr2-l3-0-1deg-l3-sea-surface-tempertu` | — | `11003007` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Sea Surface Temperture（SST）_0.1deg |
| `amsr2-l3-0-1deg-l3-sea-surface-tempertu-3006` | — | `11003006` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Sea Surface Temperture（SST）_0.1deg |
| `amsr2-l3-0-1deg-l3-sea-surface-wind-spe` | — | `11003009` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Sea Surface Wind Speed（SSW）_0.1deg |
| `amsr2-l3-0-1deg-l3-sea-surface-wind-spe-3008` | — | `11003008` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Sea Surface Wind Speed（SSW）_0.1deg |
| `amsr2-l3-0-1deg-l3-snow-depth-snd-0-1de` | — | `11003013` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Snow Depth（SND）_0.1deg |
| `amsr2-l3-0-1deg-l3-snow-depth-snd-0-1de-3012` | — | `11003012` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Snow Depth（SND）_0.1deg |
| `amsr2-l3-0-1deg-l3-soil-moisture-conten` | — | `11003015` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Soil Moisture Content（SMC）_0.1deg |
| `amsr2-l3-0-1deg-l3-soil-moisture-conten-3014` | — | `11003014` | GCOM-W/AMSR2 — 10km/0.1deg/L3-Soil Moisture Content（SMC）_0.1deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat` | — | `11003047` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_6GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3046` | — | `11003046` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_6GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3048` | — | `11003048` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_7GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3049` | — | `11003049` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_7GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3050` | — | `11003050` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_10GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3051` | — | `11003051` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_10GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3052` | — | `11003052` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_18GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3053` | — | `11003053` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_18GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3054` | — | `11003054` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_23GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3055` | — | `11003055` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_23GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3056` | — | `11003056` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_36GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3057` | — | `11003057` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_36GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3058` | — | `11003058` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_89GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-brightness-temperat-3059` | — | `11003059` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Brightness temperature（TB）_89GHz_0.25deg |
| `amsr2-l3-0-25deg-l3-cloud-liquid-water` | — | `11003033` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Cloud Liquid Water（CLW）_0.25deg |
| `amsr2-l3-0-25deg-l3-cloud-liquid-water-3032` | — | `11003032` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Cloud Liquid Water（CLW）_0.25deg |
| `amsr2-l3-0-25deg-l3-integrated-water-va` | — | `11003031` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Integrated Water Vapor（TPW）_0.25deg |
| `amsr2-l3-0-25deg-l3-integrated-water-va-3030` | — | `11003030` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Integrated Water Vapor（TPW）_0.25deg |
| `amsr2-l3-0-25deg-l3-precipitation-prc-0` | — | `11003035` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Precipitation（PRC）_0.25deg |
| `amsr2-l3-0-25deg-l3-precipitation-prc-0-3034` | — | `11003034` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Precipitation（PRC）_0.25deg |
| `amsr2-l3-0-25deg-l3-sea-ice-concentrati` | — | `11003041` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Sea Ice Concentration（SIC）_0.25deg |
| `amsr2-l3-0-25deg-l3-sea-ice-concentrati-3040` | — | `11003040` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Sea Ice Concentration（SIC）_0.25deg |
| `amsr2-l3-0-25deg-l3-sea-surface-tempert` | — | `11003037` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Sea Surface Temperture（SST）_0.25deg |
| `amsr2-l3-0-25deg-l3-sea-surface-tempert-3036` | — | `11003036` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Sea Surface Temperture（SST）_0.25deg |
| `amsr2-l3-0-25deg-l3-sea-surface-wind-sp` | — | `11003039` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Sea Surface Wind Speed（SSW）_0.25deg |
| `amsr2-l3-0-25deg-l3-sea-surface-wind-sp-3038` | — | `11003038` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Sea Surface Wind Speed（SSW）_0.25deg |
| `amsr2-l3-0-25deg-l3-snow-depth-snd-0-25` | — | `11003043` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Snow Depth（SND）_0.25deg |
| `amsr2-l3-0-25deg-l3-snow-depth-snd-0-25-3042` | — | `11003042` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Snow Depth（SND）_0.25deg |
| `amsr2-l3-0-25deg-l3-soil-moisture-conte` | — | `11003045` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Soil Moisture Content（SMC）_0.25deg |
| `amsr2-l3-0-25deg-l3-soil-moisture-conte-3044` | — | `11003044` | GCOM-W/AMSR2 — 25km/0.25deg/L3-Soil Moisture Content（SMC）_0.25deg |
| `amsr3-l1-tbr` | — | `31001002` | GOSAT-GW/AMSR3 — L1R - Resampled Brightness Temperature (TBR) |
| `amsr3-l2-asw` | — | `31002005` | GOSAT-GW/AMSR3 — L2 - All-weather Sea Surface Wind Speed（ASW） |
| `amsr3-l2-clw` | — | `31002001` | GOSAT-GW/AMSR3 — L2 - Cloud Liquid Water Content（CLW） |
| `amsr3-l2-hsi` | — | `31002007` | GOSAT-GW/AMSR3 — L2 - High-resolution Sea Ice Concentration（HSI） |
| `amsr3-l2-prc` | — | `31002002` | GOSAT-GW/AMSR3 — L2 - Precipitation （PRC） |
| `amsr3-l2-sic` | — | `31002006` | GOSAT-GW/AMSR3 — L2 - Sea Ice Concentration （SIC） |
| `amsr3-l2-smc` | — | `31002008` | GOSAT-GW/AMSR3 — L2 - Soil Moisture Content （SMC） |
| `amsr3-l2-snd` | — | `31002009` | GOSAT-GW/AMSR3 — L2 - Snow Depth （SND） |
| `amsr3-l2-sst` | — | `31002003` | GOSAT-GW/AMSR3 — L2 - Sea Surface Temperature （SST） |
| `amsr3-l2-ssw` | — | `31002004` | GOSAT-GW/AMSR3 — L2 - Sea Surface Wind Speed （SSW） |
| `amsr3-l2-tpw` | — | `31002000` | GOSAT-GW/AMSR3 — L2 - Total Precipitable Water （TPW） |
| `amsr3-l3-asw` | — | `31003016` | GOSAT-GW/AMSR3 — L3 - All-weather Sea Surface Wind Speed（ASW） |
| `amsr3-l3-clw` | — | `31003012` | GOSAT-GW/AMSR3 — L3 - Cloud Liquid Water Content （CLW） |
| `amsr3-l3-hsi` | — | `31003018` | GOSAT-GW/AMSR3 — L3 - High-resolution Sea Ice Concentration（HSI） |
| `amsr3-l3-prc` | — | `31003013` | GOSAT-GW/AMSR3 — L3 - Precipitation （PRC） |
| `amsr3-l3-sic` | — | `31003017` | GOSAT-GW/AMSR3 — L3 - Sea Ice Concentration （SIC） |
| `amsr3-l3-smc` | — | `31003019` | GOSAT-GW/AMSR3 — L3 - Soil Moisture Content （SMC） |
| `amsr3-l3-snd` | — | `31003020` | GOSAT-GW/AMSR3 — L3 - Snow Depth （SND） |
| `amsr3-l3-sst` | — | `31003014` | GOSAT-GW/AMSR3 — L3 - Sea Surface Temperature （SST） |
| `amsr3-l3-ssw` | — | `31003015` | GOSAT-GW/AMSR3 — L3 - Sea Surface Wind Speed （SSW） |
| `amsr3-l3-th1` | — | `31003007` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 89.0GHz (TH1) |
| `amsr3-l3-th2` | — | `31003008` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 165.5GHz (TH2) |
| `amsr3-l3-th3` | — | `31003009` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 183.31+/-3GHz (TH3) |
| `amsr3-l3-th4` | — | `31003010` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 183.31+/-7GHz (TH4) |
| `amsr3-l3-tl1` | — | `31003000` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 6.925GHz (TL1) |
| `amsr3-l3-tl2` | — | `31003001` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 7.3GHz (TL2) |
| `amsr3-l3-tl3` | — | `31003002` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 10.25GHz (TL3) |
| `amsr3-l3-tl4` | — | `31003003` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 10.65GHz (TL4) |
| `amsr3-l3-tl5` | — | `31003004` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 18.7GHz (TL5) |
| `amsr3-l3-tl6` | — | `31003005` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 23.8GH (TL6) |
| `amsr3-l3-tl7` | — | `31003006` | GOSAT-GW/AMSR3 — L3 - Brightness temperature 36.42GHz (TL7) |
| `amsr3-l3-tpw` | — | `31003011` | GOSAT-GW/AMSR3 — L3 - Total Precipitable Water （TPW） |
| `aqua-amsr-e-amsr2format-l1-tb` | — | `25001000` | AQUA — LEVEL1/L1B Brightness Temperatures (TB) |
| `aqua-amsr-e-amsr2format-l1-tb-1001` | — | `25001001` | AQUA — LEVEL1/L1R Brightness Temperatures (TB) |
| `aqua-amsr-e-amsr2format-l2-clw` | — | `25002001` | AQUA — LEVEL2/L2 Cloud Liquid Water (CLW) |
| `aqua-amsr-e-amsr2format-l2-prc` | — | `25002003` | AQUA — LEVEL2/L2 Precipitation (PRC) |
| `aqua-amsr-e-amsr2format-l2-prc-2002` | — | `25002002` | AQUA — LEVEL2/L2 Precipitation (PRC) |
| `aqua-amsr-e-amsr2format-l2-sic` | — | `25002006` | AQUA — LEVEL2/L2 Sea Ice Concentration (SIC) |
| `aqua-amsr-e-amsr2format-l2-smc` | — | `25002007` | AQUA — LEVEL2/L2 Soil Moisture Content (SMC) |
| `aqua-amsr-e-amsr2format-l2-snd` | — | `25002008` | AQUA — LEVEL2/L2 Snow Depth (SND) |
| `aqua-amsr-e-amsr2format-l2-sst` | — | `25002004` | AQUA — LEVEL2/L2 Sea Surface Temperature (SST) |
| `aqua-amsr-e-amsr2format-l2-ssw` | — | `25002005` | AQUA — LEVEL2/L2 Sea Surface Wind speed (SSW) |
| `aqua-amsr-e-amsr2format-l2-tpw` | — | `25002000` | AQUA — LEVEL2/L2 Total Precipitable Water (TPW) |
| `aqua-amsr-e-amsr2format-l3-0-1` | — | `25003001` | AQUA — LEVEL3/10km/0.1deg/L3 Total Precipitable Water (TPW)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3000` | — | `25003000` | AQUA — LEVEL3/10km/0.1deg/L3 Total Precipitable Water (TPW)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3002` | — | `25003002` | AQUA — LEVEL3/10km/0.1deg/L3 Cloud Liquid Water (CLW)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3003` | — | `25003003` | AQUA — LEVEL3/10km/0.1deg/L3 Cloud Liquid Water (CLW)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3004` | — | `25003004` | AQUA — LEVEL3/10km/0.1deg/L3 Precipitation (PRC)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3005` | — | `25003005` | AQUA — LEVEL3/10km/0.1deg/L3 Precipitation (PRC)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3006` | — | `25003006` | AQUA — LEVEL3/10km/0.1deg/L3 Sea Surface Temperature (SST)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3007` | — | `25003007` | AQUA — LEVEL3/10km/0.1deg/L3 Sea Surface Temperature (SST)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3008` | — | `25003008` | AQUA — LEVEL3/10km/0.1deg/L3 Sea Surface Wind speed (SSW)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3009` | — | `25003009` | AQUA — LEVEL3/10km/0.1deg/L3 Sea Surface Wind speed (SSW)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3010` | — | `25003010` | AQUA — LEVEL3/10km/0.1deg/L3 Sea Ice Concentration (SIC)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3011` | — | `25003011` | AQUA — LEVEL3/10km/0.1deg/L3 Sea Ice Concentration (SIC)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3012` | — | `25003012` | AQUA — LEVEL3/10km/0.1deg/L3 Soil Moisture Content (SMC)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3013` | — | `25003013` | AQUA — LEVEL3/10km/0.1deg/L3 Soil Moisture Content (SMC)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3014` | — | `25003014` | AQUA — LEVEL3/10km/0.1deg/L3 Snow Depth (SND)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3015` | — | `25003015` | AQUA — LEVEL3/10km/0.1deg/L3 Snow Depth (SND)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3016` | — | `25003016` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 6GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3017` | — | `25003017` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 6GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3018` | — | `25003018` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 10GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3019` | — | `25003019` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 10GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3020` | — | `25003020` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 18GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3021` | — | `25003021` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 18GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3022` | — | `25003022` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 23GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3023` | — | `25003023` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 23GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3024` | — | `25003024` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 36GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3025` | — | `25003025` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 36GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3026` | — | `25003026` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 89GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-1-3027` | — | `25003027` | AQUA — LEVEL3/10km/0.1deg/L3 Brightness Temperatures 89GHz (TB)(0.1°) |
| `aqua-amsr-e-amsr2format-l3-0-25` | — | `25003029` | AQUA — LEVEL3/25km/0.25deg/L3 Total Precipitable Water (TPW)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3028` | — | `25003028` | AQUA — LEVEL3/25km/0.25deg/L3 Total Precipitable Water (TPW)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3030` | — | `25003030` | AQUA — LEVEL3/25km/0.25deg/L3 Cloud Liquid Water (CLW)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3031` | — | `25003031` | AQUA — LEVEL3/25km/0.25deg/L3 Cloud Liquid Water (CLW)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3032` | — | `25003032` | AQUA — LEVEL3/25km/0.25deg/L3 Precipitation (PRC)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3033` | — | `25003033` | AQUA — LEVEL3/25km/0.25deg/L3 Precipitation (PRC)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3034` | — | `25003034` | AQUA — LEVEL3/25km/0.25deg/L3 Sea Surface Temperature (SST)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3035` | — | `25003035` | AQUA — LEVEL3/25km/0.25deg/L3 Sea Surface Temperature (SST)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3036` | — | `25003036` | AQUA — LEVEL3/25km/0.25deg/L3 Sea Surface Wind speed (SSW)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3037` | — | `25003037` | AQUA — LEVEL3/25km/0.25deg/L3 Sea Surface Wind speed (SSW)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3038` | — | `25003038` | AQUA — LEVEL3/25km/0.25deg/L3 Sea Ice Concentration (SIC)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3039` | — | `25003039` | AQUA — LEVEL3/25km/0.25deg/L3 Sea Ice Concentration (SIC)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3040` | — | `25003040` | AQUA — LEVEL3/25km/0.25deg/L3 Soil Moisture Content (SMC)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3041` | — | `25003041` | AQUA — LEVEL3/25km/0.25deg/L3 Soil Moisture Content (SMC)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3042` | — | `25003042` | AQUA — LEVEL3/25km/0.25deg/L3 Snow Depth (SND)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3043` | — | `25003043` | AQUA — LEVEL3/25km/0.25deg/L3 Snow Depth (SND)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3044` | — | `25003044` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 6GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3045` | — | `25003045` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 6GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3046` | — | `25003046` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 10GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3047` | — | `25003047` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 10GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3048` | — | `25003048` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 18GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3049` | — | `25003049` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 18GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3050` | — | `25003050` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 23GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3051` | — | `25003051` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 23GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3052` | — | `25003052` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 36GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3053` | — | `25003053` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 36GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3054` | — | `25003054` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 89GHz (TB)(0.25°) |
| `aqua-amsr-e-amsr2format-l3-0-25-3055` | — | `25003055` | AQUA — LEVEL3/25km/0.25deg/L3 Brightness Temperatures 89GHz (TB)(0.25°) |
| `aqua-amsr-e-l1-l1b-brightness-temperature` | — | `24001001` | AQUA — LEVEL1/L1B Brightness Temperature |
| `aqua-amsr-e-l2-ap` | — | `24002002` | AQUA — LEVEL2/L2 Amount of Precipitation (AP) |
| `aqua-amsr-e-l2-clw` | — | `24002001` | AQUA — LEVEL2/L2 Cloud Liquid Water (CLW) |
| `aqua-amsr-e-l2-ic` | — | `24002005` | AQUA — LEVEL2/L2 Ice Concentration (IC) |
| `aqua-amsr-e-l2-sm` | — | `24002006` | AQUA — LEVEL2/L2 Soil Moisture (SM) |
| `aqua-amsr-e-l2-sst` | — | `24002004` | AQUA — LEVEL2/L2 Sea Surface Temperature (SST) |
| `aqua-amsr-e-l2-ssw` | — | `24002003` | AQUA — LEVEL2/L2 Sea SurfaceWind Speed (SSW) |
| `aqua-amsr-e-l2-swe` | — | `24002007` | AQUA — LEVEL2/L2 Snow Water Equivalence (SWE) |
| `aqua-amsr-e-l2-wv` | — | `24002000` | AQUA — LEVEL2/L2 Water Vapor (WV) |
| `aqua-amsr-e-l3-ap` | — | `24003002` | AQUA — LEVEL3/L3 Amount of Precipitation (AP) |
| `aqua-amsr-e-l3-clw` | — | `24003001` | AQUA — LEVEL3/L3 Cloud Liquid Water (CLW) |
| `aqua-amsr-e-l3-ic` | — | `24003005` | AQUA — LEVEL3/L3 Ice Concentration (IC) |
| `aqua-amsr-e-l3-sm` | — | `24003006` | AQUA — LEVEL3/L3 Soid Moisture (SM) |
| `aqua-amsr-e-l3-sst` | — | `24003004` | AQUA — LEVEL3/L3 Sea Surface Temperature (SST) |
| `aqua-amsr-e-l3-ssw` | — | `24003003` | AQUA — LEVEL3/L3 Sea SurfaceWind Speed (SSW) |
| `aqua-amsr-e-l3-swe` | — | `24003007` | AQUA — LEVEL3/L3 Snow Water Equivalence (SWE) |
| `aqua-amsr-e-l3-tb10ghz-h` | — | `24003011` | AQUA — LEVEL3/L3 Brightness Temperature 10GHz-H (TB10GHz_H) |
| `aqua-amsr-e-l3-tb10ghz-v` | — | `24003010` | AQUA — LEVEL3/L3 Brightness Temperature 10GHz-V (TB10GHz_V) |
| `aqua-amsr-e-l3-tb18ghz-h` | — | `24003013` | AQUA — LEVEL3/L3 Brightness Temperature 18GHz-H (TB18GHz_H) |
| `aqua-amsr-e-l3-tb18ghz-v` | — | `24003012` | AQUA — LEVEL3/L3 Brightness Temperature 18GHz-V (TB18GHz_V) |
| `aqua-amsr-e-l3-tb23ghz-h` | — | `24003015` | AQUA — LEVEL3/L3 Brightness Temperature 23GHz-H (TB23GHz_H) |
| `aqua-amsr-e-l3-tb23ghz-v` | — | `24003014` | AQUA — LEVEL3/L3 Brightness Temperature 23GHz-V (TB23GHz_V) |
| `aqua-amsr-e-l3-tb36ghz-h` | — | `24003017` | AQUA — LEVEL3/L3 Brightness Temperature 36GHz-H (TB36GHz_H) |
| `aqua-amsr-e-l3-tb36ghz-v` | — | `24003016` | AQUA — LEVEL3/L3 Brightness Temperature 36GHz-V (TB36GHz_V) |
| `aqua-amsr-e-l3-tb6ghz-h` | — | `24003009` | AQUA — LEVEL3/L3 Brightness Temperature 6GHz-H (TB6GHz_H) |
| `aqua-amsr-e-l3-tb6ghz-v` | — | `24003008` | AQUA — LEVEL3/L3 Brightness Temperature 6GHz-V (TB6GHz_V) |
| `aqua-amsr-e-l3-tb89ghz-h` | — | `24003019` | AQUA — LEVEL3/L3 Brightness Temperature 89GHz-H (TB89GHz_H) |
| `aqua-amsr-e-l3-tb89ghz-v` | — | `24003018` | AQUA — LEVEL3/L3 Brightness Temperature 89GHz-V (TB89GHz_V) |
| `aqua-amsr-e-l3-wv` | — | `24003000` | AQUA — LEVEL3/L3 Water Vapor (WV) |
| `circ-alos-2-alos-2-circ-l1-rad` | — | `21001000` | CIRC — ALOS-2 CIRC L1 RAD |
| `circ-alos-2-alos-2-circ-lst` | — | `21001001` | CIRC — ALOS-2 CIRC LST |
| `earthcare-atlid-l1-atl-nom` | — | `16011000` | EarthCARE — LEVEL1/L1b Backscatter Coefficient（ATL_NOM） |
| `earthcare-atlid-l2-atl-aer` | — | `16012002` | EarthCARE — LEVEL2/Aerosol Profiles（ATL_AER） |
| `earthcare-atlid-l2-atl-af` | — | `16012001` | EarthCARE — LEVEL2/Feature Mask（ATL_AF_） |
| `earthcare-atlid-l2-atl-ald` | — | `16012007` | EarthCARE — LEVEL2/Aerosol Layer Descriptor（ATL_ALD） |
| `earthcare-atlid-l2-atl-cla` | — | `16012000` | EarthCARE — LEVEL2/Cloud and Aerosol（ATL_CLA） |
| `earthcare-atlid-l2-atl-cth` | — | `16012006` | EarthCARE — LEVEL2/Cloud Top Height（ATL_CTH） |
| `earthcare-atlid-l2-atl-ebd` | — | `16012003` | EarthCARE — LEVEL2/Extinction, Backscatter, Depolarisation（ATL_EBD） |
| `earthcare-atlid-l2-atl-ice` | — | `16012005` | EarthCARE — LEVEL2/Ice Cloud Properties（ATL_ICE） |
| `earthcare-atlid-l2-atl-tc` | — | `16012004` | EarthCARE — LEVEL2/Target Classification（ATL_TC_） |
| `earthcare-atlid-msi-bbr-l2-bm-flx` | — | `16082001` | EarthCARE — LEVEL2/BBR-estimated TOA fluxes (BM__FLX) |
| `earthcare-atlid-msi-l2-am-acd` | — | `16072001` | EarthCARE — LEVEL2/Aerosol column descriptor（AM__ACD） |
| `earthcare-atlid-msi-l2-am-cth` | — | `16072000` | EarthCARE — LEVEL2/Cloud top height（AM__CTH） |
| `earthcare-atlid-msi-l2-am-mo` | — | `16082000` | EarthCARE — LEVEL2/Merged observations（AM_MO_） |
| `earthcare-bbr-l1-bbr-nom` | — | `16031000` | EarthCARE — LEVEL1/L1b Radiometric（BBR_NOM） |
| `earthcare-bbr-msi-l2-bm-rad` | — | `16092000` | EarthCARE — LEVEL2/BBR unfiltered radiances（BM__RAD） |
| `earthcare-cpr-atlid-l2-ac-clp` | — | `16042000` | EarthCARE — LEVEL2/CPR-ATLID Synergy Cloud（AC__CLP） |
| `earthcare-cpr-atlid-l2-ac-tc` | — | `16042001` | EarthCARE — LEVEL2/CPR-ATLID target classification (AC__TC_) |
| `earthcare-cpr-atlid-msi-bbr-l2-all-3d` | — | `16052004` | EarthCARE — LEVEL2/Constructed 3D scene indecees（ALL_3D_） |
| `earthcare-cpr-atlid-msi-bbr-l2-all-df` | — | `16062001` | EarthCARE — LEVEL2/Difference 1D-Modelled and BBR-Estimated TOA Fluxes（ALL_DF_） |
| `earthcare-cpr-atlid-msi-bbr-l2-all-rad` | — | `16062000` | EarthCARE — LEVEL2/Four Sensors Synergy Radiation Budget（ALL_RAD） |
| `earthcare-cpr-atlid-msi-l2-acm-cap` | — | `16052002` | EarthCARE — LEVEL2/Cloud and precipitation best estimates（ACM_CAP） |
| `earthcare-cpr-atlid-msi-l2-acm-clp` | — | `16052000` | EarthCARE — LEVEL2/CPR-ATLID-MSI Synergy Cloud（ACM_CLP） |
| `earthcare-cpr-atlid-msi-l2-acm-com` | — | `16052003` | EarthCARE — LEVEL2/Composite cloud /aerosol profiles derived from L2a（ACM_COM） |
| `earthcare-cpr-atlid-msi-l2-acm-rt` | — | `16052001` | EarthCARE — LEVEL2/Radiative flux and heating rate profiles & TOA (ACM_RT) |
| `earthcare-cpr-eco` | `earthcare`, `cpr` | `16002000` | EarthCARE — LEVEL2/Echo Product（CPR_ECO） |
| `earthcare-cpr-l1-cpr-nom` | — | `16001000` | EarthCARE — LEVEL1/CPR L1b（CPR_NOM） |
| `earthcare-cpr-l2-cpr-cd` | — | `16002003` | EarthCARE — LEVEL2/Corrected CPR Doppler Measurements（CPR_CD_） |
| `earthcare-cpr-l2-cpr-cld` | — | `16002005` | EarthCARE — LEVEL2/Cloud profiles（CPR_CLD） |
| `earthcare-cpr-l2-cpr-clp` | — | `16002001` | EarthCARE — LEVEL2/Cloud Product（CPR_CLP） |
| `earthcare-cpr-l2-cpr-fmr` | — | `16002002` | EarthCARE — LEVEL2/Feature Mask and Corrected Reflectivity（CPR_FMR） |
| `earthcare-cpr-l2-cpr-tc` | — | `16002004` | EarthCARE — LEVEL2/Target Classification（CPR_TC_） |
| `earthcare-msi-l1-msi-nom-1b` | — | `16021000` | EarthCARE — LEVEL1/L1b Radiance（MSI_NOM_1B） |
| `earthcare-msi-l1-msi-rgr-1c` | — | `16021001` | EarthCARE — LEVEL1/L1c Radiance（MSI_RGR_1C） |
| `earthcare-msi-l2-msi-aot` | — | `16022003` | EarthCARE — LEVEL2/Aerosol Optical Thickness （MSI_AOT） |
| `earthcare-msi-l2-msi-clp` | — | `16022000` | EarthCARE — LEVEL2/Cloud Product（MSI_CLP） |
| `earthcare-msi-l2-msi-cm` | — | `16022001` | EarthCARE — LEVEL2/Cloud Mask （MSI_CM_） |
| `earthcare-msi-l2-msi-cop` | — | `16022002` | EarthCARE — LEVEL2/Cloud Microphysical Parameters（MSI_COP） |
| `earthcare-supprt-data-aux-2d` | — | `16104000` | EarthCARE — Environment Auxiliary（AUX_2D_） |
| `earthcare-supprt-data-aux-3d` | — | `16104001` | EarthCARE — Environment Auxiliary（AUX_3D_） |
| `earthcare-supprt-data-x-jsg` | — | `16101000` | EarthCARE — L1d Joint Standard Grid (X-JSG) |
| `gosat-gw-amsr3-l1b` | `gosat-gw`, `amsr3` | `31001001` | GOSAT-GW/AMSR3 — L1B - Brightness Temperature (TBB) |
| `gpm-dpr-kupr-l1b` | `gpm`, `dpr`, `kupr` | `12001000` | GPM — LEVEL1/KuPR L1B Received Power |
| `gpm-dpr-l1-kapr-l1b-received-power` | — | `12001001` | GPM — LEVEL1/KaPR L1B Received Power |
| `gpm-dpr-l2-dpr-l2-precipitation` | — | `12002002` | GPM — LEVEL2/DPR L2 Precipitation |
| `gpm-dpr-l2-dpr-l2-spectral-latent-heating` | — | `12002003` | GPM — LEVEL2/DPR L2 Spectral Latent Heating |
| `gpm-dpr-l2-kapr-l2-precipitation` | — | `12002001` | GPM — LEVEL2/KaPR L2 Precipitation |
| `gpm-dpr-l2-kupr-l2-precipitation` | — | `12002000` | GPM — LEVEL2/KuPR L2 Precipitation |
| `gpm-dpr-l3-daily-hdf` | — | `12003000` | GPM — LEVEL3/DPR L3 Precipitation (Daily HDF) |
| `gpm-dpr-l3-daily-text` | — | `12003001` | GPM — LEVEL3/DPR L3 Precipitation (Daily TEXT) |
| `gpm-dpr-l3-dpr-l3-gridded-orbital-spectra` | — | `12003006` | GPM — LEVEL3/DPR L3 Gridded Orbital Spectral Latent Heating |
| `gpm-dpr-l3-dpr-l3-monthly-spectral-latent` | — | `12003007` | GPM — LEVEL3/DPR L3 Monthly Spectral Latent Heating |
| `gpm-dpr-l3-monthly-hdf` | — | `12003004` | GPM — LEVEL3/DPR L3  Precipitation (Monthly HDF) |
| `gpm-environment-auxiliary-dpr-environment-auxiliary` | — | `12032002` | GPM — DPR Environment Auxiliary |
| `gpm-environment-auxiliary-kapr-environment-auxiliary` | — | `12032001` | GPM — KaPR Environment Auxiliary |
| `gpm-environment-auxiliary-kupr-environment-auxiliary` | — | `12032000` | GPM — KuPR Environment Auxiliary |
| `gpm-gmi-comb-l2-comb-l2-precipitation` | — | `12022000` | GPM — LEVEL2/COMB L2 Precipitation |
| `gpm-gmi-comb-l3-comb-dpr-gmi-l3-precipitation` | — | `12023000` | GPM — LEVEL3/Comb DPR/GMI L3 Precipitation |
| `gpm-gmi-comb-l3-comb-l3-gridded-orbital-spectr` | — | `12023001` | GPM — LEVEL3/COMB L3 Gridded Orbital Spectral Latent Heating |
| `gpm-gmi-comb-l3-comb-l3-monthly-spectral-laten` | — | `12023002` | GPM — LEVEL3/COMB L3 Monthly Spectral Latent Heating |
| `gpm-gmi-l1-gmi-l1b-brightness-temperature` | — | `12011000` | GPM — LEVEL1/GMI L1B Brightness Temperature |
| `gpm-gmi-l1-gmi-l1c-inter-calibrated-brigh` | — | `12011001` | GPM — LEVEL1/GMI L1C Inter- Calibrated Brightness Temperature |
| `gpm-gmi-l2-gmi-l2-precipitation` | — | `12012000` | GPM — LEVEL2/GMI L2 Precipitation |
| `gpm-gmi-l3-gmi-l3-precipitation` | — | `12013000` | GPM — LEVEL3/GMI L3 Precipitation |
| `gpmc-amsr2-l1-gcom-w-amsr2-l1c-inter-calibra` | — | `13011000` | GPM Constellation satellites — LEVEL1/GCOM-W AMSR2 L1C Inter-Calibrated Brightness Temperature |
| `gpmc-atms-l1-npp-atms-l1c-inter-calibrated` | — | `13041000` | GPM Constellation satellites — LEVEL1/NPP ATMS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-mhs-l1-metop-a-mhs-l1c-inter-calibrat` | — | `13031002` | GPM Constellation satellites — LEVEL1/METOP-A MHS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-mhs-l1-metop-b-mhs-l1c-inter-calibrat` | — | `13031003` | GPM Constellation satellites — LEVEL1/METOP-B MHS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-mhs-l1-noaa-18-mhs-l1c-inter-calibrat` | — | `13031000` | GPM Constellation satellites — LEVEL1/NOAA-18 MHS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-mhs-l1-noaa-19-mhs-l1c-inter-calibrat` | — | `13031001` | GPM Constellation satellites — LEVEL1/NOAA-19 MHS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-saphir-l1-megha-tropiques-saphir-l1c-int` | — | `13051000` | GPM Constellation satellites — LEVEL1/Megha Tropiques SAPHIR L1C Inter-Calibrated Brightness Temperature |
| `gpmc-ssmis-l1-dmsp-f16-ssmis-l1c-inter-calib` | — | `13021000` | GPM Constellation satellites — LEVEL1/DMSP F16 SSMIS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-ssmis-l1-dmsp-f17-ssmis-l1c-inter-calib` | — | `13021001` | GPM Constellation satellites — LEVEL1/DMSP F17 SSMIS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-ssmis-l1-dmsp-f18-ssmis-l1c-inter-calib` | — | `13021002` | GPM Constellation satellites — LEVEL1/DMSP F18 SSMIS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-ssmis-l1-dmsp-f19-ssmis-l1c-inter-calib` | — | `13021003` | GPM Constellation satellites — LEVEL1/DMSP F19 SSMIS L1C Inter-Calibrated Brightness Temperature |
| `gpmc-tmi-l1-trmm-tmi-l1c-inter-calibrated` | — | `13001000` | GPM Constellation satellites — LEVEL1/TRMM TMI L1C Inter-Calibrated Brightness Temperature |
| `gsmap-gp-l3-hourly-hdf` | — | `14003000` | GSMaP — GSMaP Precipitation(Hourly HDF) |
| `gsmap-gp-l3-hourly-text` | — | `14003001` | GSMaP — GSMaP Precipitation(Hourly TEXT) |
| `gsmap-gp-l3-monthly-hdf` | — | `14003004` | GSMaP — GSMaP Precipitation(Monthly HDF) |
| `jers1-sar-l0` | — | `17020000` | JERS-1 — SAR/L0/SAR（L0) |
| `jers1-sar-l2-1-sar` | — | `17022000` | JERS-1 — SAR/L2.1/SAR |
| `jers1-swir-l2-swir` | — | `17012000` | JERS-1 — SWIR/L2/SWIR |
| `jers1-vnir-l2-vnir` | — | `17002000` | JERS-1 — VNIR/L2/VNIR |
| `mos1-messr-messr` | — | `19004000` | MOS-1 — MOS-1/MESSR |
| `mos1b-messr-messr` | — | `20004000` | MOS-1b — MOS-1b/MESSR |
| `nasacmr-aqua-modis-aqua-aerosol-5-min-l2-sw` | — | `28002002` | NASA-CMR — Level2/MODIS/Aqua Aerosol 5-Min L2 Swath 3km |
| `nasacmr-aqua-modis-aqua-aerosol-5-min-l2-sw-2003` | — | `28002003` | NASA-CMR — Level2/MODIS/Aqua Aerosol 5-Min L2 Swath 10km |
| `nasacmr-aqua-modis-aqua-aerosol-cloud-water` | — | `28003013` | NASA-CMR — Level3/MODIS/Aqua Aerosol Cloud Water Vapor Ozone Daily L3 Global 1Deg CMG |
| `nasacmr-aqua-modis-aqua-aerosol-cloud-water-3014` | — | `28003014` | NASA-CMR — Level3/MODIS/Aqua Aerosol Cloud Water Vapor Ozone 8-Day L3 Global 1Deg CMG |
| `nasacmr-aqua-modis-aqua-aerosol-cloud-water-3015` | — | `28003015` | NASA-CMR — Level3/MODIS/Aqua Aerosol Cloud Water Vapor Ozone Monthly L3 Global 1Deg CMG |
| `nasacmr-aqua-modis-aqua-clouds-5-min-l2-swa` | — | `28002004` | NASA-CMR — Level2/MODIS/Aqua Clouds 5-Min L2 Swath 1km and 5km |
| `nasacmr-aqua-modis-aqua-land-surface-temper` | — | `28003000` | NASA-CMR — Level3/MODIS/Aqua Land Surface Temperature/Emissivity Daily L3 Global 1km SIN Grid V006 |
| `nasacmr-aqua-modis-aqua-land-surface-temper-3001` | — | `28003001` | NASA-CMR — Level3/MODIS/Aqua Land Surface Temperature/Emissivity 8-Day L3 Global 1km SIN Grid V006 |
| `nasacmr-aqua-modis-aqua-land-surface-temper-3002` | — | `28003002` | NASA-CMR — Level3/MODIS/Aqua Land Surface Temperature/Emissivity Daily L3 Global 0.05Deg CMG V006 |
| `nasacmr-aqua-modis-aqua-land-surface-temper-3003` | — | `28003003` | NASA-CMR — Level3/MODIS/Aqua Land Surface Temperature/Emissivity 8-Day L3 Global 0.05Deg CMG V006 |
| `nasacmr-aqua-modis-aqua-land-surface-temper-3004` | — | `28003004` | NASA-CMR — Level3/MODIS/Aqua Land Surface Temperature/Emissivity Monthly L3 Global 0.05Deg CMG V006 |
| `nasacmr-aqua-modis-aqua-sea-surface-tempera` | — | `28002000` | NASA-CMR — Level2/MODIS/Aqua Sea Surface Temperature L2 |
| `nasacmr-aqua-modis-aqua-sea-surface-tempera-3011` | — | `28003011` | NASA-CMR — Level3/MODIS/Aqua Sea Surface Temperature L3 Monthly |
| `nasacmr-aqua-modis-aqua-snow-cover-8-day-l3` | — | `28003017` | NASA-CMR — Level3/MODIS/Aqua Snow Cover 8-Day L3 Global 0.05Deg CMG V005 |
| `nasacmr-aqua-modis-aqua-snow-cover-daily-l3` | — | `28003016` | NASA-CMR — Level3/MODIS/Aqua Snow Cover Daily L3 Global 0.05Deg CMG V005 |
| `nasacmr-aqua-modis-aqua-vegetation-indices` | — | `28003005` | NASA-CMR — Level3/MODIS/Aqua Vegetation Indices 16-Day L3 Global 500m SIN Grid V006 |
| `nasacmr-aqua-modis-aqua-vegetation-indices-3006` | — | `28003006` | NASA-CMR — Level3/MODIS/Aqua Vegetation Indices 16-Day L3 Global 1km SIN Grid V006 |
| `nasacmr-aqua-modis-aqua-vegetation-indices-3007` | — | `28003007` | NASA-CMR — Level3/MODIS/Aqua Vegetation Indices Monthly L3 Global 1km SIN Grid V006 |
| `nasacmr-aqua-modis-aqua-vegetation-indices-3008` | — | `28003008` | NASA-CMR — Level3/MODIS/Aqua Vegetation Indices 16-Day L3 Global 0.05Deg CMG V006 |
| `nasacmr-aqua-modis-aqua-vegetation-indices-3009` | — | `28003009` | NASA-CMR — Level3/MODIS/Aqua Vegetation Indices Monthly L3 Global 0.05Deg CMG V006 |
| `nasacmr-aqua-modis-aqua-vegetation-indices-3010` | — | `28003010` | NASA-CMR — Level3/MODIS/Aqua Vegetation Indices 16-Day L3 Global 250m SIN Grid V006 |
| `nasacmr-aqua-modisa-l2-ocean-color` | — | `28002001` | NASA-CMR — Level2/MODISA_L2_Ocean Color |
| `nasacmr-aqua-modisa-l3m-chlorophyll-a` | — | `28003012` | NASA-CMR — Level3/MODISA_L3m_Chlorophyll-a |
| `nasacmr-terra-modis-terra-aerosol-5-min-l2-s` | — | `29002002` | NASA-CMR — Level2/MODIS/Terra Aerosol 5-Min L2 Swath 3km |
| `nasacmr-terra-modis-terra-aerosol-5-min-l2-s-2003` | — | `29002003` | NASA-CMR — Level2/MODIS/Terra Aerosol 5-Min L2 Swath 10km |
| `nasacmr-terra-modis-terra-aerosol-cloud-wate` | — | `29003013` | NASA-CMR — Level3/MODIS/Terra Aerosol Cloud Water Vapor Ozone Daily L3 Global 1Deg CMG |
| `nasacmr-terra-modis-terra-aerosol-cloud-wate-3014` | — | `29003014` | NASA-CMR — Level3/MODIS/Terra Aerosol Cloud Water Vapor Ozone 8-Day L3 Global 1Deg CMG |
| `nasacmr-terra-modis-terra-aerosol-cloud-wate-3015` | — | `29003015` | NASA-CMR — Level3/MODIS/Terra Aerosol Cloud Water Vapor Ozone Monthly L3 Global 1Deg CMG |
| `nasacmr-terra-modis-terra-clouds-5-min-l2-sw` | — | `29002004` | NASA-CMR — Level2/MODIS/Terra Clouds 5-Min L2 Swath 1km and 5km |
| `nasacmr-terra-modis-terra-land-surface-tempe` | — | `29003000` | NASA-CMR — Level3/MODIS/Terra Land Surface Temperature/Emissivity Daily L3 Global 1km SIN Grid V006 |
| `nasacmr-terra-modis-terra-land-surface-tempe-3001` | — | `29003001` | NASA-CMR — Level3/MODIS/Terra Land Surface Temperature/Emissivity 8-Day L3 Global 1km SIN Grid V006 |
| `nasacmr-terra-modis-terra-land-surface-tempe-3002` | — | `29003002` | NASA-CMR — Level3/MODIS/Terra Land Surface Temperature/Emissivity Daily L3 Global 0.05Deg CMG V006 |
| `nasacmr-terra-modis-terra-land-surface-tempe-3003` | — | `29003003` | NASA-CMR — Level3/MODIS/Terra Land Surface Temperature/Emissivity 8-Day L3 Global 0.05Deg CMG V006 |
| `nasacmr-terra-modis-terra-land-surface-tempe-3004` | — | `29003004` | NASA-CMR — Level3/MODIS/Terra Land Surface Temperature/Emissivity Monthly L3 Global 0.05Deg CMG V006 |
| `nasacmr-terra-modis-terra-snow-cover-8-day-l` | — | `29003017` | NASA-CMR — Level3/MODIS/Terra Snow Cover 8-Day L3 Global 0.05Deg CMG V005 |
| `nasacmr-terra-modis-terra-snow-cover-daily-l` | — | `29003016` | NASA-CMR — Level3/MODIS/Terra Snow Cover Daily L3 Global 0.05Deg CMG V005 |
| `nasacmr-terra-modis-terra-vegetation-indices` | — | `29003005` | NASA-CMR — Level3/MODIS/Terra Vegetation Indices 16-Day L3 Global 500m SIN Grid V006 |
| `nasacmr-terra-modis-terra-vegetation-indices-3006` | — | `29003006` | NASA-CMR — Level3/MODIS/Terra Vegetation Indices 16-Day L3 Global 1km SIN Grid V006 |
| `nasacmr-terra-modis-terra-vegetation-indices-3007` | — | `29003007` | NASA-CMR — Level3/MODIS/Terra Vegetation Indices Monthly L3 Global 1km SIN Grid V006 |
| `nasacmr-terra-modis-terra-vegetation-indices-3008` | — | `29003008` | NASA-CMR — Level3/MODIS/Terra Vegetation Indices 16-Day L3 Global 0.05Deg CMG V006 |
| `nasacmr-terra-modis-terra-vegetation-indices-3009` | — | `29003009` | NASA-CMR — Level3/MODIS/Terra Vegetation Indices Monthly L3 Global 0.05Deg CMG V006 |
| `nasacmr-terra-modis-terra-vegetation-indices-3010` | — | `29003010` | NASA-CMR — Level3/MODIS/Terra Vegetation Indices 16-Day L3 Global 250m SIN Grid V006 |
| `nasacmr-terra-modist-l2-oc` | — | `29002001` | NASA-CMR — Level2/MODIST_L2_OC |
| `nasacmr-terra-modist-l2-sst` | — | `29002000` | NASA-CMR — Level2/MODIST_L2_SST |
| `nasacmr-terra-modist-l3m-chl` | — | `29003012` | NASA-CMR — Level3/MODIST_L3m_CHL |
| `nasacmr-terra-modist-l3m-sst` | — | `29003011` | NASA-CMR — Level3/MODIST_L3m_SST |
| `palsar2-fine-dp` | — | `27004007` | ALOS-2 — NA/Fine[10m] DP |
| `palsar2-fine-qp` | — | `27004008` | ALOS-2 — NA/Fine[10m] QP |
| `palsar2-fine-sp` | — | `27004006` | ALOS-2 — NA/Fine[10m] SP |
| `palsar2-hight-sensitive-dp` | — | `27004004` | ALOS-2 — NA/Hight-sensitive[6m] DP |
| `palsar2-hight-sensitive-qp` | — | `27004005` | ALOS-2 — NA/Hight-sensitive[6m] QP |
| `palsar2-hight-sensitive-sp` | — | `27004003` | ALOS-2 — NA/Hight-sensitive[6m] SP |
| `palsar2-scansar-14mhz-dp` | — | `27004010` | ALOS-2 — NA/ScanSAR\[350km\](14MHz) DP |
| `palsar2-scansar-14mhz-sp` | — | `27004009` | ALOS-2 — NA/ScanSAR\[350km\](14MHz) SP |
| `palsar2-scansar-28mhz-dp` | — | `27004012` | ALOS-2 — NA/ScanSAR\[350km\](28MHz) DP |
| `palsar2-scansar-28mhz-sp` | — | `27004011` | ALOS-2 — NA/ScanSAR\[350km\](28MHz) SP |
| `palsar2-scansar-sp` | — | `27004013` | ALOS-2 — NA/ScanSAR[490km] SP |
| `palsar2-scansardp` | — | `27004014` | ALOS-2 — NA/ScanSAR[490km]DP |
| `palsar2-spotlight-sp` | — | `27004000` | ALOS-2 — NA/Spotlight SP |
| `palsar2-ultra-fine-dp` | — | `27004002` | ALOS-2 — NA/Ultra-fine[3m] DP |
| `sgli-l1-swi-tir` | — | `10001002` | GCOM-C/SGLI — L1A-SWI & TIR |
| `sgli-l1-swi-tir-1005` | — | `10001005` | GCOM-C/SGLI — L1B-SWI & TIR |
| `sgli-l1-visible-near-infrared-pol` | — | `10001001` | GCOM-C/SGLI — L1A-Visible & Near Infrared, POL |
| `sgli-l1-visible-near-infrared-pol-1004` | — | `10001004` | GCOM-C/SGLI — L1B-Visible & Near Infrared, POL |
| `sgli-l1-visible-near-infrared-vnr` | — | `10001000` | GCOM-C/SGLI — L1A-Visible & Near Infrared, VNR |
| `sgli-l1-visible-near-infrared-vnr-1003` | — | `10001003` | GCOM-C/SGLI — L1B-Visible & Near Infrared, VNR |
| `sgli-l2-arnp` | — | `10002079` | GCOM-C/SGLI — Atmosphere/L2-ARNP |
| `sgli-l2-arnp-ver-2` | — | `10002060` | GCOM-C/SGLI — Atmosphere/L2-ARNP Ver.2 |
| `sgli-l2-arnp-ver-2-2059` | — | `10002059` | GCOM-C/SGLI — Atmosphere/L2-ARNP Ver.2 |
| `sgli-l2-arpl-ver-2` | — | `10002062` | GCOM-C/SGLI — Atmosphere/L2-ARPL Ver.2 |
| `sgli-l2-arpl-ver-2-2061` | — | `10002061` | GCOM-C/SGLI — Atmosphere/L2-ARPL Ver.2 |
| `sgli-l2-atmosphere-global-l2-global-ar` | — | `10002080` | GCOM-C/SGLI — Atmosphere global/L2 global-ARNP |
| `sgli-l2-atmosphere-global-l2-global-ar-2071` | — | `10002071` | GCOM-C/SGLI — Atmosphere global/L2 global-ARNP Ver.2 |
| `sgli-l2-atmosphere-global-l2-global-ar-2072` | — | `10002072` | GCOM-C/SGLI — Atmosphere global/L2 global-ARNP Ver.2 |
| `sgli-l2-atmosphere-global-l2-global-ar-2073` | — | `10002073` | GCOM-C/SGLI — Atmosphere global/L2 global-ARPL Ver.2 |
| `sgli-l2-atmosphere-global-l2-global-ar-2074` | — | `10002074` | GCOM-C/SGLI — Atmosphere global/L2 global-ARPL Ver.2 |
| `sgli-l2-atmosphere-global-l2-global-cl` | — | `10002068` | GCOM-C/SGLI — Atmosphere global/L2 global-CLFG |
| `sgli-l2-atmosphere-global-l2-global-cl-2067` | — | `10002067` | GCOM-C/SGLI — Atmosphere global/L2 global-CLFG |
| `sgli-l2-atmosphere-global-l2-global-cl-2069` | — | `10002069` | GCOM-C/SGLI — Atmosphere global/L2 global-CLPR |
| `sgli-l2-atmosphere-global-l2-global-cl-2070` | — | `10002070` | GCOM-C/SGLI — Atmosphere global/L2 global-CLPR |
| `sgli-l2-atmosphere-global-l2-global-lc` | — | `10002063` | GCOM-C/SGLI — Atmosphere global/L2 global-LCLR |
| `sgli-l2-atmosphere-global-l2-global-lt` | — | `10002066` | GCOM-C/SGLI — Atmosphere global/L2 global-LTOA |
| `sgli-l2-atmosphere-global-l2-global-lt-2064` | — | `10002064` | GCOM-C/SGLI — Atmosphere global/L2 global-LTOA |
| `sgli-l2-atmosphere-global-l2-global-lt-2065` | — | `10002065` | GCOM-C/SGLI — Atmosphere global/L2 global-LTOA |
| `sgli-l2-clfg` | — | `10002056` | GCOM-C/SGLI — Atmosphere/L2-CLFG |
| `sgli-l2-clfg-2055` | — | `10002055` | GCOM-C/SGLI — Atmosphere/L2-CLFG |
| `sgli-l2-clpr` | — | `10002058` | GCOM-C/SGLI — Atmosphere/L2-CLPR |
| `sgli-l2-clpr-2057` | — | `10002057` | GCOM-C/SGLI — Atmosphere/L2-CLPR |
| `sgli-l2-cryosphere-statistics-l2-stati` | — | `10002009` | GCOM-C/SGLI — Cryosphere statistics/L2 statistics-SICE |
| `sgli-l2-cryosphere-statistics-l2-stati-2008` | — | `10002008` | GCOM-C/SGLI — Cryosphere statistics/L2 statistics-SICE |
| `sgli-l2-cryosphere-statistics-l2-stati-2010` | — | `10002010` | GCOM-C/SGLI — Cryosphere statistics/L2 statistics-SIST |
| `sgli-l2-cryosphere-statistics-l2-stati-2011` | — | `10002011` | GCOM-C/SGLI — Cryosphere statistics/L2 statistics-SGSL |
| `sgli-l2-cryosphere-statistics-l2-stati-2077` | — | `10002077` | GCOM-C/SGLI — Cryosphere statistics/L2 statistics-SALB |
| `sgli-l2-land-area-l2-agb` | — | `10002018` | GCOM-C/SGLI — Land area/L2-AGB |
| `sgli-l2-land-area-l2-lai` | — | `10002017` | GCOM-C/SGLI — Land area/L2-LAI |
| `sgli-l2-land-area-l2-lst` | — | `10002019` | GCOM-C/SGLI — Land area/L2-LST |
| `sgli-l2-land-area-l2-ltoa` | — | `10002014` | GCOM-C/SGLI — Land area/L2-LTOA |
| `sgli-l2-land-area-l2-ltoa-2012` | — | `10002012` | GCOM-C/SGLI — Land area/L2-LTOA |
| `sgli-l2-land-area-l2-ltoa-2013` | — | `10002013` | GCOM-C/SGLI — Land area/L2-LTOA |
| `sgli-l2-land-area-l2-rsrf` | — | `10002015` | GCOM-C/SGLI — Land area/L2-RSRF |
| `sgli-l2-land-area-l2-vgi` | — | `10002016` | GCOM-C/SGLI — Land area/L2-VGI |
| `sgli-l2-land-area-statistics-l2-statis` | — | `10002022` | GCOM-C/SGLI — Land area statistics/L2 statistics-LTOA |
| `sgli-l2-land-area-statistics-l2-statis-2020` | — | `10002020` | GCOM-C/SGLI — Land area statistics/L2 statistics-LTOA |
| `sgli-l2-land-area-statistics-l2-statis-2021` | — | `10002021` | GCOM-C/SGLI — Land area statistics/L2 statistics-LTOA |
| `sgli-l2-land-area-statistics-l2-statis-2023` | — | `10002023` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2024` | — | `10002024` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2025` | — | `10002025` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2026` | — | `10002026` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2027` | — | `10002027` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2028` | — | `10002028` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2029` | — | `10002029` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2030` | — | `10002030` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2031` | — | `10002031` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2032` | — | `10002032` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2033` | — | `10002033` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2034` | — | `10002034` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2035` | — | `10002035` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2036` | — | `10002036` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2037` | — | `10002037` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2038` | — | `10002038` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2039` | — | `10002039` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2040` | — | `10002040` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2041` | — | `10002041` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2042` | — | `10002042` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2043` | — | `10002043` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2044` | — | `10002044` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2045` | — | `10002045` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2046` | — | `10002046` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-land-area-statistics-l2-statis-2047` | — | `10002047` | GCOM-C/SGLI — Land area statistics/L2 statistics-NDVI |
| `sgli-l2-land-area-statistics-l2-statis-2048` | — | `10002048` | GCOM-C/SGLI — Land area statistics/L2 statistics-EVI |
| `sgli-l2-land-area-statistics-l2-statis-2049` | — | `10002049` | GCOM-C/SGLI — Land area statistics/L2 statistics-SDI |
| `sgli-l2-land-area-statistics-l2-statis-2050` | — | `10002050` | GCOM-C/SGLI — Land area statistics/L2 statistics-FPAR |
| `sgli-l2-land-area-statistics-l2-statis-2051` | — | `10002051` | GCOM-C/SGLI — Land area statistics/L2 statistics-LAI |
| `sgli-l2-land-area-statistics-l2-statis-2052` | — | `10002052` | GCOM-C/SGLI — Land area statistics/L2 statistics-VRI |
| `sgli-l2-land-area-statistics-l2-statis-2053` | — | `10002053` | GCOM-C/SGLI — Land area statistics/L2 statistics-AGB |
| `sgli-l2-land-area-statistics-l2-statis-2054` | — | `10002054` | GCOM-C/SGLI — Land area statistics/L2 statistics-LST |
| `sgli-l2-land-area-statistics-l2-statis-2078` | — | `10002078` | GCOM-C/SGLI — Land area statistics/L2 statistics-RSRF |
| `sgli-l2-oceanic-sphere-l2-iwpr` | — | `10002001` | GCOM-C/SGLI — Oceanic sphere/L2-IWPR |
| `sgli-l2-oceanic-sphere-l2-nwlr` | — | `10002000` | GCOM-C/SGLI — Oceanic sphere/L2-NWLR |
| `sgli-l2-oceanic-sphere-l2-sst` | — | `10002002` | GCOM-C/SGLI — Oceanic sphere/L2-SST |
| `sgli-l2-okid` | — | `10002003` | GCOM-C/SGLI — Cryosphere/L2-OKID |
| `sgli-l2-sice` | — | `10002005` | GCOM-C/SGLI — Cryosphere/L2-SICE |
| `sgli-l2-sice-2004` | — | `10002004` | GCOM-C/SGLI — Cryosphere/L2-SICE |
| `sgli-l2-sipr` | — | `10002076` | GCOM-C/SGLI — Cryosphere/L2-SIPR |
| `sgli-l2-sipr-2075` | — | `10002075` | GCOM-C/SGLI — Cryosphere/L2-SIPR |
| `sgli-l2-sipr-ver-2` | — | `10002007` | GCOM-C/SGLI — Cryosphere/L2-SIPR ver.2 |
| `sgli-l2-sipr-ver-2-2006` | — | `10002006` | GCOM-C/SGLI — Cryosphere/L2-SIPR ver.2 |
| `sgli-l3-aael-ver-2` | — | `10003165` | GCOM-C/SGLI — Atmosphere/L3-AAEL Ver.2 |
| `sgli-l3-aael-ver-2-3164` | — | `10003164` | GCOM-C/SGLI — Atmosphere/L3-AAEL Ver.2 |
| `sgli-l3-aaeo-ver-2` | — | `10003163` | GCOM-C/SGLI — Atmosphere/L3-AAEO Ver.2 |
| `sgli-l3-aaeo-ver-2-3162` | — | `10003162` | GCOM-C/SGLI — Atmosphere/L3-AAEO Ver.2 |
| `sgli-l3-aaep-ver-2` | — | `10003169` | GCOM-C/SGLI — Atmosphere/L3-AAEP Ver.2 |
| `sgli-l3-aaep-ver-2-3168` | — | `10003168` | GCOM-C/SGLI — Atmosphere/L3-AAEP Ver.2 |
| `sgli-l3-aotl-ver-2` | — | `10003161` | GCOM-C/SGLI — Atmosphere/L3-AOTL Ver.2 |
| `sgli-l3-aotl-ver-2-3160` | — | `10003160` | GCOM-C/SGLI — Atmosphere/L3-AOTL Ver.2 |
| `sgli-l3-aoto-ver-2` | — | `10003159` | GCOM-C/SGLI — Atmosphere/L3-AOTO Ver.2 |
| `sgli-l3-aoto-ver-2-3158` | — | `10003158` | GCOM-C/SGLI — Atmosphere/L3-AOTO Ver.2 |
| `sgli-l3-aotp-ver-2` | — | `10003167` | GCOM-C/SGLI — Atmosphere/L3-AOTP Ver.2 |
| `sgli-l3-aotp-ver-2-3166` | — | `10003166` | GCOM-C/SGLI — Atmosphere/L3-AOTP Ver.2 |
| `sgli-l3-arae` | — | `10003175` | GCOM-C/SGLI — Atmosphere/L3-ARAE |
| `sgli-l3-arae-3174` | — | `10003174` | GCOM-C/SGLI — Atmosphere/L3-ARAE |
| `sgli-l3-arot` | — | `10003173` | GCOM-C/SGLI — Atmosphere/L3-AROT |
| `sgli-l3-arot-3172` | — | `10003172` | GCOM-C/SGLI — Atmosphere/L3-AROT |
| `sgli-l3-assa` | — | `10003177` | GCOM-C/SGLI — Atmosphere/L3-ASSA |
| `sgli-l3-assa-3176` | — | `10003176` | GCOM-C/SGLI — Atmosphere/L3-ASSA |
| `sgli-l3-assa-ver-2` | — | `10003171` | GCOM-C/SGLI — Atmosphere/L3-ASSA Ver.2 |
| `sgli-l3-assa-ver-2-3170` | — | `10003170` | GCOM-C/SGLI — Atmosphere/L3-ASSA Ver.2 |
| `sgli-l3-cerw` | — | `10003155` | GCOM-C/SGLI — Atmosphere/L3-CERW |
| `sgli-l3-cerw-3154` | — | `10003154` | GCOM-C/SGLI — Atmosphere/L3-CERW |
| `sgli-l3-clfr` | — | `10003147` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3122` | — | `10003122` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3123` | — | `10003123` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3124` | — | `10003124` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3125` | — | `10003125` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3126` | — | `10003126` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3127` | — | `10003127` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3128` | — | `10003128` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3129` | — | `10003129` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3130` | — | `10003130` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3131` | — | `10003131` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3132` | — | `10003132` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3133` | — | `10003133` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3134` | — | `10003134` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3135` | — | `10003135` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3136` | — | `10003136` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3137` | — | `10003137` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3138` | — | `10003138` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3139` | — | `10003139` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3140` | — | `10003140` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3141` | — | `10003141` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3142` | — | `10003142` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3143` | — | `10003143` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3144` | — | `10003144` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3145` | — | `10003145` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clfr-3146` | — | `10003146` | GCOM-C/SGLI — Atmosphere/L3-CLFR |
| `sgli-l3-clth` | — | `10003151` | GCOM-C/SGLI — Atmosphere/L3-CLTH |
| `sgli-l3-clth-3150` | — | `10003150` | GCOM-C/SGLI — Atmosphere/L3-CLTH |
| `sgli-l3-cltt` | — | `10003149` | GCOM-C/SGLI — Atmosphere/L3-CLTT |
| `sgli-l3-cltt-3148` | — | `10003148` | GCOM-C/SGLI — Atmosphere/L3-CLTT |
| `sgli-l3-coti` | — | `10003157` | GCOM-C/SGLI — Atmosphere/L3-COTI |
| `sgli-l3-coti-3156` | — | `10003156` | GCOM-C/SGLI — Atmosphere/L3-COTI |
| `sgli-l3-cotw` | — | `10003153` | GCOM-C/SGLI — Atmosphere/L3-COTW |
| `sgli-l3-cotw-3152` | — | `10003152` | GCOM-C/SGLI — Atmosphere/L3-COTW |
| `sgli-l3-land-area-l3-agb` | — | `10003117` | GCOM-C/SGLI — Land area/L3-AGB |
| `sgli-l3-land-area-l3-agb-3116` | — | `10003116` | GCOM-C/SGLI — Land area/L3-AGB |
| `sgli-l3-land-area-l3-evi` | — | `10003109` | GCOM-C/SGLI — Land area/L3-EVI |
| `sgli-l3-land-area-l3-evi-3108` | — | `10003108` | GCOM-C/SGLI — Land area/L3-EVI |
| `sgli-l3-land-area-l3-fpar` | — | `10003113` | GCOM-C/SGLI — Land area/L3-FPAR |
| `sgli-l3-land-area-l3-fpar-3112` | — | `10003112` | GCOM-C/SGLI — Land area/L3-FPAR |
| `sgli-l3-land-area-l3-lai` | — | `10003115` | GCOM-C/SGLI — Land area/L3-LAI |
| `sgli-l3-land-area-l3-lai-3114` | — | `10003114` | GCOM-C/SGLI — Land area/L3-LAI |
| `sgli-l3-land-area-l3-lst` | — | `10003121` | GCOM-C/SGLI — Land area/L3-LST |
| `sgli-l3-land-area-l3-lst-3120` | — | `10003120` | GCOM-C/SGLI — Land area/L3-LST |
| `sgli-l3-land-area-l3-ndvi` | — | `10003107` | GCOM-C/SGLI — Land area/L3-NDVI |
| `sgli-l3-land-area-l3-ndvi-3106` | — | `10003106` | GCOM-C/SGLI — Land area/L3-NDVI |
| `sgli-l3-land-area-l3-rsrf` | — | `10003105` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3040` | — | `10003040` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3041` | — | `10003041` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3042` | — | `10003042` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3043` | — | `10003043` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3044` | — | `10003044` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3045` | — | `10003045` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3046` | — | `10003046` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3047` | — | `10003047` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3048` | — | `10003048` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3049` | — | `10003049` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3050` | — | `10003050` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3051` | — | `10003051` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3052` | — | `10003052` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3053` | — | `10003053` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3054` | — | `10003054` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3055` | — | `10003055` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3056` | — | `10003056` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3057` | — | `10003057` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3058` | — | `10003058` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3059` | — | `10003059` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3060` | — | `10003060` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3061` | — | `10003061` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3062` | — | `10003062` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3063` | — | `10003063` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3064` | — | `10003064` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3065` | — | `10003065` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3066` | — | `10003066` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3067` | — | `10003067` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3068` | — | `10003068` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3069` | — | `10003069` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3070` | — | `10003070` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3071` | — | `10003071` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3072` | — | `10003072` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3073` | — | `10003073` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3074` | — | `10003074` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3075` | — | `10003075` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3076` | — | `10003076` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3077` | — | `10003077` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3078` | — | `10003078` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3079` | — | `10003079` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3080` | — | `10003080` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3081` | — | `10003081` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3082` | — | `10003082` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3083` | — | `10003083` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3084` | — | `10003084` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3085` | — | `10003085` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3086` | — | `10003086` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3087` | — | `10003087` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3088` | — | `10003088` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3089` | — | `10003089` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3090` | — | `10003090` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3091` | — | `10003091` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3092` | — | `10003092` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3093` | — | `10003093` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3094` | — | `10003094` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3095` | — | `10003095` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3096` | — | `10003096` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3097` | — | `10003097` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3098` | — | `10003098` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3099` | — | `10003099` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3100` | — | `10003100` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3101` | — | `10003101` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3102` | — | `10003102` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3103` | — | `10003103` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-rsrf-3104` | — | `10003104` | GCOM-C/SGLI — Land area/L3-RSRF |
| `sgli-l3-land-area-l3-sdi` | — | `10003111` | GCOM-C/SGLI — Land area/L3-SDI |
| `sgli-l3-land-area-l3-sdi-3110` | — | `10003110` | GCOM-C/SGLI — Land area/L3-SDI |
| `sgli-l3-land-area-l3-vri` | — | `10003119` | GCOM-C/SGLI — Land area/L3-VRI |
| `sgli-l3-land-area-l3-vri-3118` | — | `10003118` | GCOM-C/SGLI — Land area/L3-VRI |
| `sgli-l3-nwlr` | `sgli-l380`, `sgli-ocean-l380`, `gcom-c-nwlr` | `10003001` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-acp` | — | `10003017` | GCOM-C/SGLI — Oceanic sphere/L3-ACP |
| `sgli-l3-oceanic-sphere-l3-acp-3014` | — | `10003014` | GCOM-C/SGLI — Oceanic sphere/L3-ACP |
| `sgli-l3-oceanic-sphere-l3-acp-3015` | — | `10003015` | GCOM-C/SGLI — Oceanic sphere/L3-ACP |
| `sgli-l3-oceanic-sphere-l3-acp-3016` | — | `10003016` | GCOM-C/SGLI — Oceanic sphere/L3-ACP |
| `sgli-l3-oceanic-sphere-l3-cdom` | — | `10003025` | GCOM-C/SGLI — Oceanic sphere/L3-CDOM |
| `sgli-l3-oceanic-sphere-l3-cdom-3024` | — | `10003024` | GCOM-C/SGLI — Oceanic sphere/L3-CDOM |
| `sgli-l3-oceanic-sphere-l3-chla` | — | `10003021` | GCOM-C/SGLI — Oceanic sphere/L3-CHLA |
| `sgli-l3-oceanic-sphere-l3-chla-3020` | — | `10003020` | GCOM-C/SGLI — Oceanic sphere/L3-CHLA |
| `sgli-l3-oceanic-sphere-l3-nwlr` | — | `10003013` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3000` | — | `10003000` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3002` | — | `10003002` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3003` | — | `10003003` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3004` | — | `10003004` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3005` | — | `10003005` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3006` | — | `10003006` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3007` | — | `10003007` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3008` | — | `10003008` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3009` | — | `10003009` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3010` | — | `10003010` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3011` | — | `10003011` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-nwlr-3012` | — | `10003012` | GCOM-C/SGLI — Oceanic sphere/L3-NWLR |
| `sgli-l3-oceanic-sphere-l3-par` | — | `10003019` | GCOM-C/SGLI — Oceanic sphere/L3-PAR |
| `sgli-l3-oceanic-sphere-l3-par-3018` | — | `10003018` | GCOM-C/SGLI — Oceanic sphere/L3-PAR |
| `sgli-l3-oceanic-sphere-l3-sst` | — | `10003027` | GCOM-C/SGLI — Oceanic sphere/L3-SST |
| `sgli-l3-oceanic-sphere-l3-sst-3026` | — | `10003026` | GCOM-C/SGLI — Oceanic sphere/L3-SST |
| `sgli-l3-oceanic-sphere-l3-tsm` | — | `10003023` | GCOM-C/SGLI — Oceanic sphere/L3-TSM |
| `sgli-l3-oceanic-sphere-l3-tsm-3022` | — | `10003022` | GCOM-C/SGLI — Oceanic sphere/L3-TSM |
| `sgli-l3-sgsl` | — | `10003039` | GCOM-C/SGLI — Cryosphere/L3-SGSL |
| `sgli-l3-sgsl-3036` | — | `10003036` | GCOM-C/SGLI — Cryosphere/L3-SGSL |
| `sgli-l3-sgsl-3037` | — | `10003037` | GCOM-C/SGLI — Cryosphere/L3-SGSL |
| `sgli-l3-sgsl-3038` | — | `10003038` | GCOM-C/SGLI — Cryosphere/L3-SGSL |
| `sgli-l3-sice` | — | `10003031` | GCOM-C/SGLI — Cryosphere/L3-SICE |
| `sgli-l3-sice-3028` | — | `10003028` | GCOM-C/SGLI — Cryosphere/L3-SICE |
| `sgli-l3-sice-3029` | — | `10003029` | GCOM-C/SGLI — Cryosphere/L3-SICE |
| `sgli-l3-sice-3030` | — | `10003030` | GCOM-C/SGLI — Cryosphere/L3-SICE |
| `sgli-l3-sist` | — | `10003035` | GCOM-C/SGLI — Cryosphere/L3-SIST |
| `sgli-l3-sist-3032` | — | `10003032` | GCOM-C/SGLI — Cryosphere/L3-SIST |
| `sgli-l3-sist-3033` | — | `10003033` | GCOM-C/SGLI — Cryosphere/L3-SIST |
| `sgli-l3-sist-3034` | — | `10003034` | GCOM-C/SGLI — Cryosphere/L3-SIST |
| `slats-shirop-panchromatic-image` | — | `30000000` | SLATS — Panchromatic Image |
| `trmm-pr-l1-pr-received-power` | — | `18001000` | TRMM — LEVEL1/PR Received Power [1B21] |
| `trmm-pr-l1-pr-reflectivities` | — | `18001001` | TRMM — LEVEL1/PR Reflectivities [1C21] |
| `trmm-pr-l2-pr-qualitative` | — | `18002001` | TRMM — LEVEL2/PR Qualitative [2A23] |
| `trmm-pr-l2-pr-rainfall-profile` | — | `18002002` | TRMM — LEVEL2/PR Rainfall Profile [2A25] |
| `trmm-pr-l2-pr-spectral-latent-heating` | — | `18002003` | TRMM — LEVEL2/PR Spectral Latent Heating [2H25] |
| `trmm-pr-l2-pr-surface-cross-section` | — | `18002000` | TRMM — LEVEL2/PR Surface Cross Section [2A21] |
| `trmm-pr-l3-pr-gridded-orbital-spectral-la` | — | `18003002` | TRMM — LEVEL3/PR Gridded Orbital Spectral Latent Heating [3G25] |
| `trmm-pr-l3-pr-monthly-rainfall` | — | `18003000` | TRMM — LEVEL3/PR Monthly Rainfall [3A25] |
| `trmm-pr-l3-pr-monthly-spectral-latent-hea` | — | `18003003` | TRMM — LEVEL3/PR Monthly Spectral Latent Heating [3H25] |
| `trmm-pr-l3-pr-monthly-surface-rain` | — | `18003001` | TRMM — LEVEL3/PR Monthly Surface Rain [3A26] |
| `trmm-tmi-comb-l2-comb-convective-stratiform-hea` | — | `18032001` | TRMM — LEVEL2/COMB Convective Stratiform Heating [2H31] |
| `trmm-tmi-comb-l2-comb-rain-profile` | — | `18032000` | TRMM — LEVEL2/COMB Rain Profile [2B31] |
| `trmm-tmi-comb-l3-comb-gridded-orbital-convectiv` | — | `18033003` | TRMM — LEVEL3/COMB Gridded Orbital Convective Stratiform Heating [3G31] |
| `trmm-tmi-comb-l3-comb-gridded-orbital-convectiv-3001` | — | `15023001` | TRMM — LEVEL3/COMB Gridded Orbital Convective Stratiform Heating [3G31] |
| `trmm-tmi-comb-l3-comb-monthly-convective-strati` | — | `18033004` | TRMM — LEVEL3/COMB Monthly Convective Stratiform Heating [3H31] |
| `trmm-tmi-comb-l3-comb-monthly-rainfall` | — | `18033000` | TRMM — LEVEL3/COMB Monthly Rainfall [3B31] |
| `trmm-tmi-comb-l3-trmm-and-other-sensors-3-hourl` | — | `18033001` | TRMM — LEVEL3/TRMM and Other Sensors 3-Hourly Rainfall [342] |
| `trmm-tmi-comb-l3-trmm-and-other-sensors-monthly` | — | `18033002` | TRMM — LEVEL3/TRMM and Other Sensors Monthly Rainfall [3B43] |
| `trmm-tmi-l1-tmi-brightness-temperatures` | — | `18011000` | TRMM — LEVEL1/TMI Brightness Temperatures [1B11] |
| `trmm-tmi-l2-tmi-rainfall-profile` | — | `18012000` | TRMM — LEVEL2/TMI Rainfall Profile [2A12] |
| `trmm-tmi-l3-tmi-monthly-emisssion` | — | `18013000` | TRMM — LEVEL3/TMI Monthly Emisssion [3A11] |
| `trmm-tmi-l3-tmi-monthly-rainfall-profile` | — | `18013001` | TRMM — LEVEL3/TMI Monthly Rainfall Profile [3A12] |
| `trmm-virs-l1-virs-radiance` | — | `18021000` | TRMM — LEVEL1/VIRS Radiance [1B01] |
| `trmmgpm-environment-auxiliary-pr-environment-auxiliary` | — | `15042000` | TRMM_GPMFormat — PR Environment Auxiliary |
| `trmmgpm-pr-l1-pr-l1b-received-power` | — | `15001000` | TRMM_GPMFormat — LEVEL1/PR L1B Received Power |
| `trmmgpm-pr-l2-pr-l2-precipitation` | — | `15002000` | TRMM_GPMFormat — LEVEL2/PR L2 Precipitation |
| `trmmgpm-pr-l2-pr-l2-spectral-latent-heating` | — | `15002001` | TRMM_GPMFormat — LEVEL2/PR L2 Spectral Latent Heating |
| `trmmgpm-pr-l3-daily-hdf` | — | `15003000` | TRMM_GPMFormat — LEVEL3/PR L3 Precipitation (Daily HDF) |
| `trmmgpm-pr-l3-daily-text` | — | `15003001` | TRMM_GPMFormat — LEVEL3/PR L3 Precipitation (Daily TEXT) |
| `trmmgpm-pr-l3-monthly-hdf` | — | `15003002` | TRMM_GPMFormat — LEVEL3/PR L3 Precipitation (Monthly HDF) |
| `trmmgpm-pr-l3-pr-l3-gridded-orbital-spectral` | — | `15003003` | TRMM_GPMFormat — LEVEL3/PR L3 Gridded Orbital Spectral Latent Heating |
| `trmmgpm-pr-l3-pr-l3-monthly-spectral-latent` | — | `15003004` | TRMM_GPMFormat — LEVEL3/PR L3 Monthly Spectral Latent Heating |
| `trmmgpm-tmi-comb-l2-comb-l2-precipitation` | — | `15022000` | TRMM_GPMFormat — LEVEL2/COMB L2 Precipitation |
| `trmmgpm-tmi-comb-l3-comb-l3-gridded-orbital-spectr` | — | `15023001` | TRMM_GPMFormat — LEVEL3/COMB L3 Gridded Orbital Spectral Latent Heating |
| `trmmgpm-tmi-comb-l3-comb-l3-monthly-spectral-laten` | — | `15023002` | TRMM_GPMFormat — LEVEL3/COMB L3 Monthly Spectral Latent Heating |
| `trmmgpm-tmi-comb-l3-comb-l3-precipitation` | — | `15023000` | TRMM_GPMFormat — LEVEL3/COMB L3 Precipitation |
| `trmmgpm-tmi-l1-tmi-l1b-brightness-temperature` | — | `15011000` | TRMM_GPMFormat — LEVEL1/TMI L1B Brightness Temperature |
| `trmmgpm-tmi-l2-tmi-l2-precipitation` | — | `15012000` | TRMM_GPMFormat — LEVEL2/TMI L2 Precipitation |
| `trmmgpm-tmi-l3-tmi-l3-precipitation` | — | `15013000` | TRMM_GPMFormat — LEVEL3/TMI L3 Precipitation |
| `trmmgpm-virs-l1-virs-l1b-radiance` | — | `15031000` | TRMM_GPMFormat — LEVEL1/VIRS L1B Radiance |

## Refreshing against the live SDK universes

The CLI command `earthlens datasets refresh jaxa` walks `jaxa.earth.ImageCollectionList()` for the STAC half and `gportal.datasets()` for the G-Portal half, then diffs the live id sets against the bundled catalog so a curator can see drift at a glance:

```bash
earthlens datasets refresh jaxa
earthlens datasets refresh jaxa --write     # rewrite the bundled YAML's available_datasets:
```

The `--write` form rewrites the YAML's `available_datasets:` index without touching the hand-curated `datasets:` rows, so the friendly aliases survive a refresh.
