# NWP forecasts — usage

## Request shape

The NWP backend takes a `variables` mapping of **model key → list of
parameters** (the same shape as the GEE and STAC backends):

```python
from earthlens.earthlens import EarthLens

lens = EarthLens(
    data_source="nwp",
    variables={"gfs": ["temperature_2m", "precipitation_acc"]},
    start="2024-06-01",
    end="2024-06-02",              # cycle DATE range (inclusive)
    lat_lim=[40, 45],
    lon_lim=[-80, -75],
    path="out/gfs",
    steps=[0, 6, 12],              # forecast lead times, in hours
    mirror="aws",                  # cloud mirror (default "auto")
)
paths = lens.download()            # one bbox-cropped COG per (cycle, step)
```

Each parameter name resolves through the catalog to the centre's
selector — a Herbie `search` regex (`":TMP:2 m above ground:"`) for the
NOAA / ECMWF models, or a provider variable token (`"T_2M"`) for DWD
ICON. See [Catalog & install](catalog.md) for the available models and
their parameter names.

## Forecast steps: `steps=` vs `horizon=`

| kwarg | meaning |
|-------|---------|
| *(neither)* | only the **analysis step** `f000` — keeps a request small |
| `steps=[0, 6, 24]` | exactly these lead times (recommended — explicit) |
| `horizon=48` | `0..48` stepping on the model's `step_cadence_h` (e.g. every 3 h for GFS) |

A step beyond the model's `horizon_h` raises a `ValueError`. `horizon=`
expands on the model's published step cadence rather than blindly hourly,
so it won't request steps a coarse model never publishes. Any step the
model still doesn't carry on a given cycle is skipped per the
`errors=` policy below — it does not abort the download.

### Partial availability (`errors=`)

A `(cycle, step)` can be legitimately missing (the latest cycle isn't
published yet, or a model doesn't carry every step on every cycle).
`download(errors=...)` governs that:

| `errors=` | behaviour |
|-----------|-----------|
| `"warn"` (default) | log the miss, return the COGs that succeeded |
| `"skip"` | drop the miss silently |
| `"raise"` | abort the whole download on the first miss |

## Ensemble members

Ensemble models (`gefs`, `ens`) expose a `members=` axis parallel to `steps=`:

```python
lens = EarthLens(
    data_source="nwp",
    variables={"gefs": ["temperature_2m"]},
    start="2024-06-01", end="2024-06-01",
    lat_lim=[40, 45], lon_lim=[-80, -75], path="out/gefs",
    members=["mean", "1", "2", "3"],     # mean + 3 perturbations
)
```

One COG is written per `(cycle, step, member)` (filename suffix `_m{member}`).
Without `members=`, only the model's **default member** is fetched (`mean` for
GEFS, `control` for ENS) — keeping a plain request bounded. GEFS members map to
Herbie's `member=` (`"0"`–`"30"`, `"mean"`); ENS members map to ECMWF
`type=pf` + `number=` (`"control"` keeps the control forecast). Deterministic
models ignore `members=`. DWD/Météo-France ensembles (ICON-EPS, PEARP/PEAROME)
are a follow-on.

## Cloud mirror selection

`mirror=` chooses where the bytes come from:

| `mirror=` | NOAA (Herbie) | ECMWF Open Data |
|-----------|---------------|-----------------|
| `"auto"` (default) | the catalog `mirrors:` order | first known catalog mirror |
| `"aws"` | `aws` | `aws` |
| `"gcp"` | `google` | falls back to `ecmwf` |
| `"azure"` | `azure` | `azure` |
| `"origin"` | `nomads` | `ecmwf` |

## Output: bbox-cropped COGs

Each `(cycle, step)` yields one Cloud-Optimized GeoTIFF named
`{model}_{YYYYMMDDHH}_f{step:03d}.tif` in `path`. The download fetches
the **variable subset** GRIB2 (the bandwidth win), reads it with
`pyramids.grib.open_grib`, crops it to your bbox (global 0–360° grids
are shifted to −180..180 when the bbox reaches into negative
longitudes), and writes the COG.

!!! note "ICON-global grid"
    DWD's native ICON-global files are on an **icosahedral** grid, which
    does not crop as a regular lat/lon raster. The download path is
    correct, but for a croppable COG use a regular-lat/lon ICON product.

!!! note "Antimeridian"
    A bounding box must satisfy `longitude_min <= longitude_max`, so a
    box that crosses the 180°/−180° dateline (e.g. `lon_lim=[170, -170]`)
    cannot be expressed in a single request. Global 0–360° grids are
    shifted to −180..180 when the bbox reaches negative longitudes, but a
    true dateline-spanning AOI is not split — issue two requests (one per
    side) and mosaic the results.

## Aggregating the forecast stack

Pass `aggregate=` to reduce the per-`(cycle, step)` COGs into windowed
composites (by **valid time** = `cycle + step`):

```python
from earthlens.aggregate import AggregationConfig

lens = EarthLens(
    data_source="nwp",
    variables={"gfs": ["temperature_2m"]},
    start="2024-06-01", end="2024-06-07",
    lat_lim=[40, 45], lon_lim=[-80, -75], path="out/gfs",
    steps=[0, 6, 12, 18],
)
paths = lens.download(
    aggregate=AggregationConfig(freq="1D", op="mean"),  # daily means
)
```

Aggregation requires a **single model** per request — different models
have different native grids and cannot be co-registered into one stack.
Issue one request per model.

!!! warning "Accumulated fields"
    `precipitation_acc` (and other `*_acc` fields) are **accumulations**
    over a step-dependent window, not instantaneous values. Reducing
    them across forecast steps by valid time (`mean`/`sum`) mixes
    accumulation intervals and can give misleading totals — the backend
    logs a warning when you do. Prefer the per-`(cycle, step)` COGs, or
    de-accumulate before aggregating.
