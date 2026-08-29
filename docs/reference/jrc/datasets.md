# JRC European flood hazard — available datasets

The `jrc-flood` backend ships a single curated product (the EFHM) — there is no
`dataset=` to choose. The request axis is the **return period**, selected with
`return_periods=`.

| Product | Band | Return periods (years) | Native resolution | Units | CRS | Licence |
|---|---|---|---|---|---|---|
| JRC European Flood Hazard Map (EFHM) | `water_depth` | 10, 20, 30, 40, 50, 75, 100, 200, 500 | ~90 m (3 arc-second) | metres | EPSG:4326 | CC-BY-4.0 |

A return period may be given as an int (`100`), a string (`"100"`), or an
`RP`-prefixed string (`"RP100"`). The default when `return_periods=` is omitted
is `[100]`.

## Listing the product programmatically

```python
from earthlens.jrc import Catalog

catalog = Catalog()
list(catalog.datasets)              # ['efhm']
row = catalog.get("efhm")
row.band                            # 'water_depth'
row.return_periods                  # [10, 20, 30, 40, 50, 75, 100, 200, 500]
catalog.license_id                  # 'CC-BY-4.0'
```

## Coverage & packaging

- **Europe and the Mediterranean Basin** — one whole-Europe GeoTIFF per return
  period (`Europe_RP{n}_filled_depth.tif`) on the JRC CEMS-EFAS open-data server.
- The backend reads only the AOI window over `/vsicurl`, so a small subset never
  downloads the full per-return-period file (~300 MB compressed on the wire;
  ~23 GB uncompressed at 110162×51992 px).

## Related coverage

- **Global** JRC flood hazard (`JRC/CEMS_GLOFAS/FloodHazard/v2_1`, ~90 m, also
  covering Europe) is available via the [`gee`](../gee/introduction.md) backend.

## Licence & attribution

The EFHM is **CC-BY-4.0**. Cite Dottori et al. (2020), *River flood hazard maps
for Europe and the Mediterranean Basin region*, JRC / Copernicus Emergency
Management Service.
