# DWD RADKLIM / RADOLAN — catalog

The bundled `radklim_data_catalog.yaml` declares the four products and the
shared grid / licence metadata. It is loaded by
`earthlens.radklim.Catalog` (a `pydantic`-backed reader over the strict YAML
loader).

## Products

| Product | Stream | Cadence | Format | Coverage |
|---|---|---|---|---|
| `radklim-rw` | reproc (RADKLIM) | hourly | yearly NetCDF `.tar.gz` | 2001– (full archive) |
| `radklim-yw` | reproc (RADKLIM) | 5-min | yearly NetCDF `.tar.gz` | 2001– (full archive) |
| `radolan-rw` | operational (RADOLAN) | hourly | per-timestamp HDF5 / `.bz2` | rolling ~2 days |
| `radolan-yw` | operational (RADOLAN) | 5-min | per-timestamp HDF5 / `.bz2` | rolling ~2 days |

Each reproc row carries the reprocessing `version` (`2017_002`) and its CDC
tree token; each operational row carries its `retention_days`.

## Inspecting the catalog

```python
from earthlens.radklim import Catalog

cat = Catalog()
cat.products()                       # ['radklim-rw', 'radklim-yw', 'radolan-rw', 'radolan-yw']
cat.license                          # 'CC-BY-4.0/GeoNutzV'
cat.grid["id"]                       # 'radolan-polar-stereographic'

yw = cat.get_product("radklim-yw")
yw.stream, yw.default_format, yw.version   # ('reproc', 'nc', '2017_002')
```

An unknown key raises a `ValueError` with a did-you-mean hint.

## URL templates

- **reproc:** `.../CDC/grids_germany/{5_minutes|hourly}/radolan/reproc/2017_002/
  netCDF/{year}/{CODE}2017.002_{year}_netcdf.tar.gz`
- **operational:** `.../weather/radar/radolan/{rw|yw}/
  raa01-{code}_10000-{YYMMDDHHMM}-dwd---bin.{hdf5|bz2}`
