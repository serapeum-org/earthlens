# Tropycal — usage

Copy-paste recipes for the `tropycal` backend. See
[Introduction](introduction.md) for coverage and limits, and
[API reference](tropycal.md) for the full surface.

## Basic query (point mode)

One basin, a date window, and a bounding box. `variables` selects the
**basin**; the result is one feature per 6-hourly fix.

```python
from earthlens import EarthLens

el = EarthLens(
    data_source="tropycal",
    variables=["north_atlantic"],   # basin code(s), not data variables
    start="2005-08-01",
    end="2005-09-15",
    lat_lim=[18.0, 31.0],           # Gulf of Mexico
    lon_lim=[-98.0, -80.0],
    source="hurdat",                # "ibtracs" (default) | "hurdat"
    path="out/tropycal",
)
fc = el.download()                  # a pyramids FeatureCollection (GeoDataFrame)
print(len(fc), "fixes")
print(fc[["storm_id", "name", "time", "vmax_kt", "category"]].head())
```

The fix-level filter is "loose": a storm contributes only the fixes that fall
**inside both** the `[start, end]` window and the bounding box.

## Track mode (one LineString per storm)

```python
el = EarthLens(
    data_source="tropycal",
    variables=["north_atlantic"],
    start="2005-08-01",
    end="2005-12-01",
    lat_lim=[-90, 90],
    lon_lim=[-180, 180],
    source="hurdat",
    geometry="track",               # one LineString per storm
    path="out/tropycal",
)
tracks = el.download()
print(tracks[["name", "max_vmax_kt", "min_mslp_hpa", "max_category", "ace"]])
```

In track mode a storm is included when **any** of its fixes falls in the
window + bbox, and the rendered `LineString` is built from its **in-window**
fixes (the drawn track is clipped to the time window).

## Filtering by intensity or storm type

```python
el = EarthLens(
    data_source="tropycal",
    variables=["east_pacific"],
    start="2018-06-01",
    end="2018-11-30",
    lat_lim=[5, 30],
    lon_lim=[-140, -90],
    source="ibtracs",
    min_category=3,                 # keep only fixes at Saffir-Simpson 3+
    storm_type="HU",                # keep only hurricane-type fixes
    path="out/tropycal",
)
majors = el.download()
```

`min_category` / `storm_type` filter at the **fix** level. In
`geometry="track"` mode that means the filter is applied *before* the
`LineString` is built, so a storm that only briefly meets the threshold yields
a track drawn from just its qualifying fixes — a clipped, possibly shortened
path rather than the storm's whole track.

## Multiple basins

```python
el = EarthLens(
    data_source="tropycal",
    variables=["north_atlantic", "east_pacific"],
    start="2020-01-01",
    end="2020-12-31",
    lat_lim=[-90, 90],
    lon_lim=[-180, 180],
    source="hurdat",
    path="out/tropycal",
)
fc = el.download()   # union across both basins; one file written per basin
```

Each basin's `TrackDataset` is loaded once and reused; the first load of a
basin is slow (it downloads the best-track file).

## Output format

`download()` returns the in-memory `FeatureCollection` and also writes one
vector file per basin under `path`, named `tropycal_<basin>_<geometry>.<ext>`.
Choose the format with `file_format`:

```python
el = EarthLens(
    data_source="tropycal",
    variables=["north_atlantic"],
    start="2005-08-01", end="2005-09-15",
    lat_lim=[18, 31], lon_lim=[-98, -80],
    source="hurdat",
    file_format="geojson",          # "gpkg" (default) | "geojson"
    path="out/tropycal",
)
el.download()
```

## Other products (`product=`)

Beyond best tracks, the `product=` selector fetches other tropycal families
through the same `data_source="tropycal"` backend (see the
[Introduction](introduction.md) products table):

```python
# recon — aircraft observations for a storm (vector points)
EarthLens(data_source="tropycal", product="recon", variables=["AL122005"],
          basin="north_atlantic", source="hurdat", start="2005-08-23",
          end="2005-08-31", lat_lim=[18, 31], lon_lim=[-98, -80],
          path="out").download()

# ships — SHIPS intensity-forecast guidance (tabular DataFrame)
EarthLens(data_source="tropycal", product="ships", variables=["AL092022"],
          basin="north_atlantic", source="hurdat", ships_time="2022-09-27 00:00",
          start="2022-09-20", end="2022-10-01", lat_lim=[-90, 90],
          lon_lim=[-180, 180], path="out").download()   # -> pandas DataFrame

# realtime — live active storms (vector; empty variables = all active)
EarthLens(data_source="tropycal", product="realtime", variables=[],
          start="2026-01-01", end="2026-12-31", lat_lim=[-90, 90],
          lon_lim=[-180, 180], path="out").download()
```

`recon`/`ships` need a storm with aircraft data / archived SHIPS guidance;
`realtime` returns data only while storms are active.

## Notes

- **`aggregate=` is rejected.** Tropycal output is vector or tabular, never
  gridded, so passing `aggregate=` raises `NotImplementedError`. Post-process
  the returned `GeoDataFrame` / `DataFrame` directly instead.
- **First-load cost.** Expect a few seconds for HURDAT and longer for IBTrACS
  on the first query of a basin; subsequent queries in the same process reuse
  the cached `TrackDataset`.
