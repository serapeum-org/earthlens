# Copernicus Marine Service — introduction

<img src="../../_images/logos/cmems.svg" alt="Copernicus Marine Service logo" height="60">

The [Copernicus Marine Service](https://marine.copernicus.eu/) (CMEMS,
operationally run by Mercator Ocean International for the European
Commission's Copernicus programme) is the public European reference for
operational oceanography. Its remit covers ocean physics
(temperature / salinity / currents / sea level / sea ice), ocean
biogeochemistry (chlorophyll, nutrients, oxygen, optics), wave and wind
forecasts, in-situ observations, and merged satellite analyses such as
OSTIA SST and altimetric sea-level anomaly. CMEMS hosts roughly
**600 datasets across 50-odd product families**, with cadences from
hourly forecasts to multi-decadal reanalyses, and is the working
dataset behind a large fraction of operational ocean-state monitoring,
maritime safety, and climate-services workflows.

This page orients the `earthlens` CMEMS backend. For the hands-on
download walkthrough see [Usage](usage.md); for credentials see
[Authentication](authentication.md); the rendered API is the
[Reference](cmems.md) page.

## Why it matters here

The other earthlens backends fetch from a single-purpose vendor cloud
(CHC over FTP, ERA5 from AWS, CDS via `cdsapi`, Google Earth Engine).
CMEMS sits beside them with three properties that shape the backend:

- **A first-party SDK does the heavy lifting.** Mercator Ocean ships
  the official [`copernicusmarine`](https://help.marine.copernicus.eu/)
  toolbox (this package targets v2.x), which exposes a small set of
  primitives — `login`, `describe`, `subset`, `open_dataset`. The
  earthlens backend wraps `subset()` server-side: the toolbox does the
  bbox / time / variable / depth slicing on Mercator's infrastructure
  and streams a single NetCDF (or Zarr store) back to the user's
  output directory. earthlens never has to walk a raw archive.

- **One free registration, then no quotas.** Sign up once at
  <https://marine.copernicus.eu/register>, run
  `copernicusmarine login` to save credentials in
  `~/.copernicusmarine/`, and every subsequent process on the same
  machine is authenticated automatically. There are no per-request
  rate limits, no per-account download caps, and (with the toolbox's
  parallel-streaming defaults) requests are limited mainly by your
  bandwidth and the dataset's native resolution.

- **A curated catalog, not a gate.** earthlens ships a `catalog/`
  directory of per-domain YAML files — a map of marine datasets with
  their variable / cadence / domain / temporal metadata, plus an
  `_index.yaml` listing every dataset id the toolbox publishes. The
  catalog is a *convenience*, not a
  gate: any dataset id the toolbox recognises works as a key in the
  `CMEMS(variables=...)` mapping, curated or not. The catalog's
  purpose is to make the in-package documentation usable, give
  `earthlens.aggregate` the variable shape it needs, and avoid
  forcing every user through the toolbox's full `describe()` walk
  before they can issue a download.

The result: any CMEMS dataset addressable by the toolbox is one
`CMEMS(variables={"<dataset_id>": [...]})` away.

## The product line

The CMEMS catalogue is organised under twelve thematic groups; the
earthlens curated rows currently target the most-visible families in
each:

| Group | Curated examples | What they cover |
|---|---|---|
| **Global ocean physics** | `GLORYS12` reanalysis (`cmems_mod_glo_phy_my_0.083deg_P1D-m`), global NRT analysis-forecast | 4-D temperature / salinity / currents / sea-surface height |
| **Global biogeochemistry** | `PISCES` reanalysis (`cmems_mod_glo_bgc_my_0.25deg_P1D-m`) | chlorophyll, nutrients (NO3 / PO4 / Si), dissolved O2 |
| **SST analyses** | OSTIA L4 NRT, ESA CCI L4 reprocessed | merged satellite + in-situ SST analyses (`analysed_sst`, `sea_ice_fraction`) |
| **Sea level / altimetry** | Multi-mission L4 SLA / ADT (`cmems_obs-sl_glo_phy-ssh_my_allsat-l4-duacs-0.25deg_P1D`) | sea-level anomaly, absolute dynamic topography, surface geostrophic velocity |
| **Sea ice** | AMSR2 L4 concentration NRT | hemispheric ice concentration / thickness |
| **Mediterranean** | MED-CMCC multi-year physics | regional physics reanalysis |
| **Black Sea** | Black-Sea multi-year physics | regional physics reanalysis |
| **Baltic Sea** | Baltic multi-year physics | regional physics reanalysis |
| **Arctic** | TOPAZ4 multi-year physics | regional physics + sea-ice reanalysis |
| **IBI** (Iberian-Biscay-Ireland) | IBI multi-year physics | regional physics reanalysis |
| **NW Shelf** | NW European shelf physics / BGC | regional physics / biogeochem |
| **Indicators (OMI)** | ocean monitoring indicators | trends + anomalies (no downloadable variables, so not curated) |

Browse the full live dataset index with
`uv run python tools/cmems/refresh_cmems_catalog.py refresh --dry-run`
(or read it from `Catalog().available_datasets`). Curated rows live
under `Catalog().datasets`, and every curated id is a member of
`available_datasets`.

## Data is shipped as a server-side subset, not a file walk

Every CMEMS dataset is exposed by the toolbox as a logical
multi-dimensional store (NetCDF / Zarr) keyed by `(dataset_id,
variables, bbox, time-window, optional depth-window)`. Issue one
`copernicusmarine.subset()` call and the toolbox returns a single
NetCDF (or directory-store Zarr) containing exactly the slice you
asked for — the bbox cut, the depth clip, the variable selection are
all server-side. There is no client-side walk of the source archive.

The earthlens backend wraps that primitive 1-for-1: each
`(dataset_id, [variables])` pair in the request maps to one
`subset()` call. Per-pair failures (`DatasetNotFound`, bbox out of
domain, unknown variable) are logged and surfaced in a summary but
do not abort the remaining pairs — the "one bad variable does not
kill the batch" policy familiar from the ECMWF backend.

## Catalog layout

The CMEMS catalog ships as a directory of per-domain YAML files inside
the package (mirroring the GEE catalog's per-category split):

```
src/earthlens/cmems/catalog/
  _index.yaml              # available_datasets: (every toolbox dataset id)
  global-physics.yaml      # datasets: (global ocean physics)
  global-biogeochem.yaml   # datasets: (global BGC + ocean colour)
  global-sst.yaml          # datasets: (global SST analyses)
  global-sealevel.yaml     # datasets: (global altimetry)
  global-wave.yaml         # datasets: (global wave)
  global-wind.yaml         # datasets: (global wind)
  global-observations.yaml # datasets: (in-situ / multi-obs)
  global-other.yaml        # datasets: (everything else global)
  mediterranean.yaml       # datasets: (Mediterranean basin)
  black-sea.yaml           # ...
  baltic-sea.yaml
  arctic.yaml
  polar.yaml               # hemispheric sea-ice
  ibi.yaml                 # Iberia-Biscay-Ireland
  nw-shelf.yaml            # NW European shelf
  indicator.yaml           # OMI indicators
```

`_index.yaml`'s `available_datasets:` is an informational list of
every CMEMS dataset id the toolbox publishes (~1,251, regenerated by
`tools/cmems/refresh_cmems_catalog.py refresh`); the per-domain
`datasets:` blocks are the curated subset earthlens models in detail.
The loader merges every `*.yaml` in the directory into one `Catalog`
and enforces that every curated id is a member of
`available_datasets`. Tooling:

- `tools/cmems/refresh_cmems_catalog.py` — walks
  `copernicusmarine.describe()`, rewrites `_index.yaml`'s
  `available_datasets:`, and emits ready-to-paste `datasets:` stanzas
  via `--with-datasets <product_id>` / appends them to the routed
  per-domain file via `add-ids <dataset_id>`.
- `tools/cmems/probe_cmems_netcdf.py` — issues a tiny `subset()`
  per `(dataset, variables)` pair and writes a JSON sidecar mapping
  the variable's on-disk `long_name` / `units` (the source of
  truth the aggregator consumes).
- `tools/cmems/audit_cmems_datasets.py` — anonymous coverage report
  classifying every curated id as `covered` / `partial` / `renamed`
  / `missing`. `--strict` exits non-zero on drift; suitable as a CI
  gate.

This is the CMEMS analogue of the ECMWF `cds_data_catalog.yaml`
single-file curation pattern.

## Authentication

A free Copernicus Marine portal account, plus either explicit
credentials passed to `CMEMS(...)`, environment variables, or a saved
configuration file from a previous `copernicusmarine login`. See
[Authentication](authentication.md) for the full credential resolution
order and CI-secret pattern.

## Cost

**Free** for everyone, including commercial use. CMEMS datasets are
released under [Copernicus open-licence terms](https://marine.copernicus.eu/user-corner/service-commitments-and-licence)
(broadly **CC-BY-4.0** with attribution required; a few NRT products
have additional terms documented on their portal landing page). There
are no per-account download caps and no egress fees from the toolbox
endpoints; only your bandwidth and the dataset's native resolution
cap throughput.

## References

- CMEMS portal: <https://marine.copernicus.eu/>
- Toolbox documentation: <https://help.marine.copernicus.eu/>
- Toolbox source: <https://github.com/mercator-ocean/copernicus-marine-toolbox>
- GLORYS12 (global physics reanalysis): [doi:10.48670/moi-00021](https://doi.org/10.48670/moi-00021)
- PISCES (global biogeochemistry reanalysis): [doi:10.48670/moi-00019](https://doi.org/10.48670/moi-00019)
- OSTIA L4 SST: [doi:10.48670/moi-00165](https://doi.org/10.48670/moi-00165)
- earthlens CMEMS usage: [Usage](usage.md)
- earthlens CMEMS authentication: [Authentication](authentication.md)
- earthlens CMEMS API: [Reference](cmems.md)
