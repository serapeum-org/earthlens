# Overture Maps — Introduction

<img src="../../_images/logos/overture.svg" alt="Overture Maps Foundation logo" height="60">

`earthlens.overture` downloads vector features from the **Overture Maps
Foundation** — an open, permissively-licensed basemap published as cloud
GeoParquet. It wraps the official [`overturemaps`](https://github.com/OvertureMaps/overturemaps-py)
SDK, which reads the data directly off the public, anonymous
`s3://overturemaps-us-west-2` bucket (no credentials), and returns a
pyramids [`FeatureCollection`](https://github.com/Serapieum-of-alex/pyramids)
(a `GeoDataFrame`) written to disk.

## What it covers

Overture organises the world into six **themes**, each a parent partition
of the GeoParquet store. earthlens curates all of them:

| Theme | Feature types | Geometry | Scale | Typical licenses |
|-------|---------------|----------|-------|------------------|
| `buildings` | `building`, `building_part` | Polygon | ~2.3 B footprints | CDLA-Permissive-2.0, **ODbL-1.0** |
| `places` | `place` | Point | ~57 M POIs | CDLA-Permissive-2.0, **ODbL-1.0** |
| `transportation` | `segment`, `connector` | LineString / Point | ~86 M km of roads | CDLA-Permissive-2.0, **ODbL-1.0** |
| `divisions` | `division`, `division_area`, `division_boundary` | Point / Polygon / LineString | admin boundaries | CDLA-Permissive-2.0, **ODbL-1.0** |
| `base` | `land`, `land_use`, `land_cover`, `water`, `infrastructure`, `bathymetry` | mixed (polygon-dominant) | land/water/infra basemap | CDLA-Permissive-2.0, **ODbL-1.0** |
| `addresses` | `address` | Point | point addresses | CDLA-Permissive-2.0, **ODbL-1.0** |

See [Available datasets](datasets.md) for the full theme/type reference.

## Output kind

Overture is a **`vector`** backend: the result is a table of geolocated
features, not a gridded array. Two consequences follow:

- `Overture.OUTPUT_KIND == "vector"`, so the `EarthLens` facade **rejects
  an `aggregate=` argument** — there is no meaningful gridded reduction of
  a feature table.
- Overture is a **static per-release snapshot** with no temporal axis, so
  `start` / `end` are accepted but ignored. Time is expressed only by the
  **release** (`yyyy-mm-dd.x`). When you do not pin one, the newest release
  is targeted live: the SDK auto-targets it on the default fetch path, and
  the DuckDB (`where=` / `columns=`) path looks it up from Overture's STAC
  catalog at `https://stac.overturemaps.org` before building its S3 glob.
  That is a second endpoint to allow through a proxy or firewall; pin a
  `release` to skip the lookup entirely.

The output is written as **GeoParquet** by default (which preserves
Overture's deeply-nested schema — `names`, `categories`, `sources`, … —
losslessly), with GeoPackage and GeoJSON as alternatives.

## The headline feature — per-row licensing

Overture rows carry **per-feature provenance** in a `sources` column, and
different sources carry different licenses: the permissive bulk is
`CDLA-Permissive-2.0`, but **OpenStreetMap-derived rows carry
`ODbL-1.0`**, a share-alike license with attribution obligations. No other
earthlens backend surfaces this, and for a downstream commercial user it
is critical. `earthlens.overture` therefore:

- adds a per-row **`license_id`** column to every result, and
- emits a **`LicenseWarning`** when any `ODbL-1.0` rows are present.

See the dedicated [Licensing](licensing.md) page for the derivation rule
and the obligations.

## How it maps onto the facade

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="overture",
    variables={"places": []},          # {theme: [type, ...]}; [] = the theme's primary type
    lat_lim=[40.757, 40.759],          # bounded bbox is required
    lon_lim=[-73.987, -73.984],
    path="out",
).download()
```

`download()` returns the list of written file paths (one per requested
feature type).

## Install

```bash
pip install earthlens[overture]
```

The only extra dependency is the `overturemaps` SDK (`geopandas` and
`pyarrow` are already pulled in). No credentials, no account.

See [Usage](usage.md) for the full request shape and every keyword
argument, and [Available datasets](datasets.md) for the theme/type
reference.
