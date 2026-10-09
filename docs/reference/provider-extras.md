# Provider → extra mapping

Which pip **extra** (if any) each earthlens backend needs, the **SDK** it pulls, its **distribution**, and whether
it is bundled in `earthlens[all]`.

**At a glance:** **61 backends** · **34 extras** (31 in `all`, 3 held out) · **30 backends are SDK-free** (no extra
— they ship with their thematic distribution and just work).

- **SDK-bearing backend** → `pip install "earthlens[<extra>]"`.
- **SDK-free backend** → nothing extra: `pip install earthlens` (or just the theme, e.g. `pip install
  earthlens-atmosphere`).
- The extra **name** is what you type in `pip install "earthlens[<name>]"`; note the casing (`eea_aq`,
  `sentinel-hub`, `usgs-water`, `ecmwf-modern`).

## Backends with an extra (31 backends, 33 extras)

| Backend | Distribution | Extra (`pip install "earthlens[…]"`) | SDK / dependency it adds | In `all`? |
|---|---|---|---|:---:|
| `asf` | imagery | `asf` | `asf_search` (+ the `earthdata` extra) | ✅ |
| `argo` | ocean | `argo` | `argopy` | ❌ held out |
| `cmems` | ocean | `cmems` | `copernicusmarine` | ✅ |
| `cmip6` | atmosphere | `cmip6` | *(empty — no SDK; works without it)* | ✅ |
| `dem` | land | `dem` | `earthlens-core[s3]` (boto3) | ✅ |
| `earthdata` | imagery | `earthdata` | `earthaccess` (py ≥ 3.12) | ✅ |
| `ecmwf` | atmosphere | `ecmwf` | `cdsapi` | ✅ |
| `ecmwf` | atmosphere | `ecmwf-modern` | `ecmwf-datastores-client` | ✅ |
| `eea_aq` | atmosphere | `eea_aq` | `airbase`, `nest_asyncio` | ✅ |
| `emdat` | hazards | `emdat` | `earthaccess` | ✅ |
| `erddap` | ocean | `erddap` | `erddapy` | ✅ |
| `eumetsat` | imagery | `eumetsat` | `eumdac` | ✅ |
| `fdsn` | hazards | `fdsn` | `obspy` | ✅ |
| `gbif` | land | `gbif` | `pygbif` | ✅ |
| `gee` | imagery | `eedai` | `pyramids-eo` (optional EEDAI fetch path) | ❌ held out |
| `gee` | imagery | `gee` | `earthengine-api`, `google-api-python-client`, `google-cloud-storage`, `Rtree`, `urllib3` | ✅ |
| `ghsl` | land | `ghsl` | *(empty — no SDK; works without it)* | ✅ |
| `goes` | atmosphere | `goes` | `earthlens-core[s3]` (boto3) | ✅ |
| `hdx` | hazards | `hdx` | `hdx-python-api` | ✅ |
| `jaxa` | imagery | `jaxa` | `jaxa.earth`, `gportal` | ✅ |
| `nwm` | ocean | `nwm` | `earthlens-core[s3]` (boto3), `pyramids-gis[parquet]` | ✅ |
| `nwp` | atmosphere | `nwp` | `herbie-data`, `ecmwf-opendata`, `ecmwflibs` (win) | ✅ |
| `obis` | ocean | `obis` | `pyobis` | ✅ |
| `openeo` | imagery | `openeo` | `openeo` | ✅ |
| `osm` | hazards | `osm` | `overpy`, `ohsome`, `osmium` (pyosmium; the `pyrosm` engine is opt-in) | ✅ |
| `overture` | hazards | `overture` | `overturemaps`, `duckdb` | ✅ |
| `radar` | atmosphere | `radar` | `earthlens-core[s3]` (boto3) | ✅ |
| `s3` | atmosphere | `s3` | `earthlens-core[s3]` (boto3 / botocore) | ✅ |
| `sentinel_hub` | imagery | `sentinel-hub` | `sentinelhub` | ✅ |
| `stac` | imagery | `stac` | `pyramids-gis[stac]` | ✅ |
| `tropycal` | atmosphere | `tropycal` | `tropycal`, `cartopy` | ✅ |
| `usgs_water` | ocean | `usgs-water` | `dataretrieval` | ✅ |
| `worldpop` | land | `worldpop` | `worldpoppy`, `py7zr` | ✅ |

## Backends with NO extra — SDK-free (30)

These use only core + pyramids (FTP / plain HTTP / static files), so there is **no extra to install** — they ship
with their thematic distribution and work out of the box.

| Backend | Distribution | Backend | Distribution |
|---|---|---|---|
| `admin` | hazards | `airnow` | atmosphere |
| `aqueduct` | hazards | `bathymetry` | land |
| `caravan` | ocean | `catrare` | atmosphere |
| `chc` | atmosphere | `climate-indices` | atmosphere |
| `drought` | atmosphere | `fabdem` | land |
| `firms` | hazards | `flodis` | hazards |
| `flopros` | hazards | `gdacs` | hazards |
| `glaciers` | land | `hanze` | hazards |
| `isimip` | atmosphere | `iucn` | land |
| `jrc` | hazards | `mswep` | atmosphere |
| `nrel` | atmosphere | `nsi` | hazards |
| `openaq` | atmosphere | `pvgis` | atmosphere |
| `radklim` | atmosphere | `risk-indicators` | hazards |
| `sensor-community` | atmosphere | `soilgrids` | land |
| `solar-wind-atlas` | atmosphere | `wdpa` | land |

## Notes

- **`all` bundles 31 of the 33 extras.** Two are deliberately held out (still installable on their own):
  - **`argo`** — `argopy` pins `xarray>=2025.7` while `openeo` pins `xarray<2025.1.2`; the two can't co-resolve
    (see `#789`).
  - **`eedai`** — installing it flips the GEE backend's default `engine="auto"` onto the `pyramids-eo` EEDAI
    reader, which samples and grids differently from Earth Engine. It resolves cleanly; it is held out so an
    `all` upgrade never changes an existing user's pixels.
- **The `osm` extra covers all three OSM protocols and is in `all`** — `overpy` + `ohsome` (live queries) plus
  `osmium` (the wheel-clean `pyosmium` engine, the `pbf` default), all wheels. The richer in-memory `pyrosm` pbf
  engine is opt-in via a manual `pip install pyrosm` (which builds the sdist-only `cykhash` from source); see `#1186`.
- **Two empty extras** — `cmip6` and `ghsl` are declared (`= []`) but pull **no** dependency; the backend works
  without the extra. They exist for API/CLI symmetry.
- **Extras that reuse the S3 client** — `s3`, `radar`, `goes`, `dem`, `nwm` pull `earthlens-core[s3]` (boto3) rather
  than a bespoke SDK.
- **`ecmwf` and `gee` each have more than one extra** — a base one and a variant (`ecmwf-modern`, `eedai`) —
  which is why 33 extras cover 31 backends.
- **Distributions:** `earthlens-atmosphere`, `-ocean`, `-imagery`, `-land`, `-hazards`. The meta-package
  `earthlens` depends on all five; installing a single distribution gives you that theme's backends (SDK-free ones
  usable immediately; SDK-bearing ones after their extra).
