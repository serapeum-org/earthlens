# ISIMIP — usage

## Download a bbox cutout

Pin a facet set, a date window, and a bounding box; `download()` submits the
server-side cutout job and returns the `list[Path]` of the cut NetCDF granules
(one per resolved dataset / decade granule):

```python
from earthlens.core import EarthLens

paths = EarthLens(
    "isimip",
    dataset="ISIMIP3b",         # the simulation round
    gcm="gfdl-esm4",            # the CMIP6 GCM (any casing)
    scenario="ssp585",          # the scenario
    variables=["pr"],           # precipitation
    temporal_resolution="daily",
    start="2030-01-01",
    end="2040-12-31",
    lat_lim=[51.0, 53.0],       # cut to a small European box
    lon_lim=[6.0, 8.0],
    path="isimip-out",
).download()
```

Everything is anonymous — no credentials are needed, only the `isimip` extra
(`pip install "earthlens[isimip]"`). The cutout job runs on ISIMIP's servers,
so only the requested box is transferred.

## The facet set

`gcm`, `scenario`, and `variables` are required; `dataset` defaults to
`"ISIMIP3b"` and `temporal_resolution` to `"daily"`. The backend fetches the
bias-adjusted `InputData` forcing. Every facet is validated against the bundled
catalog, so an unknown GCM / scenario / variable raises a clear did-you-mean
error:

```python
from earthlens.isimip import Catalog

cat = Catalog()
sorted(cat.forcings)     # the curated GCMs / reanalyses
sorted(cat.scenarios)    # the curated scenarios
sorted(cat.datasets)     # the curated variables
cat.get_forcing("gfdl-esm4").round   # -> 'ISIMIP3b'
```

Requesting several variables fans out into one cutout per variable's dataset:

```python
paths = EarthLens(
    "isimip",
    gcm="ukesm1-0-ll",
    scenario="ssp126",
    variables=["tas", "tasmax", "tasmin"],
    start="2030-01-01",
    end="2035-12-31",
    lat_lim=[-5.0, 5.0],
    lon_lim=[10.0, 20.0],
    path="isimip-multi",
).download()
```

## Whole-globe download (opt-in)

The cutout is the default and is mandatory unless you explicitly ask for the raw
global granules. Because those are ~1–2 GB each, `whole_globe=True` warns:

```python
paths = EarthLens(
    "isimip",
    gcm="gfdl-esm4",
    scenario="ssp585",
    variables=["pr"],
    start="2030-01-01",
    end="2040-12-31",
    whole_globe=True,           # no bbox -> download the raw global granules
    path="isimip-global",
).download()
```

Omitting both a bbox and `whole_globe` is rejected, so you never pull ~18 GB by
accident.

## Reading and aggregating the output

`download()` returns raw NetCDF paths — reading, regridding, and reducing them is
[pyramids](https://github.com/serapeum-org/pyramids)' job. The backend does not
decode NetCDF and does **not** accept `aggregate=` (it is refused): reduce the
written granules separately with `earthlens.aggregate.aggregate_netcdf`.
