# MSWEP / MSWX — introduction

`earthlens.mswep` downloads raw NetCDF granules of **MSWEP** and **MSWX** from
[GloH2O](https://www.gloh2o.org/) via the Google-Drive folder shared with an approved non-commercial user.

- **MSWEP** — Multi-Source Weighted-Ensemble Precipitation. Machine-learning merged gauge, satellite and
  reanalysis precipitation at **0.1°**, hourly, **1979 → ~2 hours from real time**.
- **MSWX** — Multi-Source Weather, the companion bias-corrected meteorological forcing: **10 variables** at
  0.1° / 3-hourly. Its forecast streams extend MSWEP forward.

`OUTPUT_KIND` is `raster` and `download()` returns the `list[Path]` of granules written. earthlens **does not
decode** them — reading and regridding NetCDF is [pyramids](https://github.com/serapeum-org/pyramids)' job, and
no module under `earthlens.mswep` imports `xarray`.

## Access is a prerequisite

There is no anonymous download. You must have your **own** approved GloH2O access before this backend can do
anything — see [Authentication](authentication.md), which is the page to read first. GloH2O link-shares its
folders, so any Google credential works — a service account, or your existing `gcloud` login.

## Licence

**CC BY-NC 4.0** — non-commercial only, attribution required. Every request emits a `LicenseWarning` carrying
the citation:

> Wang, X., Alharbi, R. S., Baez-Villanueva, O. M., Miralles, D. G., Ma, J., Xu, S., McCabe, M. F.,
> Pappenberger, F., van Dijk, A. I. J. M., McVicar, T. R., Karthikeyan, L., Fowler, H. J., Pan, M.,
> Gebrechorkos, S. H., & Beck, H. E. (2026). *MSWEP V3: Machine learning-powered global precipitation estimates
> at 0.1° hourly resolution (1979–present)*. arXiv. <https://doi.org/10.48550/arXiv.2602.01436>

## Known data caveat — V3.15 / V3.16 trends

GloH2O reports that IMERG and GSMaP show a systematic precipitation increase from 2015, so **MSWEP V3.15/V3.16
reads artificially low over 2000–2015** relative to the surrounding periods. It affects both `Past` and
`Past_nogauge`.

> "For users who require reliable trend estimates, we recommend using **V2.80** for 1979–2021 until the issue
> is resolved." — MSWEP V3.16 Documentation

GloH2O shares a separate folder per version, so pass the `folder_id` for the version you want (`version=` then selects its catalog metadata):

```python
EarthLens("mswep", version="2.80", ...)   # the trend-safe archive
```

## Use rclone for bulk

An hourly year is ~8760 granules. The Drive API is not built for that, and GloH2O asks non-commercial users to
transfer via `rclone`. This backend is for targeted product / variant / resolution / window requests; it warns
when a request exceeds the catalog's granule threshold.

```bash
rclone sync -v --drive-shared-with-me GoogleDrive:/MSWEP_V316_test/Past/Daily ./mswep
```
