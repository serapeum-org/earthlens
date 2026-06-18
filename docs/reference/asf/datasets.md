# ASF — curated products

The `asf` catalog ships **42 curated product rows** covering every
stackable product class in `asf_search`, the legacy archives ASF
mirrors, the full NISAR product family (anticipatory — image
products land post-launch), the OPERA-S1-CALVAL calibration
companion, and the TROPO atmospheric corrections used by
downstream InSAR processors. Each row maps a friendly product key
to its raw `asf_search` enum values (`PLATFORM` or `DATASET`,
plus `PRODUCT_TYPE`) and flags whether `ASFProduct.stack()` is
meaningful for it.

Inspect the catalog at runtime:

```python
from earthlens.asf import Catalog

cat = Catalog()
cat.available_products            # sorted list of every curated key
cat.stackable_products()          # only the stackable subset
row = cat.get_product("sentinel-1-slc")
row.platform, row.product_type, row.stackable
```

## Stackable products (InSAR-ready)

These products are the input to a baseline `stack()` call. Use
them in stack mode with `reference=<granule id>`.

| Key | Aliases | SDK selector | Description |
|---|---|---|---|
| `sentinel-1-slc` | `s1-slc`, `sentinel1-slc` | `PLATFORM.SENTINEL1` + `SLC` | Sentinel-1 Single Look Complex — the standard InSAR input |
| `sentinel-1a-slc` | `s1a-slc` | `PLATFORM.SENTINEL1A` + `SLC` | Same, restricted to the S1A satellite |
| `sentinel-1b-slc` | `s1b-slc` | `PLATFORM.SENTINEL1B` + `SLC` | Same, S1B (archive only, decommissioned 2022) |
| `sentinel-1c-slc` | `s1c-slc` | `PLATFORM.SENTINEL1C` + `SLC` | Same, S1C (replacement for S1B, 2024+) |
| `sentinel-1d-slc` | `s1d-slc` | `PLATFORM.SENTINEL1D` + `SLC` | Same, planned S1D |
| `sentinel-1-burst` | `s1-burst` | `DATASET.SLC_BURST` + `BURST` | Per-burst InSAR-ready subset of an SLC |
| `alos-palsar-slc` | `alos-slc`, `palsar-slc`, `alos-l1.1` | `PLATFORM.ALOS` + `L1.1` | JAXA L-band PALSAR Level-1.1 SLC (2006-2011) |
| `alos-2-l11` | `alos2-l11`, `palsar2-l11` | `DATASET.ALOS_2` + `L1.1` | ALOS-2 PALSAR-2 Level-1.1 SLC (2014+, restricted) |
| `opera-cslc-s1` | `opera-cslc`, `opera-s1-cslc` | `DATASET.OPERA_S1` + `CSLC` | OPERA Coregistered Single Look Complex |
| `opera-cslc-calval` | `opera-s1-cslc-calval` | `DATASET.OPERA_S1_CALVAL` + `CSLC` | OPERA-S1 CSLC calibration / validation (no live products yet) |
| `aria-s1-gunw` | `aria-gunw`, `gunw` | `DATASET.ARIA_S1_GUNW` + `GUNW_STD` | ARIA precomputed unwrapped interferogram (stackable but search-only in practice) |
| `nisar-rslc` | `nisar-slc` | `PLATFORM.NISAR` + `RSLC` | NISAR Range-Doppler SLC (Level-1 InSAR-ready, post-launch) |
| `nisar-gslc` | `nisar-geocoded-slc` | `PLATFORM.NISAR` + `GSLC` | NISAR Geocoded SLC (Level-2, post-launch) |
| `nisar-gcov` | `nisar-covariance` | `PLATFORM.NISAR` + `GCOV` | NISAR Geocoded Covariance (Level-2 polarimetric, post-launch) |
| `ers-1-slc` | `ers1-slc` | `PLATFORM.ERS1` + `L1` | ERS-1 Level-1 SLC (1991-2000) — legacy C-band for long-baseline InSAR |
| `ers-2-slc` | `ers2-slc` | `PLATFORM.ERS2` + `L1` | ERS-2 Level-1 SLC (1995-2011) |
| `jers-1-l0` | `jers1-l0` | `PLATFORM.JERS` + `L0` | JERS-1 Level-0 raw downlink (L-band, 1992-1998) |
| `radarsat-1-l0` | `radarsat1-l0` | `PLATFORM.RADARSAT` + `L0` | RADARSAT-1 Level-0 raw downlink (1995-2013) |
| `radarsat-1-l1` | `radarsat1-l1` | `PLATFORM.RADARSAT` + `L1` | RADARSAT-1 Level-1 single-look |

## Search-only products

Either processed-derivative outputs (RTC, GUNW) where there is no
baseline-comparable acquisition, or analysis-ready products (GRD,
OCN) where the InSAR phase has been collapsed. Use these in
search mode (without `reference=`).

| Key | Aliases | SDK selector | Description |
|---|---|---|---|
| `sentinel-1-grd` | `s1-grd`, `s1-grd-hd` | `PLATFORM.SENTINEL1` + `GRD_HD` | Sentinel-1 Ground Range Detected, high-res dual-pol |
| `sentinel-1-grd-md` | `s1-grd-md` | `PLATFORM.SENTINEL1` + `GRD_MD` | Sentinel-1 GRD medium-resolution |
| `sentinel-1-ocn` | `s1-ocn` | `PLATFORM.SENTINEL1` + `OCN` | Sentinel-1 Ocean Level-2 (wind / wave / Doppler) |
| `sentinel-1-raw` | `s1-raw` | `PLATFORM.SENTINEL1` + `RAW` | Sentinel-1 raw (Level-0) downlinks |
| `opera-rtc-s1` | `opera-rtc`, `opera-s1-rtc` | `DATASET.OPERA_S1` + `RTC` | OPERA Radiometric Terrain Corrected |
| `opera-rtc-s1-static` | `opera-rtc-static` | `DATASET.OPERA_S1` + `RTC_STATIC` | OPERA RTC static-layer mask |
| `opera-cslc-s1-static` | `opera-cslc-static` | `DATASET.OPERA_S1` + `CSLC_STATIC` | OPERA CSLC static-layer mask |
| `opera-dist-alert-s1` | `opera-dist-s1`, `dist-alert-s1` | `DATASET.OPERA_S1` + `DIST_ALERT_S1` | OPERA land-surface disturbance alert |
| `opera-rtc-calval` | `opera-s1-rtc-calval` | `DATASET.OPERA_S1_CALVAL` + `RTC` | OPERA-S1 RTC calibration / validation (no live products yet) |
| `nisar-l0b` | `nisar-raw` | `PLATFORM.NISAR` + `L0B` | NISAR Level-0B raw downlink (post-launch) |
| `nisar-rifg` | `nisar-interferogram` | `PLATFORM.NISAR` + `RIFG` | NISAR Range-Doppler Interferogram (Level-2 InSAR output) |
| `nisar-runw` | `nisar-unwrapped` | `PLATFORM.NISAR` + `RUNW` | NISAR Range-Doppler Unwrapped (Level-2 InSAR output) |
| `nisar-gunw` | `nisar-geocoded-unwrapped` | `PLATFORM.NISAR` + `GUNW` | NISAR Geocoded Unwrapped Interferogram (Level-3 InSAR output) |
| `nisar-roff` | `nisar-offsets` | `PLATFORM.NISAR` + `ROFF` | NISAR Range-Doppler Pixel Offsets (Level-2 InSAR output) |
| `nisar-goff` | `nisar-geocoded-offsets` | `PLATFORM.NISAR` + `GOFF` | NISAR Geocoded Pixel Offsets (Level-3 InSAR output) |
| `nisar-lrclk-utc` | `nisar-clock` | `PLATFORM.NISAR` + `LRCLK_UTC` | NISAR L-band radar clock-correction files (the live NISAR data so far) |
| `tropo-ecmwf` | `ecmwf-tropo` | `DATASET.TROPO` + `ECMWF_TROPO` | ECMWF-derived tropospheric phase-delay correction (atmospheric) |
| `tropo-zenith` | `zenith-tropo` | `DATASET.TROPO` + `TROPO_ZENITH` | Tropospheric zenith-total-delay correction (atmospheric) |
| `seasat-l1` | `seasat` | `PLATFORM.SEASAT` + `L1` | SEASAT-1 reprocessed L1 (1978 — the original spaceborne SAR) |
| `sir-c-slc` | `sirc-slc`, `sir-c` | `PLATFORM.SIRC` + `SLC` | SIR-C / X-SAR Shuttle SAR campaigns (1994) |
| `airsar-l1` | `airsar` | `PLATFORM.AIRSAR` + `L1` | AIRSAR airborne SAR (NASA / JPL, 1988-2004) |
| `uavsar-amp` | `uavsar` | `PLATFORM.UAVSAR` + `AMPLITUDE` | UAVSAR airborne L-band amplitude |
| `smap-l1` | `smap` | `PLATFORM.SMAP` + `L1A_RADAR_HDF5` | SMAP Level-1A radar HDF5 (archive only) |

## What "stackable" actually means

`asf_search` decides at the **product-class level** whether a
product can be stacked: every `asf_search.ASFStackableProduct`
subclass overrides `get_default_baseline_product_type()` to return
the product type that pairs with it. For ASF's catalogue:

- `S1Product` → `'SLC'` (Sentinel-1 SLC stacks against SLCs)
- `S1BurstProduct` → `'BURST'`
- `ALOSProduct` → `'L1.1'`
- `OPERAS1Product` → `None` for RTC/DIST-ALERT; `'CSLC'` for the CSLC
- `RADARSATProduct` → `'L0'`
- `ERSProduct` / `JERSProduct` → `'L0'` / `'L1'`
- `NISARProduct` → not yet pinned upstream

When you pass a non-stackable product key with `reference=`, the
backend raises a `ValueError` at construction — no need to round-trip
to ASF only to discover the stack is empty.

## Picking a product

| You want | Use | Notes |
|---|---|---|
| C-band InSAR over land, 2014+ | `sentinel-1-slc` | The default InSAR input |
| Sub-AOI processing | `sentinel-1-burst` | Smaller download per burst |
| L-band InSAR (forest / agriculture / wetlands) | `alos-palsar-slc` | Mission ended 2011; ALOS-2 if you have access |
| Pre-Sentinel baseline (1990s, 2000s) | `ers-1-slc` / `ers-2-slc` / `radarsat-1-l1` | Useful for multi-decadal change detection |
| Analysis-ready backscatter (no phase) | `opera-rtc-s1` or `sentinel-1-grd` | No baseline; pure amplitude work |
| Precomputed interferogram | `aria-s1-gunw` | ARIA does the InSAR for you |
| Disturbance flag | `opera-dist-alert-s1` | Land-surface change alert (low-latency) |
| L+S-band data, post-launch | `nisar-rslc` | Empty until NISAR delivers data |

Pass any alias and the catalog resolves it to the canonical key
with a did-you-mean hint on a typo:

```python
>>> Catalog().resolve("s1-slc")
'sentinel-1-slc'
>>> Catalog().resolve("sentinel1-slx")
Traceback (most recent call last):
    ...
ValueError: 'sentinel1-slx' is not in the ASF product catalog. ...
```

## Refreshing the catalog

The curated set is **hand-maintained** — `asf_search`'s enum
constants change rarely (every couple of years when a new
satellite launches), so a live refresher would not pay for itself.
The CLI's `refresh` verb reports `unsupported`:

```bash
earthlens datasets refresh asf
# unsupported: no public live-listing endpoint wired up
```

What **is** wired up is a sanity-check validator that confirms every
curated row's PLATFORM / DATASET / PRODUCT_TYPE member name still
exists on the installed `asf_search`:

```bash
earthlens datasets validate asf
# ok: 42 row(s) checked, 0 issue(s)
```

That covers the failure mode the SDK actually exhibits — a
breaking rename of a constant. Add a row by hand-editing
`src/earthlens/asf/asf_data_catalog.yaml`; the loader's invariant
check enforces alias uniqueness and that exactly one of
`platform` / `dataset` is set per row, and the validator confirms
the constants resolve on the installed SDK.
