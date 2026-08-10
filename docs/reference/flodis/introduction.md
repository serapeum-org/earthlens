# FLODIS observed flood footprints ↔ impacts — introduction

[FLODIS](https://doi.org/10.1038/s41597-023-02376-9) (Mester, Frieler & Schewe, Potsdam Institute for Climate
Impact Research; *Scientific Data* **10**, 482, 2023) is the **observed hazard-footprint → impact bridge**: it links
recorded flood impacts — human displacements, fatalities and economic damages — to the **satellite flood footprints**
that caused them. It is the global companion to the European [`hanze`](../hanze/introduction.md) backend, and the
matched-and-enriched sibling of the raw impact tables in [`emdat`](../emdat/introduction.md).

earthlens ships a single `flodis` backend. This page orients it. For the hands-on walkthrough see
[Usage](usage.md); the rendered API is the [Reference](flodis.md) page.

## What FLODIS links

FLODIS matches three impact sources to one footprint source, in space and time:

- **impacts** — EM-DAT fatalities and economic damages (CRED/UCLouvain), and IDMC human displacements.
- **footprints** — the [Global Flood Database](https://global-flood-database.cloudtostreet.ai/) (GFD; Tellman et
  al. 2021), a satellite-observed inventory of historic flood extents.
- **exposure** — for each matched event, the affected population (GHSL + GPW), GDP, and critical-infrastructure
  counts (power, health, transport, telecommunication, water) intersected with the footprint.

## What you get

`flodis` is a **tabular** backend: `download()` returns a `pandas.DataFrame`, one row per matched event, carrying
FLODIS's documented columns. `dataset=` selects which of the two tables you fetch:

- **`dataset="damages"`** (the default) → the EM-DAT deaths/damages table (697 rows), keyed on the EM-DAT
  **`disasterno`**. Columns include `total_deaths`, `no_affected_EMDAT`, `total_damages_(000_USD)`, the GFD-match
  block (`GFD_matches`, `GFD_duration`, `matching_type`), and the per-event exposure sums
  (`pop_affected_sum_GHSL/GPW`, `GDP_affected_sum`, the infrastructure counts).
- **`dataset="displacement"`** → the IDMC displacement table (335 rows), keyed on the GADM **`GID_1`** / **`GID_2`**
  admin codes. Columns include `displacements`, `num_provinces` / `num_districts`, and the same GFD-match and
  exposure blocks.

## Know these three things before you use it

**1. It is a pinned Zenodo release, and it is not current.** earthlens pins the exact Zenodo record
[`8123096`](https://doi.org/10.5281/zenodo.8123096) (CC-BY-4.0) rather than a moving GitHub branch, so a request is
reproducible; the files are byte-identical to the frozen upstream repository. FLODIS covers **2000–2018** — the
overlap window of the three input archives — so it is a historical record, not a live feed. Both CSVs are small
(≈395 KB and ≈185 KB); earthlens downloads them directly and caches them under your output directory.

**2. earthlens fetches the impact tables, not the footprints.** FLODIS carries the *keys* to the footprints, not the
geometry. earthlens does **not** re-implement GDIS or the Global Flood Database — it exposes the join keys so you can
attach the footprints from the layers earthlens already ships: the GDIS disaster geometry from the
[`emdat`](../emdat/introduction.md) backend (joined on `disasterno`) and the GFD flood extents from the
[`gee`](../google-earth-engine/introduction.md) backend
(`GLOBAL_FLOOD_DB/MODIS_EVENTS/V1`). [Usage](usage.md) shows the join.

**3. The two tables are keyed differently.** `damages` is keyed on the EM-DAT `disasterno`; `displacement` is keyed
on the GADM `GID_1` / `GID_2`. Filter both by `country=` (ISO3, e.g. `"MOZ"`) and a `[start, end]` year window;
filter the displacement table additionally by `gid=` (a GADM code). The tables carry no per-row coordinates — a
bounding box is not a filter axis here, because the geometry lives in the `emdat` / `gee` layers you join to.

## Licence & attribution

FLODIS is **CC-BY-4.0** (Zenodo record `8123096`). Cite Mester, Frieler & Schewe (2023) and credit the upstream
sources — IDMC, EM-DAT (CRED/UCLouvain) and the Global Flood Database (Tellman et al. 2021) — in any derived work.
The backend logs the citation on each `download()`.
