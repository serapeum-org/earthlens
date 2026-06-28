# Glaciers — available datasets

Five datasets across three sources. Resolve one with `Catalog().get(<id>)` (a
did-you-mean hint on a typo); list them with `Catalog().available()`.

| Dataset id | Source | Output | What |
|---|---|---|---|
| `rgi:outlines` | RGI 7.0 | vector | Global glacier outlines (per GTN-G region; clipped to your bbox) |
| `glims:outlines` | GLIMS | vector | Time-series glacier outlines (WFS bbox query) |
| `wgms:mass_balance` | WGMS | tabular | Mass balance (winter / summer / annual) |
| `wgms:front_variation` | WGMS | tabular | Front variation (length change) |
| `wgms:state` | WGMS | tabular | Glacier state (elevation / area / length snapshots) |

## RGI selectors

An `rgi:outlines` request takes a **bounding box** (`lat_lim` / `lon_lim` or
`aoi=`), mapped to the overlapping GTN-G first-order region(s), or a `region=`
override naming one or more region ids directly.

| id | Region | id | Region |
|----|--------|----|--------|
| 01 | Alaska | 11 | Central Europe |
| 02 | Western Canada and USA | 12 | Caucasus and Middle East |
| 03 | Arctic Canada North | 13 | Central Asia |
| 04 | Arctic Canada South | 14 | South Asia West |
| 05 | Greenland Periphery | 15 | South Asia East |
| 06 | Iceland | 16 | Low Latitudes |
| 07 | Svalbard and Jan Mayen | 17 | Southern Andes |
| 08 | Scandinavia | 18 | New Zealand |
| 09 | Russian Arctic | 19 | Subantarctic and Antarctic Islands |
| 10 | North Asia (spans the antimeridian) | | |

Region 20 (Antarctic Mainland) is the ice sheet and is not part of the RGI
glacier product.

## GLIMS selectors

A `glims:outlines` request takes a **bounding box** plus an optional
`max_features=` cap on the WFS result. GLIMS is multi-temporal — expect more than
one outline per glacier, distinguished by their acquisition date.

## WGMS selectors

The `wgms:*` datasets take optional filters: `glacier_id=` (one id or a list),
`glacier_name=` (a case-insensitive substring), `region=` (a GTN-G region id), or
a bounding box (matched against each glacier's point coordinates via the FoG
`glacier` table). With no filter, the whole table is returned.

The WGMS FoG archive also contains a large `change` (elevation / volume) table;
it is intentionally **not** exposed because it is hundreds of megabytes
uncompressed.
