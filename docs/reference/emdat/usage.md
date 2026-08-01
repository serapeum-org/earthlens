# EM-DAT — usage

This page is the hands-on walkthrough. For what EM-DAT is, which product you are getting and why the licences
matter, read [Introduction](introduction.md) first; the rendered API is on the [Reference](emdat.md) page.

## Install

The `emdat:events` source is plain HTTP and needs **no extra**. The GDIS sources fetch from NASA Earthdata Cloud
through `earthaccess`:

```bash
pip install earthlens[emdat]
```

The requirement is `earthaccess >=0.17.0` with no Python marker: 0.18 raised its floor to Python 3.12, but 0.17
supports 3.11 with the same API, so both GDIS sources work on every Python earthlens supports.

## The three dataset ids

Exactly one id goes in `variables` — `OUTPUT_KIND` is per instance, so one instance serves one dataset.

| `variables=` | Returns | Auth | Download | Coverage |
|---|---|---|---|---|
| `["emdat:events"]` | `pandas.DataFrame` | none | 8 MB | 1900– , natural + technological |
| `["gdis:points"]` | `FeatureCollection` (points) | Earthdata Login | 1.09 MB | 1960–2018, natural only |
| `["gdis:polygons"]` | `FeatureCollection` (polygons) | Earthdata Login | **2.2 GB** | 1960–2018, natural only |

## Event impacts (no credentials)

```python
from earthlens.core import EarthLens

events = EarthLens(
    "emdat",
    variables=["emdat:events"],
    start="2000-01-01",
    end="2010-12-31",
    hazard="flood",
    country="BGD",
    path="out",
).download()

events[["DisNo.", "Start Year", "Total Deaths", "Total Affected"]].head()
```

That returns every flood EM-DAT recorded in Bangladesh over those eleven years — a couple of dozen rows, though
the exact count moves as CRED re-cuts the archive — and writes them to `out/emdat_events.csv`. The fetched archive workbook also lands in `out/`, and a second call reuses it
rather than re-downloading.

Every `emdat:events` download raises a `LicenseWarning`. That is deliberate: it names the eligibility restriction
in the Terms of Use, which is easy to miss and is stricter than "CC-BY-NC-ND" implies. Read it once, then filter
it if you have confirmed you are eligible:

```python
import warnings
from earthlens.biodiversity import LicenseWarning

warnings.filterwarnings("ignore", category=LicenseWarning)
```

## Geocoded locations (Earthdata Login)

```python
footprints = EarthLens(
    "emdat",
    variables=["gdis:points"],
    start="1990-01-01",
    end="2000-12-31",
    hazard="flood",
    lat_lim=[20.5, 26.7],
    lon_lim=[88.0, 92.7],
    path="out",
).download()

footprints.plot(markersize=4)
```

For real admin-unit geometry rather than centroids, swap in `variables=["gdis:polygons"]` — and expect the 2.2 GB
fetch the first time.

## Filtering

All four filters are optional. They work the same way on every source, with one difference in `hazard=`.

- **`hazard=`** — one type or a list, matched case- and whitespace-insensitively. **The vocabulary is per
  source**, because the two do not carry the same types:
    - `gdis:*` accepts the eight values GDIS geocoded — `drought`, `earthquake`, `extreme temperature`, `flood`,
      `landslide`, `mass movement (dry)`, `storm`, `volcanic activity`.
    - `emdat:events` accepts EM-DAT's fuller `Disaster Type` list, which adds `wildfire`, `epidemic`,
      `infestation`, `glacial lake outburst flood` and the whole technological group (`oil spill`, `road`,
      `rail`, `gas leak`, `industrial accident (general)`, …). It has no `landslide` — EM-DAT files those under
      mass movement.

  An unknown value fails at construction with a did-you-mean hint naming the dataset, rather than silently
  returning nothing. (The shipped GeoPackage spells one value `"extreme temperature "` with a trailing space; you
  never have to care — pass the canonical name.)
- **`country=`** — an ISO3 code, any casing. A value that is not three letters is rejected, so a typo fails
  loudly instead of looking like an empty result.
- **`start=` / `end=`** — only the *year* is significant; both sources are indexed by event year. Either bound may
  be omitted for an open-ended window.
- **`lat_lim=` / `lon_lim=`** — a bounding box. Omit both for a world-wide request, which applies no spatial
  filter at all, so events with no recorded coordinates are kept rather than silently dropped.

For `gdis:polygons` the hazard, country and bbox filters are pushed down into the driver, which is what makes a
6.3 GB GeoPackage usable — a bbox-plus-hazard query against the real file returns in about a second. The date
window is the exception: the GeoPackage has no date column, so the year is recovered from the `disasterno` prefix
in memory after the read.

## Joining the two sources

EM-DAT's `DisNo.` (`2009-0631-BGD`) is GDIS's `disasterno` (`2009-0631`) with the ISO3 suffix dropped:

```python
events["disasterno"] = events["DisNo."].str.rsplit("-", n=1).str[0]
joined = footprints.merge(events, on="disasterno", how="inner")
```

The [example notebook](../../examples/emdat/flood_events_and_footprints.ipynb) works this through end to end.

## Authentication

Only `gdis:*` needs credentials. Any of these work, in this order:

1. `username=` / `password=`, or `token=`, passed to the constructor.
2. `EARTHDATA_TOKEN`, or `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD`.
3. A `machine urs.earthdata.nasa.gov` entry in `~/.netrc`.
4. An interactive prompt.

Register free at <https://urs.earthdata.nasa.gov/users/new>.

**Authenticating is not sufficient on its own.** The SEDAC collection carries a data-use agreement, and until the
account accepts it the download fails with `401 Be sure to agree to the EULA` even though the login succeeded.
Accept any outstanding agreements once at
<https://urs.earthdata.nasa.gov/users/earthaccess/unaccepted_eulas>.

## Aggregation

`aggregate=` is refused for every dataset. These are event records and pre-geocoded locations, not gridded
rasters, so there is no meaningful gridded reduction — aggregate the returned `DataFrame` or `FeatureCollection`
with pandas instead.
