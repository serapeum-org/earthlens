# Climate indices — introduction

<img src="../../_images/logos/climate_indices.svg" alt="NOAA Physical Sciences Laboratory logo" height="60">

earthlens ships a single `climate-indices` backend that fetches **monthly
climate / teleconnection index series** — ENSO/ONI, NAO, AO, PDO, AMO,
SOI, PNA and friends — from two open ASCII sources and returns them as a
tidy long-format `pandas.DataFrame`. These indices are **global scalar
monthly series** (one number per month, no geometry), so the backend is
tabular, not raster: there is no bounding box and no grid.

Two open sources are covered:

- **[NOAA PSL](https://psl.noaa.gov/data/climateindices/)** — the
  Physical Sciences Laboratory's classic `correlation/<index>.data`
  series (ONI, NAO, AO, PDO, SOI, PNA, Niño 3.4, …).
- **[KNMI Climate Explorer](https://climexp.knmi.nl)** — the `<id>.dat`
  monthly grid series (e.g. the detrended AMO).

This page orients the backend. For the hands-on walkthrough see
[Usage](usage.md) and the shipped index ids on the
[Available indices](datasets.md) page; the rendered API is the
[Reference](climate_indices.md) page.

## How it works

Each requested index id selects a catalogue row that pins its source, URL
and ASCII **dialect**. The backend downloads the small text file with a
plain `requests` GET, parses the source's dialect into a tidy
`(date, value)` frame, stamps the `index` and `source` columns, filters to
the requested `[start, end]` window, and concatenates every requested
index into one long-format frame:

```python
from earthlens.earthlens import EarthLens

df = EarthLens(
    data_source="climate-indices",
    variables=["oni", "nao"],
    start="1990-01-01",
    end="2020-12-31",
).download()

df.columns.tolist()   # ['date', 'index', 'value', 'source']
sorted(df["index"].unique())   # ['nao', 'oni']
```

Two ASCII dialects are parsed:

- **`psl`** — a `<first_year> <last_year>` header, one `year jan..dec`
  row per year, then a lone missing-value sentinel line (it varies per
  file — `-99.9`, `-99.99`, `-9.99`, …) and a free-text provenance footer.
- **`climexp`** — `#`-prefixed comment lines, then `year jan..dec` rows
  (a trailing annual-mean column, when present, is dropped); the sentinel
  is `-999.9`.

The missing-value sentinel maps to `NaN` (kept, not dropped, so gaps stay
visible). The parse is pure text → pandas — no gridded-array dependency is
imported.

## Three things that shape the backend

- **The series are global scalars — there is no geometry.** Spatial
  arguments (`lat_lim` / `lon_lim` / `aoi`) are accepted for signature
  parity with the other backends but **ignored**; there is no bbox subset.

- **There is nothing to grid-reduce.** The values are already monthly
  scalars, so the `EarthLens` facade rejects a non-`None` `aggregate=` for
  this tabular backend with `NotImplementedError`. Post-process the
  returned DataFrame directly for any rollup you need.

- **One canonical source per index.** Where an index exists on both
  sources, the catalogue picks one canonical source (see
  [Available indices](datasets.md)); a per-request source override is a
  future addition.

## Attribution

Both sources are open; please cite them. `download()` logs the citation of
each source used once, as an info line:

- **NOAA PSL** — NOAA Physical Sciences Laboratory, Boulder, Colorado, USA
  (<https://psl.noaa.gov/data/climateindices/>).
- **KNMI Climate Explorer** — Royal Netherlands Meteorological Institute
  (<https://climexp.knmi.nl>).
