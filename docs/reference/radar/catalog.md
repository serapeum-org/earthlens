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

Two `earthlens datasets` verbs keep the catalog honest:

| Command | Purpose |
|---------|---------|
| `refresh radar --write` | Regenerate `radar_data_catalog.yaml` from NOAA HOMR's authoritative [`nexrad-stations.txt`](https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt) (fixed-width parse; ICAO / name / state / lat / lon). |
| `validate radar --live` | Cross-check the catalog against the **live** chunk feed — flags an unreachable / empty feed, or an id-format mismatch (the feed is non-empty but no catalogued station is in it). |

```bash
earthlens datasets refresh radar           # diff the catalog against live HOMR
earthlens datasets refresh radar --write    # rewrite the catalog
earthlens datasets validate radar --live    # confirm the real-time feed is reachable
```

Because the feed is a rolling ~1–2 h buffer, per-station idleness is expected
at any instant (sites between scans or briefly offline) — so `validate
--live` only flags a fully empty feed or a wholesale id mismatch, not per-site
idleness.
