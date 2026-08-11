# FLOPROS flood-protection standards — introduction

[FLOPROS](https://doi.org/10.5194/nhess-16-1049-2016) (FLOod PROtection Standards)
is a global database of the flood-protection standards in force across the
world, expressed as **return periods in years** — e.g. "this region is defended
against the 100-year flood". earthlens ships a single `flopros` backend that
reads the FLOPROS shapefile directly from the NHESS-2016 paper's public
supplement (`nhess.copernicus.org`, CC-BY-3.0, no credentials) and returns the
~4650 subnational polygons carrying the protection standards.

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md); the rendered API is the [Reference](flopros.md) page.

## Why it matters here

FLOPROS is the **defended-vs-undefended correction**. A raw flood-hazard map (a
depth or extent layer, e.g. from `aqueduct` or `gee`) tells you where water
*could* go; it does not know about dikes, levees, and flood-control policy. Clip
or threshold that hazard map by the local FLOPROS standard and you separate the
areas that are actually *protected* to a given return period from those that are
*exposed*. That is the step between a hazard map and a defended-risk estimate.

Like the GDACS, Aqueduct, and admin-boundaries backends, FLOPROS departs from
the gridded backends (CHC rainfall, ERA5, GEE imagery) in two ways:

- **The output is a vector table, not a grid.** A query returns the subnational
  polygons carrying protection-standard columns, so the facade rejects an
  `aggregate=` argument.
- **There is no time axis.** FLOPROS is a static snapshot, so `start` / `end`
  are accepted but ignored.

## The FLOPROS layers

Each polygon carries the protection standard under several **layers**, exposed
under friendly names (the source `.dbf` column is in brackets):

- `modelled_riverine` (`ModL_Riv`) — the modelled riverine standard.
- `merged_riverine` (`MerL_Riv`) — the **Merged** layer: the recommended
  combined riverine standard (empirical where known, modelled elsewhere).
- `design_min_riverine` / `design_max_riverine` (`DL_Min_Riv` / `DL_Max_Riv`) —
  the Design-layer range (design standards of actual defences).
- `policy_min_riverine` / `policy_max_riverine` (`PL_Min_Riv` / `PL_Max_Riv`) —
  the Policy-layer range (protection targets set by policy).
- The four `*_coastal` layers (`DL_*_Co` / `PL_*_Co`) — the coastal equivalents
  of the Design and Policy layers.

A value of `0` means no standard is recorded for that layer in that unit. For a
single representative number, use `merged_riverine`.

## Licence

FLOPROS is CC-BY-3.0. Cite Scussolini et al. (2016), *Nat. Hazards Earth Syst.
Sci.*, 16, 1049–1061 (doi:10.5194/nhess-16-1049-2016). The backend logs the
attribution on every download.
