# ASF InSAR backend — usage

This page walks through the two request shapes the `asf` backend
supports: a **plain SAR catalog search** and an **InSAR baseline
stack** from a reference granule. Both run through the same
`EarthLens(data_source="asf", …)` facade; the mode is decided by
whether a `reference=` granule id is set.

## Install

```bash
pip install earthlens[asf]
```

Set credentials (only needed for the **download** step — search
runs anonymously):

```bash
export EARTHDATA_TOKEN="<your EDL bearer token>"
# or
export EARTHDATA_USERNAME="you"
export EARTHDATA_PASSWORD="••••"
# or place a `urs.earthdata.nasa.gov` entry in ~/.netrc
```

## Plain SAR search

Find every Sentinel-1 SLC over a bbox and time window:

```python
from earthlens.core import EarthLens

el = EarthLens(
    data_source="asf",
    variables=["sentinel-1-slc"],   # one curated product key per call
    start="2024-06-01",
    end="2024-06-30",
    lat_lim=[63.8, 64.4],          # Iceland, central rift zone
    lon_lim=[-21.7, -21.0],
    path="asf_out",
    flight_direction="ASCENDING",   # ASF refinement kwargs
    beam_mode="IW",
    polarization="VV+VH",
    max_results=20,
)
paths = el.download()
```

`download()` returns a `list[Path]`. The first call downloads every
matching product into `asf_out/`; a second call returns the same
list without re-downloading (the backend skips files already on
disk).

### Refinement kwargs

| Backend kwarg | ASF search field | Meaning |
|---|---|---|
| `beam_mode` | `beamMode` | e.g. `"IW"` (Sentinel-1 Interferometric Wide) |
| `flight_direction` | `flightDirection` | `"ASCENDING"` / `"DESCENDING"` |
| `polarization` | `polarization` | `"VV"`, `"VV+VH"`, `"HH"`, … |
| `max_results` | `maxResults` | cap on the number of hits |

## InSAR baseline stack

Build the coregistered stack from a reference SLC, windowed on the
perpendicular and temporal baselines:

```python
el = EarthLens(
    data_source="asf:insar",             # topic key for "asf"
    variables=["sentinel-1-slc"],    # must be a stackable product
    reference="S1A_IW_SLC__1SDV_20240601T072115_20240601T072143_054132_06960B_6FE8",
    perpendicular_baseline=(-100.0, 100.0),  # metres, (min, max)
    temporal_baseline=(0, 60),               # whole days, (min, max)
    start="2024-01-01",   # search-mode dates; ignored in stack mode
    end="2024-12-31",
    path="insar_stack",
)
paths = el.download()
```

- `reference=` switches the backend into stack mode.
- The full reference-frame stack is fetched first
  (`ASFProduct.stack()` returns every acquisition that shares the
  reference scene's footprint and orbit).
- `perpendicular_baseline=(min_m, max_m)` and
  `temporal_baseline=(min_days, max_days)` are then applied as a
  **client-side post-filter** on the returned stack. Empirically,
  passing these as `ASFSearchOptions` makes the SDK's internal
  search return zero products before the baseline calculator runs,
  so the post-filter is the only working path.

The bbox is **not** required in stack mode — the reference granule
defines the area of interest. The two date kwargs (`start` / `end`)
are still required by the facade but are advisory; the actual
acquisitions returned are decided by the baseline windows.

A stack mode call on a non-stackable product (`opera-rtc-s1`,
`sentinel-1-grd`, `aria-s1-gunw`) raises `ValueError` at
construction:

```python
>>> EarthLens(data_source="asf",
...           variables=["opera-rtc-s1"],
...           reference="OPERA_RTC_...",
...           start="2024-01-01", end="2024-12-31",
...           path="out")  # doctest: +SKIP
Traceback (most recent call last):
    ...
ValueError: 'opera-rtc-s1' is not InSAR-stackable; ...
```

## Picking a product

| Product key | Use it when | `stackable` |
|---|---|---|
| `sentinel-1-slc` | C-band InSAR over land; the default InSAR input | yes |
| `sentinel-1-burst` | only one burst is needed (smaller download) | yes |
| `alos-palsar-slc` | L-band InSAR for forest / agriculture; pre-2011 archive | yes |
| `opera-cslc-s1` | OPERA-coregistered S1 SLC (NASA-preprocessed) | yes |
| `sentinel-1-grd` | amplitude-only change detection (no phase) | no |
| `opera-rtc-s1` | terrain-corrected backscatter (analysis-ready) | no |
| `aria-s1-gunw` | precomputed unwrapped interferograms | no |
| `nisar-rslc` | L+S-band SLC (data starts arriving post-launch) | yes |

Aliases work — `variables=["s1-slc"]`, `variables=["opera-rtc"]`,
etc. The catalog raises with a did-you-mean hint on an unknown name.

## Why no `aggregate=`?

The MVP returns SAR product paths and does **not** perform any
in-flight processing. An SLC carries complex-valued (I/Q) samples
that are meaningless after a plain bbox crop, and processed
products (RTC, GUNW) are already gridded outputs from the InSAR
pipeline — a temporal "mean across the stack" would not produce a
useful artefact.

```python
>>> from earthlens.core import EarthLens
>>> from earthlens.aggregate import AggregationConfig
>>> el = EarthLens(data_source="asf",
...                variables=["sentinel-1-slc"],
...                lat_lim=[0.0, 1.0], lon_lim=[0.0, 1.0],
...                start="2024-01-01", end="2024-01-31",
...                path="out")  # doctest: +SKIP
>>> el.download(aggregate=AggregationConfig(...))  # doctest: +SKIP
Traceback (most recent call last):
    ...
NotImplementedError: ASF returns SAR products for downstream InSAR/RTC tooling; ...
```

For downstream processing, hand the downloaded stack to
[HyP3](https://hyp3-docs.asf.alaska.edu/),
[SNAP](https://step.esa.int/main/toolboxes/snap/),
[ISCE2/3](https://github.com/isce-framework/isce2), or
[MintPy](https://mintpy.readthedocs.io/).

## Cross-link: ASF vs `earthdata`

For plain "find granule X and pull it" use either backend; the
`earthdata` backend is more direct for a single named granule. For
the InSAR baseline stack, `asf` is the only path —
`earthlens.earthdata` cannot reach `ASFProduct.stack()`. The two
backends share the same EDL auth handle, so a credential change
takes effect for both.
