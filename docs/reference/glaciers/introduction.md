# Glaciers — introduction

<img src="../../_images/logos/glaciers.png" alt="NSIDC Randolph Glacier Inventory logo" height="60">

earthlens ships a single `glaciers` backend that fetches **glacier outlines and
fluctuations** from three open sources and returns them either as a pyramids
`FeatureCollection` (outlines) or a tidy `pandas.DataFrame` (fluctuations). The
output shape is **per instance** — it follows the dataset you pick — and
`aggregate=` is rejected for both shapes (these are pre-computed inventories /
measurements, not gridded fields).

Three sources are covered:

- **[RGI 7.0](https://nsidc.org/data/nsidc-0770/versions/7)** (Randolph Glacier
  Inventory) — global glacier **outlines**, one shapefile per GTN-G first-order
  region (regions 01–19). Served openly — no login — by the
  **[UNESCO IHP-WINS](https://ihp-wins.unesco.org/dataset/randolph-glacier-inventory-rgi-7-0-glacier-product)**
  mirror (the NSIDC host is Earthdata-gated, and the backend never touches it).
  Vector.
- **[GLIMS](https://www.glims.org/)** (Global Land Ice Measurements from Space) —
  **time-series** glacier outlines (a glacier can have several outlines with
  different acquisition dates), queried by bounding box through the open GLIMS
  GeoServer **WFS**. Vector.
- **[WGMS](https://wgms.ch/)** — the **Fluctuations of Glaciers** (FoG) database:
  mass balance, front variation (length change), and glacier state, as tabular
  CSV tables. Open download. Tabular.

This page orients the backend. For the hands-on walkthrough see [Usage](usage.md)
and the shipped dataset ids on the [Available datasets](datasets.md) page; the
rendered API is the [Reference](glaciers.md) page.

## Two design points

**Per-instance `OUTPUT_KIND`.** RGI and GLIMS outlines are `vector`; WGMS
fluctuations are `tabular`. The resolved dataset's `output_kind` is copied onto
the backend's `OUTPUT_KIND` at construction, so `download()` returns a
`FeatureCollection` for `rgi:outlines` / `glims:outlines` and a `DataFrame` for
the `wgms:*` datasets. The `EarthLens` facade reads it to know the return shape
and to reject `aggregate=`.

**Region-download-then-clip (RGI).** RGI ships per GTN-G region (each region is a
multi-MB shapefile zip), so the backend maps your request bounding box to the
overlapping region(s), downloads and caches each region zip once, reads the
shapefile inside it via pyramids `FeatureCollection.read_file`, reprojects to
EPSG:4326 if needed, and clips to the bbox. A `region=` override (a GTN-G region
id such as `"11"`) skips the bbox→region step.

## Authentication

**None.** All three sources are open — IHP-WINS (RGI), the GLIMS WFS, and the
WGMS FoG download — so the backend ships no auth module and needs no
credentials.

## Licenses / citations

On a successful download the resolved dataset's citation is logged once:

- **RGI 7.0** — RGI 7.0 Consortium (2023), DOI `10.5067/F6JMOVY5NAVZ`, CC-BY-4.0.
- **GLIMS** — GLIMS and NSIDC (`nsidc-0272`), CC-BY-4.0; cite the contributing
  analysts.
- **WGMS** — WGMS (2026), Fluctuations of Glaciers Database, DOI
  `10.5904/wgms-fog-2026-02-10`.
