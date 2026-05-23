# Tropycal — introduction

The `tropycal` backend (`data_source="tropycal"`) returns **tropical-cyclone
best tracks** as a vector
[`FeatureCollection`](https://serapeum-org.github.io/pyramids/) of track
features. It wraps the [`tropycal`](https://tropycal.github.io/tropycal/)
library's `tracks.TrackDataset`.

## What it covers

- **Sources.** Two best-track sources, selected with `source=`:
    - `"ibtracs"` (default) — IBTrACS v04r01, global coverage, every basin,
      1848–present.
    - `"hurdat"` — HURDAT2, the NHC North Atlantic / East Pacific reanalysis.

    There is **no `jtwc` source** in tropycal 1.4; JTWC data is reachable only
    inside IBTrACS for some basins.
- **Basins.** Selected through `variables=[...]` (this backend overloads
  `variables` to mean *basin codes*, not data variables):
  `north_atlantic`, `east_pacific`, `west_pacific`, `north_indian`,
  `south_indian`, `australia`, `south_pacific`, `south_atlantic`, plus the
  aggregates `both` (HURDAT North Atlantic + East Pacific) and `all` (IBTrACS
  global). HURDAT serves only `north_atlantic` / `east_pacific` / `both`;
  IBTrACS serves every basin. The catalog records the valid `(basin, source)`
  pairs and an invalid pair is rejected with a clear error.
- **Output.** `OUTPUT_KIND = "vector"`. Two geometries (`geometry=`):
    - `"point"` (default) — one feature per 6-hourly track fix, carrying
      `storm_id`, `name`, `time`, `lat`, `lon`, `vmax_kt`, `mslp_hpa`,
      `storm_type`, `category`, `basin`, `source`.
    - `"track"` — one `LineString` per storm with summary attributes
      (`max_vmax_kt`, `min_mslp_hpa`, `max_category`, `ace`, `start_time`,
      `end_time`).

  The Saffir–Simpson `category` is **derived** from `vmax` (tropycal exposes no
  category column).

## Limits and scope

- **Best-track only.** tropycal's realtime / operational / forecast products
  (`tropycal.realtime`, NHC advisory cones, forecast objects) are **out of
  scope** for this backend — it returns historical best tracks only.
- **First-load cost.** Constructing a `TrackDataset` downloads and parses a
  whole basin's best-track file. HURDAT loads in a few seconds; IBTrACS is
  larger and slower. earthlens loads each `(basin, source)` once per process
  and reuses it across years/storms, and logs an info line on the first load.
- **No aggregation.** Because the output is vector, the `EarthLens` facade
  rejects an `aggregate=` argument with `NotImplementedError`.

## Installation

tropycal is an optional dependency:

```bash
pip install earthlens[tropycal]
```

This pulls `tropycal`, plus `cartopy` and `shapely` (which tropycal imports at
load time but does not itself declare). tropycal's version shim also requires
`setuptools < 81` (it imports the now-removed `pkg_resources`). No credentials
are needed — tropycal fetches public best-track files from NCEI / NHC over
HTTPS.

## See also

- [Usage](usage.md) — copy-paste recipes.
- [API reference](tropycal.md) — the rendered `earthlens.tropycal` API.
