# Architecture

This page documents the internal architecture of `earthlens` using [Mermaid](https://mermaid.js.org/) diagrams. It replaces the original draw.io class diagram.

## System Overview

The `EarthLens` facade exposes a uniform API on top of several concrete data-source backends. Each backend implements the `AbstractDataSource` interface, and each has a companion `Catalog` class that describes available variables.

```mermaid
flowchart LR
    user([User])
    earthlens[EarthLens]
    user --> earthlens
    earthlens --> CHIRPS
    earthlens --> S3
    earthlens --> ECMWF
    earthlens --> GEE
    CHIRPS --> FTP[(UCSB FTP<br/>data.chc.ucsb.edu)]
    S3 --> AWS[(AWS S3<br/>era5-pds bucket)]
    ECMWF --> CDS[(ECMWF<br/>Climate Data Store)]
    GEE --> Earth[(Google<br/>Earth Engine)]
```

## Class Diagram

The core abstraction is `AbstractDataSource`. Concrete classes `CHIRPS`, `S3`, `ECMWF`, and the `GEE` subpackage implement it. `AbstractCatalog` plays the same role for the variable/dataset metadata catalogs.

```mermaid
classDiagram
    class AbstractDataSource {
        <<abstract>>
        +space: Dict
        +time: Dict
        +client
        +root_dir: Path
        +temporal_resolution: str
        +variables: list
        +check_input_dates(start, end, res, fmt)*
        +initialize()*
        +create_grid(lat_lim, lon_lim)*
        +download()*
        +download_dataset()
        +api()*
    }

    class AbstractCatalog {
        <<abstract>>
        +catalog: Dict
        +get_catalog()
        +get_variable(var_name)
    }

    class CHIRPS {
        +start_date
        +end_date
        +lat_limits
        +lon_limits
        +check_input_dates(...)
        +initialize()
        +create_grid(lat_lim, lon_lim)
        +download(progress_bar, cores)
        +API(date, args)
        +callAPI(pathFTP, path, filename)
        +post_download(...)
    }

    class S3 {
        +bucket: str
        +check_input_dates(...)
        +initialize(bucket)
        +create_grid(lat_lim, lon_lim)
        +download(progress_bar)
        +downloadDataset(var, progress_bar)
        +API(s3_file_path, local_dir, bucket)
        +parse_response_metadata(response)$
    }

    class ECMWF {
        +check_input_dates(...)
        +initialize()
        +create_grid(lat_lim, lon_lim)
        +download(...)
        +download_dataset(...)
        +api(var_info)
        +post_download(...)
    }

    class EarthLens {
        +DataSources: Dict
        +datasource: AbstractDataSource
        +download(progress_bar, *args, **kwargs)
    }

    AbstractDataSource <|-- CHIRPS
    AbstractDataSource <|-- S3
    AbstractDataSource <|-- ECMWF
    EarthLens o--> AbstractDataSource : delegates to
    AbstractCatalog <|-- CHIRPS_Catalog
    AbstractCatalog <|-- S3_Catalog
    AbstractCatalog <|-- ECMWF_Catalog
    class CHIRPS_Catalog["Catalog (CHIRPS)"]
    class S3_Catalog["Catalog (S3)"] {
        +initialize(bucket)$
        +get_catalog()
        +get_variable(var_name)
        +get_available_years(bucket)
        +get_available_data(...)
    }
    class ECMWF_Catalog["Catalog (ECMWF)"] {
        +get_catalog()
        +get_variable(dataset_name, variable_name)
        +get_dataset(name)
        +describe(name)
    }
```

## GEE Subpackage

The Google Earth Engine backend lives in its own subpackage and has a different shape: rather than implementing `AbstractDataSource`, it wraps the `earthengine-api` client directly through a small class hierarchy.

```mermaid
classDiagram
    class GEE {
        +service_account: str
        +service_key_path: str
        +initialize(service_account, service_key)$
        +encodeServiceAccount(key_dir)$
        +decodeServiceAccount(key_bytes)$
    }

    class Dataset {
        +getDate(...)
        +addBoundary(gdf)
        +filterByRegion(gdf)
    }

    GEE <|-- Dataset
```

## Download Sequence

The user calls `EarthLens.download()`, which delegates to the selected backend. Each backend follows the same high-level sequence: authenticate / open a session, iterate over dates × variables, fetch, and post-process.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Facade as EarthLens
    participant DS as AbstractDataSource
    participant Server as Remote server<br/>(FTP / S3 / CDS)
    participant Pyramids as pyramids-gis

    User->>Facade: EarthLens(data_source, start, end, ...)
    Facade->>DS: instantiate backend
    DS->>DS: initialize() / check_input_dates() / create_grid()
    User->>Facade: download()
    Facade->>DS: download()
    loop for each date × variable
        DS->>Server: api() / callAPI()
        Server-->>DS: NetCDF / raw file
        DS->>Pyramids: post_download() → clip + convert
        Pyramids-->>DS: GeoTIFF
    end
    DS-->>User: files saved under path/
```

## Catalog Pattern

Every data source has a companion `Catalog` class that loads variable metadata from a YAML file (for CHIRPS and ECMWF) or introspects the remote bucket (for S3).

```mermaid
flowchart TB
    subgraph CHIRPS
        direction TB
        C1[Catalog]
        C2[(chirps entries<br/>in code)]
        C1 --> C2
    end
    subgraph ECMWF
        direction TB
        E1[Catalog]
        E2[(ecmwf/catalog/)]
        E1 --> E2
    end
    subgraph S3
        direction TB
        S1[Catalog]
        S2[(era5-pds<br/>S3 bucket listing)]
        S1 --> S2
    end
    subgraph GEE
        direction TB
        G1[Catalog]
        G2[(gee/catalog.yaml)]
        G1 --> G2
    end
```

## Subpackage layout & style

Every provider backend under `src/earthlens/<pkg>/` follows one layout so the
backends read the same way; a new backend should match it.

### Module layout

| File | Role |
|------|------|
| `__init__.py` | Module docstring (required) + `__all__`; re-exports the public surface. |
| `backend.py` | The `AbstractDataSource` subclass `<Provider>`. Always `backend.py` — never `<pkg>.py`. |
| `catalog.py` | The catalog loader (see below). |
| `catalog/` **or** `<pkg>_data_catalog.yaml` | The catalog data (see "Catalog storage"). |
| `auth.py` | Auth surface, when the provider needs credentials (see "Auth"). |
| `_helpers.py` | Private, stateless helpers (optional). |
| `events.py` | Vector-event → `FeatureCollection` builders (vector backends only). |
| `providers.yaml` | Provider registry (backends that populate the base `providers` field). |

Per-backend tooling lives at repo-level `tools/<pkg>/`; tests in `tests/<pkg>/`
(or `tests/test_<pkg>/`). Backend-specific extras (e.g. `gee/filters.py`,
`ecmwf/constraints.py`, `sentinel_hub/evalscripts/`) sit alongside these.

### Catalog storage

Which storage shape a backend uses is decided by a rule, not ad hoc:

- **Sharded `catalog/` directory** — per-family `<family>.yaml` files plus an
  `_index.yaml` holding the informational `available_*` index. Used for large
  or multi-family catalogs (gee, cmems, earthdata, eumetsat, stac, openeo,
  sentinel_hub, ghsl, chc).
- **Single `<pkg>_data_catalog.yaml`** at the package root — for a small,
  single-family enumeration (fdsn, gdacs, firms, radar, tropycal, openaq,
  usgs_water, overture, nwp, s3, worldpop).
- **Large-index variant** — when the upstream "every dataset" index is too big
  to keep inline it lives in a sibling gzipped/plain JSON kept out of the
  `*.yaml` glob (earthdata `catalog/_auto.json`, hdx `catalog/_available.json.gz`)
  while the curated rows stay in `*.yaml`.

Both shapes load through the same loader, which also accepts a single `*.yaml`
file (used by tests that monkey-patch `CATALOG_PATH`).

### Catalog loader API

`catalog.py` always exposes a module-level `CATALOG_PATH`, a
`clear_catalog_cache()` helper, a `(path, mtime_ns)` parse cache, and a pydantic
`Catalog` class (radar keeps a `StationCatalog` alias) that subclasses
`AbstractCatalog`, chains `super().model_post_init()`, and parses through the
shared `earthlens.base.yaml_loader.load_yaml_strict`.

### Auth

When a provider needs credentials the auth surface lives in `auth.py` as a
`<Provider>Auth` + `<Provider>Credentials` pair with env-var fallbacks, raising
`AuthenticationError` on failure. Sanctioned exceptions: a multi-endpoint
backend may use a signer model instead (stac: `signers.py` + `auth_cdse.py`),
and a backend whose SDK owns auth (ecmwf via `~/.cdsapirc`) may keep its
`AuthenticationError` in `backend.py`. Public/anonymous backends (chc, gdacs,
hdx, overture, tropycal) have no auth module.
