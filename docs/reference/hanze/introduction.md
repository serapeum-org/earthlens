# HANZE historical flood impacts — introduction

[HANZE](https://naturalhazards.eu/) (Historical Analysis of Natural Hazards in Europe), compiled by Paprotny et
al., is the reference record of **observed** European flood events and their impacts: real floods since 1870, each
with its fatalities, persons affected, area flooded and economic losses. It is the observed hazard → loss side of
the flood chain — the yardstick a modelled event set is validated against. Companion to the global
[`emdat`](../emdat/introduction.md) backend.

earthlens ships a single `hanze` backend. This page orients it. For the hands-on walkthrough see
[Usage](usage.md); the rendered API is the [Reference](hanze.md) page.

## What you get

`OUTPUT_KIND` is **per instance** here:

- **the default** → `tabular`, a `pandas.DataFrame`. One row per historical flood, carrying HANZE's documented
  columns: `Country code`, `Year`, `Type`, `Regions affected (NUTS 3)`, `Area affected`, `Fatalities`,
  `Persons affected`, and both nominal and inflation-adjusted (2025 €) losses.
- **`with_geometry=True`** → `vector`, a pyramids `FeatureCollection` of the affected NUTS-3 regions. Each event's
  semicolon-separated `Regions affected (NUTS 3)` list is joined to the NUTS-3 boundary polygons, and every
  affected region comes back once with an `n_events` count — ready to draw as a choropleth.

## Know these three things before you use it

**1. It is a pinned Zenodo release, and it is a beta.** earthlens pins the exact version record
[`20478847`](https://doi.org/10.5281/zenodo.8410025) — HANZE **v3.0.1-beta**, 1870–2025, 42 countries — rather than
the moving concept DOI, so a request is reproducible. The `-beta` label is the authors'; treat the figures as
provisional until a stable v3 lands. The files are small individual objects (a 618 KB events CSV, a 2.4 MB region
shapefile zip), so earthlens downloads them directly and caches them under your output directory.

**2. The flood-type vocabulary has four values, and `Compound` is not one of them.** The `Type` column is one of
`River`, `Flash`, `Coastal`, or `River/Coastal` — the combined river-and-coastal case is spelled `River/Coastal`,
not `Compound`. Select with `flood_type="River"` (case-insensitive); an unknown type is rejected with a
did-you-mean hint.

**3. Losses are reported, not exposure-normalised.** HANZE ships nominal losses and inflation-adjusted *real*
losses (2025 €). Comparing impacts across eras fairly needs exposure normalisation — accounting for how much more
there is to damage today — which is the paper's derived method, **not** a raw column. earthlens returns the
reported columns and does not fabricate a normalised one.

## Selection

The backend is selected purely by facet keyword arguments — it declares no `variables`:

- `country=` — one ISO2 code or a list (`"DE"`, `["DE", "NL"]`).
- `region=` — one NUTS-3 code or a list, matched against each event's affected-region list.
- `flood_type=` — one of the four types or a list.
- `start=` / `end=` — a date window, applied on the event `Year`.
- `lat_lim=` / `lon_lim=` (or `aoi=`) — a bbox, resolved through the region geometry.

## Licence and citation

HANZE is **CC-BY-4.0** — redistributable and cacheable with attribution. Cite:

- Paprotny, D. (2024). HANZE (Historical Analysis of Natural Hazards in Europe) v3.0.1-beta. Zenodo.
  <https://doi.org/10.5281/zenodo.8410025>.

## What this backend deliberately does not do

- **The gridded exposure layers and the ~15,000 modelled flood footprints.** HANZE also publishes population /
  land-use exposure grids and a modelled event set (1950–2020). The observed events + impacts table is the
  deliverable here; the exposure grids and modelled footprints are a follow-up, not part of this backend.
- **Exposure-normalised losses.** See point 3 above — the reported columns are returned as-is.
