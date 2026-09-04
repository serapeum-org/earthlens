# JAXA — introduction

<img src="../../_images/logos/jaxa.svg" alt="JAXA logo" height="60">

JAXA's Earth-observation archive is reached through several portals — the
authless **JAXA Earth API** (a STAC + COG view of ~118 ARCO-style
collections), the credentialed **G-Portal** mission archive (raw L1 / L2
swaths over SFTP), and the dedicated **P-Tree** Himawari FTP.

The `earthlens` JAXA backend wraps **all three** as a single backend with
a `protocol:` discriminator on each catalog row, so one `EarthLens("jaxa",
…)` call writes a north-up GeoTIFF for an AW3D30 elevation tile, downloads
a PALSAR-2 L1.1 product over SFTP, or pulls a Himawari AHI HSD 10-minute
observation over FTP — depending on which dataset key you ask for.

For the hands-on download walkthrough see [Usage](usage.md); the rendered
API is the [Reference](jaxa.md) page.

## What earthlens returns

`OUTPUT_KIND="raster"`. `download()` returns the list of written paths:

* **`protocol: jaxa-earth`** — one GeoTIFF per band per requested
  dataset, written via `pyramids.dataset.Dataset.from_array(...)`
  → `.to_file(...)`. Filenames are `<dataset key>_<band>.tif`. EPSG:4326,
  north-up.
* **`protocol: gportal`** — the raw G-Portal product(s) that match the
  request, fetched over SFTP into the output directory. File extensions
  are mission-dependent (HDF5 for SGLI/AMSR2, GeoTIFF for some L3, …).
* **`protocol: ptree`** — raw Himawari-9 AHI HSD `.DAT.bz2` granules
  from JAXA P-Tree over plain FTP. Ten segments per band per 10-minute
  observation slot are downloaded and mirrored under
  `out_dir/YYYYMM/DD/HH/`. Decode is deliberately out of scope
  (`satpy` — tracked as pyramids `PY-2`).

## The three protocols at a glance

| Aspect | `jaxa-earth` | `gportal` | `ptree` |
|---|---|---|---|
| Authentication | none | free G-Portal account | free P-Tree account (separate from G-Portal) |
| SDK | `jaxa.earth >=0.1.6,<0.2` | `gportal >=0.4,<0.5` | stdlib `ftplib` — no extra dep |
| Protocol | HTTPS (STAC + COG) | SFTP (`ftp.gportal.jaxa.jp:2051`) | FTP (`ftp.ptree.jaxa.jp:21`) |
| Catalog size | 118 STAC collections | 799 numeric dataset IDs | 1 curated product (Himawari HSD FLDK) |
| Output | per-band GeoTIFFs (north-up) | raw product files | raw HSD `.DAT.bz2` segments (10 per band per 10-min slot) |
| Cadence / window | catalog-driven | catalog-driven | 10-min, last 30 days only |
| `aggregate=` | not supported (deferred) | not supported | not supported |

Mixing keys from more than one protocol in **one** `JAXA(...)` call is
rejected — the file shapes and concurrency profiles differ; issue one
call per protocol.

## Catalog highlights

The bundled catalog ships **918 rows** — every one of the 118 live
`jaxa.earth` collections, the entire 799-product G-Portal universe,
and the one curated P-Tree Himawari HSD product — sharded into
per-mission YAML files under `src/earthlens/jaxa/catalog/`
(`jaxa-earth.yaml`, `sgli.yaml`, `amsr.yaml`, `alos-palsar.yaml`,
`earthcare.yaml`, `precipitation.yaml`, `himawari.yaml`, …), matching
the layout used by the `gee` and `ecmwf` siblings. **Every collection
has a short, friendly canonical key** (the long auto-derived slug
stays as an alias).

The naming scheme is `<mission>-<product>[-<d|n>][-<cadence>][-norm]`,
where `d` / `n` mark daytime / nighttime variants, the cadence comes
from the source (`daily`, `halfmonth`, `monthly`, `8day`, `hourly`,
`yearly`), and `-norm` flags a climatological *normal*. The 14 mission
families: `aw3d30`, `alos2`, `amsr2`, `amsre`, `gsmap`, `hrlulc`,
`mod11`, `mod11c3`, `modis`, `myd11`, `proba`, `sgli`, `spi`, `temsm`.

Headline keys (the long auto-slugs and a few English aliases also
resolve via `cat.get(...)`):

| Key | Protocol | Mission / product |
|---|---|---|
| `aw3d30` (aka `elevation`, `dem`) | `jaxa-earth` | ALOS PRISM AW3D30 v3.2 — 30 m global DSM |
| `aw3d30-v41` (aka `elevation-v41`) | `jaxa-earth` | ALOS PRISM AW3D30 v4.1 |
| `alos2-fnf` (aka `fnf`) | `jaxa-earth` | ALOS-2 PALSAR-2 yearly forest / non-forest |
| `gsmap` (aka `precipitation`) | `jaxa-earth` | GSMaP standard hourly precipitation v6 |
| `gsmap-nrt` / `gsmap-daily` / `gsmap-monthly` | `jaxa-earth` | GSMaP near-real-time + climatological variants |
| `spi` | `jaxa-earth` | GSMaP-derived monthly SPI drought index |
| `hrlulc` (aka `lulc`) | `jaxa-earth` | High-resolution Japan land use / land cover |
| `proba-v-lccs` (aka `lccs`, `landcover`) | `jaxa-earth` | Copernicus C3S PROBA-V land cover |
| `amsr2-smc-d-daily` (and 9 variants) | `jaxa-earth` | GCOM-W AMSR2 L3 soil moisture (day/night × daily / halfmonth / monthly × standard / normal) |
| `sgli-lst-d-daily` / `sgli-ndvi-d-daily` / `sgli-chla-d-daily` / `sgli-arot-d-daily` | `jaxa-earth` | GCOM-C SGLI L3 family (LST, NDVI, chlorophyll, aerosol) |
| `modis-ndvi-monthly` / `modis-aqua-swr-daily` / `modis-terra-swr-daily` | `jaxa-earth` | JASMES MODIS family (NDVI, surface shortwave radiation) |
| `mod11-lst-d-daily` / `myd11-lst-d-daily` | `jaxa-earth` | NASA EOSDIS Terra/Aqua MODIS C1 LST re-hosts |
| `temsm-outflw` / `temsm-flddph` / `temsm-fldfrc` (×7) | `jaxa-earth` | UTokyo TE-MSM Japan flood / river forecasts |
| `sgli-l3-nwlr` (aka `sgli-l380`) | `gportal` | GCOM-C/SGLI L3 Normalized Water-Leaving Radiance — id `10003001` |
| `amsr2-l1r` | `gportal` | GCOM-W/AMSR2 L1R resampled brightness temperatures — id `11001002` |
| `alos2-palsar2-uf-sp` (aka `palsar2`) | `gportal` | ALOS-2/PALSAR-2 L1.1 ultra-fine 3 m single-pol — id `27004001` |
| `earthcare-cpr-eco` (aka `earthcare`) | `gportal` | EarthCARE/CPR L2 Echo Product — id `16002000` |
| `gpm-dpr-kupr-l1b` (aka `gpm`) | `gportal` | GPM/DPR KuPR L1B received power — id `12001000` |
| `gosat-gw-amsr3-l1b` | `gportal` | GOSAT-GW/AMSR3 L1B brightness temperatures (TBB) — id `31001001` |

For the full list of friendly keys (104 of 118 are ≤25 characters) see
the per-mission shards under `src/earthlens/jaxa/catalog/`. Use
`Catalog().by_protocol("jaxa-earth")`, `.by_protocol("gportal")`, or
`.by_protocol("ptree")` to list them programmatically.

**Two intentional exclusions:**

- **Legacy GOSAT** (CO2 / CH4 from the original GOSAT). It lives on the
  separate GOSAT Data Archive Service, not on G-Portal. The next-gen
  **GOSAT-GW** (carrying AMSR3) **is** on G-Portal and **is** in the
  catalog.
- **ALOS-4 / PALSAR-3.** Launched 2024-07; commercial distribution
  (Synspective / PASCO). Non-commercial G-Portal access is not confirmed
  as of 2026-06.

## Install

```bash
pip install 'earthlens[jaxa]'
```

This pulls both SDKs: the official `jaxa.earth` for the COG path and the
community `gportal` for the SFTP path. Both are optional — importing
`earthlens.jaxa` works without the extra, but the branches fail with a
friendly `ImportError` pointing at `earthlens[jaxa]` when invoked. The
`ptree` branch needs **no extra**: it uses stdlib `ftplib` only, so a
plain `pip install earthlens` is enough to reach Himawari HSD.

Do **not** install `gportal[gcomc]` — that extra pins `numpy<2`, which
conflicts with this repo's numpy 2.x line. The base `gportal` is enough;
HDF5 readers for SGLI products are downstream of earthlens.

## Authentication

- `jaxa-earth` — **none**.
- `gportal` — a free G-Portal account from
  <https://gportal.jaxa.jp/gpr/user/regist1>. Set the credentials either
  by env var:

    ```bash
    export GPORTAL_USERNAME="your_username"
    export GPORTAL_PASSWORD="your_password"
    ```

  or as kwargs to `EarthLens(...)`:

    ```python
    EarthLens(
        data_source="jaxa",
        variables=["alos2-palsar2-l1-1"],
        gportal_username="...",
        gportal_password="...",
        ...,
    )
    ```
- `ptree` — a **separate** free P-Tree account from
  <https://www.eorc.jaxa.jp/ptree/registration_top.html> (never reuse the
  G-Portal credentials). Env vars mirror the G-Portal pattern:

    ```bash
    export JAXA_PTREE_USERNAME="your_registered_email"
    export JAXA_PTREE_PASSWORD="your_password"
    ```

  or as kwargs to `EarthLens(...)`:

    ```python
    EarthLens(
        data_source="jaxa",
        variables=["himawari-ahi-fldk"],
        bands=["B13"],
        ptree_username="...",
        ptree_password="...",
        ...,
    )
    ```

For `gportal`, the SDK does **not** auto-read either env var — earthlens
reads them inside `JaxaAuth.configure()` and threads them straight to
`gportal.download(username=, password=)` as call-site kwargs, so the
SDK's module-level credential globals stay untouched between requests.
Search is anonymous; only the actual SFTP `download` step uses the
credentials. The `ptree` branch resolves its pair the same way and
passes them straight to `ftplib.FTP.login(...)`.

## Things to know up front

- **Strict chain order.** Internally the `jaxa-earth` branch calls
  `filter_date → filter_resolution → filter_bounds → select → get_images`
  on `ImageCollection` — out-of-order calls raise from the SDK. You don't
  see this from the facade, but it explains why the request shape is
  fixed.
- **`resolution=` is pixels-per-degree.** The `jaxa-earth` branch maps
  the `resolution` kwarg to the API's `ppu` (e.g. `1000` ≈ ~110 m at the
  equator). The SDK snaps to its native resolution if the request is
  too fine.
- **Tensors are 4-D.** The API always returns
  `Raster.img` as `(time, lat, lon, band)`, even for single-date /
  single-band collections. The backend slices to `(lat, lon)` and writes
  one COG per band; multi-time stacks are a follow-on.
- **G-Portal `download` is sequential.** The upstream SDK has no
  parallelism. Bulk SGLI / PALSAR-2 pulls are slow.
- **No `aggregate=`** — the per-date reducer is deferred (planning G6).
- **P-Tree (Himawari) window is a rolling 30 days** — requests older
  than that raise `earthlens.jaxa.RetentionError` before any FTP call
  (the server itself returns an opaque `450 No such file or directory`
  past that horizon). Decoding HSD `.DAT.bz2` to arrays is deliberately
  out of scope: that's `satpy`, tracked as `pyramids PY-2`.
