# Bathymetry — available datasets

The `bathymetry` backend ships curated DEM rows across two transports. Pick one
with the `dataset=` argument. Every row is a static elevation grid with a single
band in **metres relative to sea level** (negative = below sea level).

- **Global DEMs (ERDDAP `griddap`)** — GEBCO / ETOPO, served as NOAA ERDDAP
  coverages, subset server-side to the request bbox.
- **European high-resolution DEM (OGC WCS)** — EMODnet Bathymetry, read through
  pyramids `Dataset.from_wcs`; the coastal / shelf complement to the global DEMs.

| `dataset=` | DEM | Native resolution | Band | Transport | Server / coverage |
|---|---|---|---|---|---|
| `gebco_2020` | GEBCO 2020 (topography + bathymetry) | 15 arc-second | `elevation` | ERDDAP `griddap` | NOAA CoastWatch ERDDAP |
| `etopo1_ice` | ETOPO1 global relief, **Ice Surface** | 1 arc-minute | `z` | ERDDAP `griddap` | NOAA upwell ERDDAP |
| `etopo1_bedrock` | ETOPO1 global relief, **Bedrock** | 1 arc-minute | `z` | ERDDAP `griddap` | NOAA upwell ERDDAP |
| `emodnet` | EMODnet Bathymetry DTM, **latest release** (DTM 2024) | ~3.75 arc-second | `elevation` | OGC WCS | EMODnet Bathymetry WCS (`emodnet:mean`) |
| `emodnet_2022` | EMODnet Bathymetry DTM, 2022 release | ~3.75 arc-second | `elevation` | OGC WCS | EMODnet Bathymetry WCS (`emodnet:mean_2022`) |
| `emodnet_2020` | EMODnet Bathymetry DTM, 2020 release | ~3.75 arc-second | `elevation` | OGC WCS | EMODnet Bathymetry WCS (`emodnet:mean_2020`) |
| `emodnet_2018` | EMODnet Bathymetry DTM, 2018 release | ~3.75 arc-second | `elevation` | OGC WCS | EMODnet Bathymetry WCS (`emodnet:mean_2018`) |
| `emodnet_2016` | EMODnet Bathymetry DTM, 2016 release | ~3.75 arc-second | `elevation` | OGC WCS | EMODnet Bathymetry WCS (`emodnet:mean_2016`) |

The ETOPO / EMODnet **release is the dataset id, not a `variables` knob**: choose
`etopo1_ice` vs `etopo1_bedrock`, or `emodnet` (latest) vs a year-stamped
`emodnet_2022` / `_2020` / `_2018` / `_2016`.

## EMODnet Bathymetry (European seas)

`dataset="emodnet"` fetches the EMODnet Digital Terrain Model — a high-resolution
(~3.75 arc-second) grid of mean sea-floor depth harmonised from national
hydrographic surveys and satellite-derived bathymetry, covering **European seas
and the extended NE-Atlantic domain** (roughly longitude −70.5°..43°, latitude
11°..90°). It is read over **OGC WCS** via pyramids `Dataset.from_wcs` and cropped
to the request bbox.

Because the coverage is regional, a request whose bounding box falls entirely
outside that domain raises a clear error pointing you back at the global DEMs
(`gebco_2020` / `etopo1_ice`) — the WCS server would otherwise return a
zero-filled grid rather than an error.

**Licence / attribution.** EMODnet Bathymetry is provided under the EMODnet use
conditions (attribution required). Cite: *EMODnet Digital Bathymetry (DTM 2024),
EMODnet Bathymetry Consortium* (doi:10.12770/cf51df64-56f9-4a99-b1aa-36b8d7b743a1).
See the [EMODnet terms of use](https://emodnet.ec.europa.eu/en/terms-use-emodnet-online-services-data-and-data-products).

## Facade aliases

Two friendly aliases route to the same backend, so a script reads the way
you think about the data:

- `data_source="gebco"` — then pass `dataset="gebco_2020"`.
- `data_source="etopo"` — then pass `dataset="etopo1_ice"` or
  `dataset="etopo1_bedrock"`.

`data_source="bathymetry"` works with any of the `dataset=` ids (including the
`emodnet` rows).

## Listing the ids programmatically

```python
from earthlens.bathymetry import Catalog

catalog = Catalog()
sorted(catalog.datasets)
# ['emodnet', 'emodnet_2016', 'emodnet_2018', 'emodnet_2020', 'emodnet_2022',
#  'etopo1_bedrock', 'etopo1_ice', 'gebco_2020']
catalog.get("gebco_2020").variable          # 'elevation'
catalog.get("emodnet").transport            # 'wcs'
catalog.get("emodnet").dataset_id           # 'emodnet:mean'
```

## Versions

The global rows are the versions cleanly subsettable over the public ERDDAP
`griddap` transport today (GEBCO 2020 and ETOPO1). For EMODnet, `emodnet` tracks
the latest published DTM release while the year-stamped ids pin an earlier one.
Newer global releases (ETOPO 2022, later GEBCO grids) are not yet exposed as a
bbox-subsettable ERDDAP coverage on the public hosts; when they are, they can be
added as extra catalog rows without any backend change.
