# Supported data sources

earthlens exposes every provider through one facade, `EarthLens(data_source="<key>", ...)`. The table below lists
the providers that are integrated today, the string key(s) each one accepts, the natural output it produces, and
the optional dependency (extra) you need to install for it.

Install a backend's optional dependency with:

```bash
pip install earthlens[<extra>]      # e.g. earthlens[ecmwf]
pip install earthlens[all]          # every backend's SDK
```

Backends with no extra (CHC, GDACS, OpenAQ) need only the core install — they use anonymous FTP or plain HTTP.

## Integrated providers

| Provider | `data_source` key(s) | Output | Auth | Extra | Docs |
|---|---|---|---|---|---|
| Climate Hazards Center (CHIRPS / CHIRTS / SPI / SPEI / WBGT / …) | `chc`, `chirps` | raster | anonymous FTP | — | [CHC](chc/introduction.md) |
| ERA5 on AWS (`era5-pds`) | `amazon-s3` | raster | unsigned AWS (public bucket) | `s3` | [Amazon S3](s3.md) |
| ECMWF Climate Data Store | `ecmwf` | raster | `~/.cdsapirc` token | `ecmwf` | [ECMWF](ecmwf.md) |
| Google Earth Engine | `gee`, `google-earth-engine` | raster | service account | `gee` | [GEE](gee/introduction.md) |
| Copernicus Marine (CMEMS) | `cmems` | raster | Copernicus Marine login | `cmems` | [CMEMS](cmems/introduction.md) |
| FDSN seismic events (USGS / EMSC / INGV / …) | `fdsn` | vector | none | `fdsn` | [FDSN](fdsn/introduction.md) |
| GDACS disaster alerts | `gdacs` | vector | none | — | [GDACS](gdacs/introduction.md) |
| OpenAQ air quality | `openaq` | tabular | API key (`X-API-Key`) | — | [OpenAQ](openaq/introduction.md) |
| Tropycal tropical-cyclone tracks | `tropycal` | vector (tabular for SHIPS) | none | `tropycal` | [Tropycal](tropycal/introduction.md) |
| STAC — Planetary Computer / CDSE / Earth Search | `stac`, `cdse` | raster | per-endpoint (anonymous / MPC SAS / CDSE S3) | `stac` | [STAC](stac/introduction.md) |
| NASA Earthdata (9 EOSDIS DAACs via `earthaccess`) | `earthdata` | per-dataset (raster / vector / tabular) | EDL login or bearer token | `earthdata` | [Earthdata](earthdata/introduction.md) |
| openEO server-side processing (defaults to CDSE) | `openeo` | raster | CDSE OIDC (interactive or client-credentials) | `openeo` | [openEO](openeo/introduction.md) |
| Humanitarian Data Exchange (UN OCHA, CKAN) | `hdx` | mixed | none (public) | `hdx` | [HDX](hdx/introduction.md) |

`Output` is the backend's `OUTPUT_KIND` — `raster` writes GeoTIFF/COG/NetCDF, `vector` writes geometry tables
(GeoJSON / GeoPackage), `tabular` writes plain tables (CSV / Parquet), and `mixed` (HDX) downloads each
resource file as-is in whatever format it ships. It also governs `aggregate=`: the temporal aggregator is
accepted for `raster` backends and rejected for `vector` / `tabular` ones; HDX (`mixed`) rejects it because
resources are returned as-is.

## Planned providers (not yet integrated)

These have a completion plan but no code yet — they are **not** available from the facade.

| Provider | Planned key | Notes |
|---|---|---|
| NASA FIRMS active fire detections | `firms` | `MAP_KEY` auth |

For the full roadmap of providers beyond these, see the project's planning notes.
