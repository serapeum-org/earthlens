# GHSL — Introduction

`earthlens.ghsl` wraps the **JRC Global Human Settlement Layer (GHSL)** — the
European Commission Joint Research Centre's open, global, multi-temporal grids
of human presence: where people live, how much is built, how tall it is, and
how settlements are classified. It is the canonical open reference for
population and built-up mapping.

## What it covers

| Product | Measures | Epochs | Resolutions | Categorical |
|---|---|---|---|---|
| `GHS_POP` | residential population per cell | 1975–2030 / 5 | 100 m, 1 km, 3″, 30″ | no |
| `GHS_BUILT_S` (+ `_NRES`) | built-up surface (m²; total / non-residential) | 1975–2030 / 5 (+ 2018 @ 10 m) | 100 m, 1 km, 3″, 30″ (10 m for 2018) | no |
| `GHS_BUILT_V` (+ `_NRES`) | built-up volume (m³) | 1975–2030 / 5 | 100 m, 1 km, 3″, 30″ | no |
| `GHS_BUILT_H_ANBH` / `_AGBH` | building height (net / gross, m) | 2018 | 100 m, 3″ | no |
| `GHS_BUILT_C_MSZ` / `_FUN` | built-up morphological / functional class | 2018 | 10 m | **yes** |
| `GHS_SMOD` | settlement model (Degree of Urbanisation) | 1975–2030 / 5 | 1 km, 30″ | **yes** |
| `GHS_LAND` | land fraction | 2018 (R2022A) | 10 m, 100 m, 1 km | no |
| `GHS_DUC` | Degree-of-Urbanisation classification of admin units | 1975–2030 / 5 | tabular | — |
| `GHS_WUP_*` (R2025A) | population / built-up / DoU **projections to 2100** | 1975–2100 / 5 | 1 km, 30″ | mixed |

The R2025A **GHS-WUP** (World Urbanization Prospects) family adds long-range
projections: `GHS_WUP_POP`, `GHS_WUP_BUILT_S`, `GHS_WUP_DEGURBA` (raster) and
`GHS_WUP_DUC` / `GHS_WUP_MTUC` / `GHS_WUP_COUNTRY_STATS` (tabular).

## How it maps onto `EarthLens`

GHSL is a **download-and-localise raster backend** (`OUTPUT_KIND="raster"`).
Given a bbox + time window + a list of product keys, the backend:

1. resolves the matching epochs against the bundled availability catalog,
2. builds the deterministic JRC `.zip` URL(s) over anonymous HTTPS,
3. downloads the intersecting **Mollweide tiles** (fine resolutions) or the
   single **whole-globe** file (coarse resolutions),
4. uses [**pyramids**](https://github.com/serapeum-org/pyramids) to mosaic,
   reproject (Mollweide → your CRS) and crop to the AOI,

writing **one GeoTIFF per `(product, epoch)`**. Multi-epoch requests support
`aggregate=` (reduced across epochs). The unified facade key is `"ghsl"`
(aliases `"ghs"`, `"ghsl:human-settlement"`).

## Install

The default deterministic-HTTPS path needs only the core dependencies
(`requests` + `pyramids-gis`), so no extra is required:

```bash
pip install earthlens
```

`pip install earthlens[ghsl]` is accepted but currently pulls nothing extra —
GHSL is open data and the GIS work runs locally in `pyramids`.

## Licence & attribution

GHSL is **open and free, attribution only** ("reuse is authorised provided the
source is acknowledged"). No account, key, or login is needed. Downstream
products must carry the attribution string
(`earthlens.ghsl.GHSL_ATTRIBUTION`):

> Source: European Commission, Joint Research Centre (JRC) — Global Human
> Settlement Layer (GHSL).

See [Usage](usage.md) for the request shape, [Products](products.md) for the
full reference + categorical legends, and [Projections & tiling](projections.md)
for the Mollweide / WGS84 / tile-grid details.
