# Administrative boundaries — available datasets

The `admin` catalog ships the dataset ids below across four public providers.
Pass one (or more) as `variables=`. Every dataset returns a
`pyramids.feature.collection.FeatureCollection` of polygons in EPSG:4326.

List them live at any time:

```python
from earthlens.core import EarthLens

EarthLens.list_datasets("admin")
```

## geoBoundaries (gbOpen, CC-BY-4.0)

Per-country administrative boundaries. Needs `country=` (ISO3). Columns:
`shapeName`, `shapeISO`, `shapeID`, `shapeGroup`, `shapeType`, `geometry`.

| dataset id | selector | description |
|---|---|---|
| `geoboundaries:adm0` | `country=` | National boundary for one country |
| `geoboundaries:adm1` | `country=` | First sub-national level (states / provinces) |
| `geoboundaries:adm2` | `country=` | Second sub-national level (districts / counties) |
| `geoboundaries:adm3` | `country=` | Third sub-national level |
| `geoboundaries:adm4` | `country=` | Fourth sub-national level |
| `geoboundaries:adm5` | `country=` | Fifth sub-national level |

## CGAZ (Comprehensive Global Admin Zones, CC-BY-4.0)

Single seamless global layers (every country in one file). No selector.

| dataset id | selector | description |
|---|---|---|
| `cgaz:adm0` | — | Seamless global ADM0 (national boundaries) |
| `cgaz:adm1` | — | Seamless global ADM1 (first sub-national level) |
| `cgaz:adm2` | — | Seamless global ADM2 (second sub-national level) |

## Natural Earth (public domain)

Global cultural admin layers by scale. Optional `scale=` (`10m` / `50m` /
`110m`); each dataset has a default.

| dataset id | selector | default scale | description |
|---|---|---|---|
| `natural_earth:countries` | `scale=` | 110m | Admin-0 countries |
| `natural_earth:states` | `scale=` | 50m | Admin-1 states / provinces |

## US Census TIGER/Line (public domain)

Generalized cartographic-boundary (`cb_`) files; native NAD83, reprojected to
EPSG:4326. Optional `year=` (defaults to 2023); `tiger:tract` is per-state and
needs `state=` (FIPS code).

| dataset id | selector | description |
|---|---|---|
| `tiger:nation` | `year=` | US nation outline (5m) |
| `tiger:state` | `year=` | US states (500k) |
| `tiger:county` | `year=` | US counties (500k) |
| `tiger:tract` | `year=`, `state=` | Census tracts for one state (500k, per-state) |

## Why GADM is absent

[GADM](https://gadm.org/) is intentionally excluded — its no-commercial /
no-redistribute license is incompatible with redistribution. The four shipped
sources are all CC-BY-4.0 or public domain. See the
[Introduction](introduction.md#why-gadm-is-not-here).
