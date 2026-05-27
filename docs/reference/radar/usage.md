# NEXRAD radar — usage

## Install

```bash
pip install earthlens[radar]      # boto3 (unsigned S3); no credentials needed
```

`pyart` (for reading/gridding the assembled `.ar2v` volumes) is **not**
required to fetch — it's a downstream follow-on.

## Request

`variables` maps **WSR-88D site id → an (advisory) moment list** — a
Level-II volume carries every moment, so the whole volume is fetched and
the list value is informational:

```python
from earthlens.earthlens import EarthLens

lens = EarthLens(
    data_source="radar",                    # alias: "nexrad"
    variables={"KTLX": ["reflectivity"]},   # Oklahoma City
    start="2024-06-01T18:00:00",            # ISO datetime (sub-hourly feed)
    end="2024-06-01T19:00:00",
    lat_lim=[33, 37],
    lon_lim=[-100, -95],
    path="out/radar",
)
inventory = lens.download()   # GeoDataFrame of assembled volumes
```

- **Stations** — keys of `variables`. Any valid four-letter id works;
  the bundled station catalog provides geometry + bbox discovery for
  the full ~210-site WSR-88D network (a site absent from the catalog still fetches, just
  with no point geometry). `StationCatalog().in_bbox(w, s, e, n)` lists
  the catalogued radars in a box.
- **Time window** — `start` / `end` filter volumes by scan-start time.
  Because the feed is a rolling buffer, the window must be **recent**
  (~last hour); older windows return an empty inventory.
- **`fmt`** — defaults to `"%Y-%m-%dT%H:%M:%S"` (the feed is sub-hourly).

## Output

`download()` returns a `GeoDataFrame` with one row per assembled volume:

| column | meaning |
|--------|---------|
| `station_id` | WSR-88D site id |
| `volume` | rolling volume number |
| `scan_time` | volume scan-start (UTC) |
| `n_chunks` | chunks concatenated |
| `path` | local `.ar2v` file |
| `geometry` | station point (EPSG:4326), or `None` if uncatalogued |

The assembled `.ar2v` files are written to `path`; read them with
`pyart.io.read_nexrad_archive(...)`.

`aggregate=` is **not supported** (raw polar volumes aren't griddable by
the pyramids reducer — the facade rejects it for this `vector` backend).
A failed volume is skipped with a warning rather than aborting the batch.
