# NEXRAD radar — introduction

The `earthlens.radar` backend fetches **WSR-88D Level-II radar volumes**
from the NEXRAD network (the US national weather-radar system). Unlike a
gridded forecast, a Level-II volume is a stack of polar radar sweeps
(reflectivity, velocity, spectrum width, dual-pol moments) from a single
site — geophysical instrument data, so the backend's output is a
`vector` inventory, not a raster.

## Source: the real-time chunk feed

NEXRAD Level-II is published two ways on AWS:

| Bucket | Access | Coverage | Used here |
|--------|--------|----------|-----------|
| `noaa-nexrad-level2` | anonymous **GET only** (listing denied) | full archive (1991→) | ✗ — can't enumerate scans anonymously |
| `unidata-nexrad-level2-chunks` | anonymous **list + GET** | rolling buffer (~last hour) | ✓ |

So earthlens uses the **`unidata-nexrad-level2-chunks`** bucket. Each
volume is delivered as ordered chunks —

```
{STATION}/{VOLUME}/{YYYYMMDD}-{HHMMSS}-{CHUNK:03d}-{TYPE}
```

— one `S` (start, carries the `AR2V…` header), many `I` (intermediate),
and a final `E` (end). Concatenating a volume's chunks in chunk order
reconstructs a valid `.ar2v` Level-II file.

!!! note "Real-time only"
    The chunk bucket is a rolling buffer of roughly the last hour or two
    of volumes — not a historical archive. A request for an old date
    returns nothing. (Archival access needs the `noaa-nexrad-level2`
    bucket, which denies anonymous listing.)

## What the backend does

For each requested site + time window it lists the available volumes,
keeps those whose scan-start time falls in the window, downloads and
concatenates each volume's chunks into one `.ar2v` file, and returns a
`GeoDataFrame` cataloguing them (station, scan time, chunk count, local
path, station-point geometry). **Reading / gridding** the assembled
volumes (via `pyart`) is a downstream follow-on — this backend fetches
and inventories; it does not decode.

See [Usage](usage.md) for the request shape.
