# FLODIS usage

This page walks through fetching the FLODIS impact tables and joining them to the flood **footprints** earthlens
already ships. Background is on the [Introduction](introduction.md) page; the rendered API is
[Reference](flodis.md).

## Fetch the damages table

`dataset="damages"` is the default. Filter by `country=` (ISO3) and a `[start, end]` year window:

```python
from earthlens.core import EarthLens

damages = EarthLens(
    "flodis",
    dataset="damages",
    country="MOZ",
    start="2000",
    end="2018",
).download()

damages[["ISO3", "year", "disasterno", "total_deaths", "total_damages_(000_USD)", "GFD_matches"]].head()
```

`download()` returns a `pandas.DataFrame` — one row per EM-DAT flood event that FLODIS matched to a Global Flood
Database footprint — and also writes it as a CSV under your output directory. Each row keeps the EM-DAT
**`disasterno`** key plus the per-event exposure sums (`pop_affected_sum_GHSL`, `GDP_affected_sum`, the
infrastructure counts).

## Fetch the displacement table

`dataset="displacement"` returns the IDMC table, keyed on the GADM **`GID_1`** / **`GID_2`** admin codes. Filter it
by `country=` and, optionally, `gid=` (a GADM code matched against either level):

```python
displacement = EarthLens(
    "flodis",
    dataset="displacement",
    country="MOZ",
    gid="MOZ.1_1",
).download()

displacement[["ISO3", "year", "displacements", "GID_1", "GID_2", "num_provinces"]].head()
```

## Join the footprints from the layers earthlens already ships

FLODIS carries the **keys** to the footprints, not the geometry. Attach the footprints from the shipped backends:

**GDIS disaster geometry** (joined on `disasterno`) comes from the [`emdat`](../emdat/introduction.md) backend:

```python
gdis = EarthLens("emdat", variables=["gdis:points"], country="MOZ").download()  # a FeatureCollection

# `disasterno` is the shared key. GDIS carries it as `disasterno`; FLODIS damages as `disasterno`.
merged = gdis.to_dataframe().merge(damages, on="disasterno", how="inner", suffixes=("_gdis", "_flodis"))
```

**Global Flood Database extents** come from the [`gee`](../google-earth-engine/introduction.md) backend
(`GLOBAL_FLOOD_DB/MODIS_EVENTS/V1`). FLODIS records which GFD events it matched in `GFD_matches`, so you fetch the
matching footprints and overlay them on the impact rows:

```python
gfd = EarthLens(
    "gee",
    dataset="GLOBAL_FLOOD_DB/MODIS_EVENTS/V1",
    start="2000",
    end="2018",
    aoi=(32.0, -26.0, 41.0, -10.0),  # Mozambique bbox
).download()  # the GFD flood-extent rasters for the window
```

The three layers share time and place, so a mapped example is: FLODIS gives the **impact magnitude**, GDIS gives the
**where** (admin geometry), and GFD gives the **observed flood extent** — the footprint that caused the impact.

## What FLODIS does not do

- **No live coverage.** FLODIS is 2000–2018. For current events, reach for the raw [`emdat`](../emdat/introduction.md)
  or [`gdacs`](../gdacs/introduction.md) feeds.
- **No bounding-box row filter.** The tables carry no per-row coordinates; a bbox restricts the `emdat` / `gee`
  layers you join to, not the FLODIS table. Filter FLODIS by `country=` / `gid=` / the year window.
- **No exposure normalisation.** FLODIS reports the affected sums as published; comparing impacts fairly across
  eras is the paper's derived method, not a raw column.

## Aggregation

`flodis` is tabular, so it takes no `aggregate=`:

```python
EarthLens("flodis", dataset="damages", aggregate=object()).download()
# NotImplementedError: aggregate= is not supported by FLODIS ...
```
