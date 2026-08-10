# FABDEM — available datasets

The `fabdem` backend ships a single curated product — there is no `dataset=` to
choose. It is a static global bare-earth elevation grid served as 10°×10° bundle
zips of 1° Cloud-Optimized GeoTIFF tiles.

| Product | Version | Native resolution | Band | Units | CRS | Licence |
|---|---|---|---|---|---|---|
| FABDEM (Forest And Buildings removed Copernicus DEM) | V1-2 | ~30 m (1 arc-second) | `elevation` | metres | EPSG:4326 | CC-BY-NC-SA 4.0 |

## Coverage & packaging

- **Global land**, tiled into 1°×1° cells; ocean-only cells and 10° bundles are
  not published (the backend skips them).
- Delivered from the University of Bristol data repository as
  `<SW>-<NE>_FABDEM_V1-2.zip` 10° bundles (e.g. `N50E000-N60E010_FABDEM_V1-2.zip`),
  each holding `<SW>_FABDEM_V1-2.tif` 1° tiles (e.g. `N50E000_FABDEM_V1-2.tif`).

## Listing the product programmatically

```python
from earthlens.fabdem import Catalog

catalog = Catalog()
list(catalog.datasets)          # ['fabdem']
row = catalog.get("fabdem")
row.band, row.version, row.units    # ('elevation', 'V1-2', 'm')
catalog.license_id              # 'CC-BY-NC-SA-4.0'
```

## Licence & attribution

FABDEM V1-2 is **CC-BY-NC-SA 4.0 (non-commercial)**; `download()` emits a
`LicenseWarning`. Commercial licensing is via [Fathom](https://www.fathom.global/).
Cite Hawker et al. (2022), *A 30 m global map of elevation with forests and
buildings removed*, Environmental Research Letters 17, 024016.

## Related backends

- For the Copernicus **surface** DEM (GLO-30 / GLO-90, includes canopy and
  buildings) use the [`dem`](../dem/introduction.md) backend.
- For land-and-ocean relief use the [`bathymetry`](../bathymetry/introduction.md)
  backend.
