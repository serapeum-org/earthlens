# Overture Maps — Licensing

This is the headline, novel piece of the Overture backend: **per-row
license provenance.** Overture aggregates data from many upstream sources
with different licenses, and the obligations differ. If you redistribute
Overture-derived data — especially commercially — you must know which rows
carry which license. `earthlens.overture` surfaces this per feature.

## The two licenses that matter

- **`CDLA-Permissive-2.0`** — the Community Data License Agreement
  (Permissive). The permissive bulk of Overture. No share-alike
  obligation.
- **`ODbL-1.0`** — the Open Database License, carried by
  **OpenStreetMap-derived** rows. A copyleft / **share-alike** license:
  redistributing or building a derivative database obliges you to
  attribute and to share the derived database under ODbL too.

Other licenses appear on individual sources (e.g. `Apache-2.0`,
`CC0-1.0`), but the legally load-bearing distinction is **ODbL vs not**.

## How earthlens surfaces it

Every fetched `GeoDataFrame` carries a per-row **`license_id`** column,
and a **`LicenseWarning`** is emitted when any `ODbL-1.0` rows are present:

```python
import warnings
from earthlens.core import EarthLens
from earthlens.overture import LicenseWarning

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    paths = EarthLens(
        data_source="overture",
        variables={"buildings": []},
        lat_lim=[40.757, 40.759],
        lon_lim=[-73.987, -73.984],
        path="out",
    ).download()

# Building footprints in many cities are OSM-derived -> ODbL, so a
# LicenseWarning is emitted naming the obligation.
assert any(issubclass(w.category, LicenseWarning) for w in caught)
```

```python
import geopandas as gpd

gdf = gpd.read_parquet(paths[0])
gdf["license_id"].value_counts()
# ODbL-1.0    45
```

## The derivation rule

The per-row `license_id` is derived from the row's `sources` column — a
list of source structs `{property, dataset, license, record_id,
update_time, confidence}` — in this order:

1. **ODbL wins.** If any source carries `ODbL-1.0` *or* names the
   `OpenStreetMap` dataset, the row is `ODbL-1.0`. The share-alike
   obligation dominates the combined feature, so it wins outright — this
   is deliberately *not* "first non-empty license", because an ODbL source
   that is not listed first must still be caught.
2. **Otherwise, the explicit licenses.** If the sources carry explicit
   permissive licenses, the row is the sorted, `"; "`-joined set of those
   distinct licenses (e.g. `Apache-2.0; CDLA-Permissive-2.0` for a place
   built from Foursquare + Overture). This is faithful to a feature built
   from multiple permissively-licensed sources.
3. **Otherwise, the fallback.** With no explicit license field (older
   releases), the row falls back to `CDLA-Permissive-2.0`, Overture's
   default. An `OpenStreetMap` dataset with no explicit license still maps
   to `ODbL-1.0` via step 1.

The same rule is exposed as `earthlens.overture.row_license` for
post-hoc analysis of a `sources` cell.

## Per-theme expectations

| Theme | Typical licensing |
|-------|-------------------|
| `buildings` | Mixed: many footprints are OSM-derived (**ODbL-1.0**); others CDLA-Permissive-2.0. |
| `places` | Mostly permissive (Meta/Foursquare → CDLA-Permissive / Apache), with some OSM (ODbL). |
| `transportation` | Heavily OSM-derived → expect **ODbL-1.0**. |
| `divisions` | Largely OSM-derived → expect **ODbL-1.0**. |

Always check `license_id` on the rows you actually use rather than
assuming a theme-wide license.

## Your obligation

When `ODbL-1.0` rows are present and you redistribute them (or a database
derived from them), honour ODbL: **attribute** OpenStreetMap and the
Overture Maps Foundation, and **share-alike** any derived database. See the
[Overture license documentation](https://docs.overturemaps.org/attribution/)
for the canonical attribution text.
