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
cat.available_types()              # all 15 Overture types (curated + deferred)
cat.available_releases             # ['2026-06-17.0', '2026-07-22.0']
```

## Curated vs available

Following the same pattern as the other backends (e.g. GEE), the catalog
holds two things:

- **Curated `themes:`** — the six themes below, hand-vetted with full
  metadata (types, geometry, key columns, licenses). These are what
  `variables={theme: [...]}` dispatches on.
- **`available_datasets:`** — an auto-generated index of **every** Overture
  feature type the SDK exposes (rebuilt by the
  [refresh tool](usage.md#catalog-tooling) from the live SDK), so the
  catalog reflects the provider's *full* queryable universe. Every curated
  theme's types are a subset of it — and with all six themes curated, the
  two sets now coincide.

The full type universe (15 types across all 6 themes — all curated):

| Theme | Types | Curated? |
|-------|-------|----------|
| `buildings` | `building`, `building_part` | ✅ |
| `places` | `place` | ✅ |
| `transportation` | `segment`, `connector` | ✅ |
| `divisions` | `division`, `division_area`, `division_boundary` | ✅ |
| `base` | `land`, `land_use`, `land_cover`, `water`, `infrastructure`, `bathymetry` | ✅ |
| `addresses` | `address` | ✅ |

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

### `base`

The base map layer the other themes sit on: land, land use, land cover,
water bodies, infrastructure (bridges, towers, …) and bathymetry. Types
span **mixed geometries** — polygon-dominant, but `water` / `infrastructure`
also carry lines and points. Largely OSM- and Daylight-derived. Bounded
bbox only (guarded).

| Field | Value |
|-------|-------|
| Types | `land` (primary), `land_use`, `land_cover`, `water`, `infrastructure`, `bathymetry` |
| Geometry | Polygon (dominant; mixed across types) |
| Key columns | `id`, `names`, `subtype`, `class`, `sources` (plus per-type fields: `depth`, `surface`, `elevation`, `is_salt`, …) |
| Licenses | `CDLA-Permissive-2.0`, `ODbL-1.0` |

### `addresses`

Point addresses (street / number / unit / postcode). Very dense, so a
**bounded bbox is required** (guarded like buildings). Sourced from open
address datasets and OSM.

| Field | Value |
|-------|-------|
| Types | `address` (primary) |
| Geometry | Point |
| Key columns | `id`, `street`, `number`, `unit`, `postcode`, `country`, `sources` |
| Licenses | `CDLA-Permissive-2.0`, `ODbL-1.0` |

## Releases

Overture publishes a new **release** roughly monthly, identified
`yyyy-mm-dd.x`, and keeps only the newest one (or two) on S3 — older
releases are pruned from the bucket and their paths stop resolving.

`release=None` (the default) therefore targets whatever Overture publishes
*now*: the SDK auto-targets it on the default fetch path, and the DuckDB
`where=` / `columns=` path resolves it live before building the S3 glob.
The bundled `available_releases:` index is informational (rebuilt by the
[refresh tool](usage.md#catalog-tooling)) and serves only as an offline
fallback.

Pin a `release` when you need a byte-reproducible download, and expect the
pin to stop resolving once Overture prunes that release — a pinned id is
reproducible only for as long as it exists upstream.

## Coverage

All six Overture themes are curated, so `available_datasets` (the full
type universe) and the curated themes' types coincide — there are no
deferred themes. The `base` types carry mixed geometries; if you need a
single geometry kind, request the relevant type and filter
`geometry.geom_type` client-side.
