# Overture Maps — Available datasets

The curated catalog (`src/earthlens/overture/overture_data_catalog.yaml`,
loaded by `earthlens.overture.Catalog`) maps four friendly **themes** to
their Overture feature **types**. Unlike the raster backends there are no
per-variable bands — a request selects a theme and its type(s). All four
themes are `vector`.

Browse it in code (no network):

```python
from earthlens.overture import Catalog

cat = Catalog()
cat.themes()                       # ['buildings', 'divisions', 'places', 'transportation']
cat.get_theme("buildings").types   # ['building', 'building_part']
cat.available_releases             # ['2026-05-20.0', '2026-04-15.0']
```

## Curated themes

### `buildings`

Building footprints and their parts (~2.3 B rows globally). The headline
Overture theme. **Bounded-bbox only** (guarded — see [Usage](usage.md)).

| Field | Value |
|-------|-------|
| Types | `building` (primary), `building_part` |
| Geometry | Polygon |
| Key columns | `id`, `names`, `class`, `subtype`, `height`, `num_floors`, `sources` |
| Licenses | `CDLA-Permissive-2.0`, `ODbL-1.0` (OSM-derived) |

### `places`

Points of interest (~57 M POIs) with names, categories, and contact
details. Sourced largely from Meta/Foursquare with OSM-derived rows mixed
in.

| Field | Value |
|-------|-------|
| Types | `place` (primary) |
| Geometry | Point |
| Key columns | `id`, `names`, `categories`, `confidence`, `addresses`, `sources` |
| Licenses | `CDLA-Permissive-2.0`, `ODbL-1.0` |

### `transportation`

The transportation network (~86 M km of roads): routable segments and the
connectors that join them.

| Field | Value |
|-------|-------|
| Types | `segment` (primary, LineString), `connector` (Point) |
| Geometry | LineString |
| Key columns | `id`, `names`, `subtype`, `class`, `connectors`, `sources` |
| Licenses | `CDLA-Permissive-2.0`, `ODbL-1.0` (heavily OSM-derived) |

### `divisions`

Administrative boundaries and places: division points, their boundary
lines, and the filled area polygons. Mostly OSM-derived. Unguarded (few
rows globally).

| Field | Value |
|-------|-------|
| Types | `division`, `division_area` (primary, Polygon), `division_boundary` |
| Geometry | Polygon |
| Key columns | `id`, `names`, `subtype`, `country`, `region`, `sources` |
| Licenses | `CDLA-Permissive-2.0`, `ODbL-1.0` |

## Releases

Overture publishes a new **release** roughly monthly, identified
`yyyy-mm-dd.x`. The bundled `available_releases:` index is informational
(rebuilt by the [refresh tool](usage.md#catalog-tooling)); the SDK
auto-targets the newest release when `release=None`. Pin a `release` for
reproducible downloads.

## Out of scope

The upstream `base` (land, land use, water, infrastructure, bathymetry)
and `addresses` themes exist but are **not curated** here. They are
follow-ons; the curated MVP is buildings / places / transportation /
divisions.
