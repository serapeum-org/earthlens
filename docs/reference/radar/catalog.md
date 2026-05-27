# NEXRAD radar — catalog & tooling

## Station catalog

`earthlens.radar` ships a station registry, `radar_data_catalog.yaml`,
loaded by `StationCatalog`. It maps each four-letter WSR-88D `ICAO` id
(`KTLX`) to its name, latitude, longitude, and US state — giving every
fetched volume a point geometry and letting a request discover the
radars inside a bounding box.

The catalog is **informational**: any valid four-letter site id can be
fetched even if it is absent from the catalog (the assembled volume just
gets no point geometry). It currently holds **210** sites — the full
NOAA WSR-88D network.

```python
from earthlens.radar import StationCatalog

cat = StationCatalog()
len(cat.datasets)                       # 210
cat.get_station("KTLX")                 # Station(name='Oklahoma City', ...)
cat.in_bbox(-100, 33, -95, 37)          # ['KFDR', 'KINX', 'KTLX', 'KVNX', ...]
```

`get_station` raises a `ValueError` with a did-you-mean hint for an
unknown id, matching the other backends' catalogs.

## Tooling

Two scripts under `tools/radar/` keep the catalog honest:

| Tool | Purpose |
|------|---------|
| `refresh_radar_catalog.py` | Regenerate `radar_data_catalog.yaml` from NOAA HOMR's authoritative [`nexrad-stations.txt`](https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt) (fixed-width parse; ICAO / name / state / lat / lon). |
| `audit_radar_catalog.py` | Cross-check the catalog against the **live** chunk feed — reports `streaming` / `idle` / `uncatalogued` sites (a liveness snapshot, not a correctness check). |

```bash
pixi run -e dev python tools/radar/refresh_radar_catalog.py          # rewrite the catalog
pixi run -e dev python tools/radar/refresh_radar_catalog.py --dry-run
pixi run -e dev python tools/radar/audit_radar_catalog.py            # live coverage snapshot
```

Because the feed is a rolling ~1–2 h buffer, the audit's `idle` bucket is
expected to be non-empty at any instant (sites between scans or briefly
offline) — it is not a defect.
