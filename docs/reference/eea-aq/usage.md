# Using the EEA (`eea_aq`) backend

This page is the hands-on guide to the `earthlens` EEA backend — picking
pollutants, choosing countries, the reporting eras, and the output. For
background see the [Introduction](introduction.md); for the install see
[Authentication](authentication.md); the rendered API is on the
[Reference](eea-aq.md) page.

> **Needs the `[eea_aq]` extra.** This backend wraps the `airbase` SDK,
> imported lazily. Install it with `pip install earthlens[eea_aq]` (see
> [Authentication](authentication.md)). No credentials are required — the
> EEA download service is public.

## 1. A first query

```python
from earthlens import EarthLens

df = EarthLens(
    data_source="eea-aq",
    variables=["pm25"],
    start="2023-06-01",
    end="2023-06-30",
    country="MT",                  # Malta — small, quick to download
    lat_lim=[35.7, 36.1],          # used only if country= is omitted
    lon_lim=[14.1, 14.6],
    path="out/eea",
).download()

df.head()
```

`download()` returns the long-format `DataFrame` and also writes it to
`path`.

## 2. Selecting pollutants via `variables`

`variables` is the list of **pollutants**, resolved to airbase `poll`
notations through the bundled catalog:

```python
from earthlens.eea_aq import Catalog

sorted(Catalog().pollutants)        # ['co', 'no2', 'o3', 'pm10', 'pm25', 'so2']
Catalog().polls_for(["pm25", "o3"]) # ['PM2.5', 'O3']
```

## 3. Choosing countries — `country=` vs the bbox

The EEA service is queried per country. There are two ways to say which:

- **Explicit `country=`** — an ISO2 code or list (`"DE"`,
  `["DE", "FR"]`). Precise and fast; use this when you know the country.
- **From the bbox** — omit `country=` and the backend selects every EEA
  reporting country whose bounding box intersects `lat_lim` / `lon_lim`.
  Convenient, but coarse: a bbox clipping the corner of a large country
  pulls that **whole** country.

```python
from earthlens.eea_aq._helpers import countries_in_bbox
countries_in_bbox((50.8, 52.0), (4.0, 6.0))   # ['BE', 'DE', 'FR', 'NL']
```

Because the delivered Parquet has no coordinates, results are always
**country-granular** — every station in the chosen countries — and the
frame has no `lat` / `lon` columns.

## 4. Reporting eras and the date window

`start` / `end` set the inclusive window (parsed with `fmt`, default
`"%Y-%m-%d"`). The backend maps the requested years to the EEA
dataset(s) that span them (`Historical` 2002–2012, `Verified` 2013
onward, `Unverified` 2023+), downloads each, and filters the rows to the
exact window. A recent year is queried against **both** `Verified` and
`Unverified` (a year is promoted from the near-real-time stream into the
validated archive ~9 months after it ends), and the rows are
de-duplicated:

```python
from earthlens.eea_aq._helpers import datasets_for_years
datasets_for_years(2021, 2024)     # ['Verified', 'Unverified']
datasets_for_years(2015, 2016)     # ['Verified']
```

> **⚠ Download volume.** `airbase` has **no date filter** — a request
> downloads the *entire* reporting era for each country + pollutant, then
> the window is applied in memory. A one-day query for a large country can
> pull hundreds of MB to gigabytes. Keep it cheap: a small explicit
> `country=`, one pollutant, and the tightest year range; a recent year
> costs two eras.

## 5. Output format

`download()` writes the frame to `path` as CSV by default, or Parquet
with `file_format="parquet"`. The file is named
`eea_aq_<pollutants>_<start>_<end>.<ext>`.
