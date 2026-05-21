# GDACS multi-hazard alerts — usage

This page walks through fetching disaster alerts with the `gdacs`
backend. For background and the hazard list see
[Introduction](introduction.md); the rendered API is the
[Reference](gdacs.md) page.

## Install

Nothing extra to install — GDACS uses only core dependencies
(`requests` + pyramids), so the base package is enough:

```bash
pip install earthlens
```

There is no `[gdacs]` extra and no credentials to configure.

## Quickstart — recent global earthquake alerts

```python
from earthlens import EarthLens

alerts = EarthLens(
    variables=["EQ"],            # the hazard type(s) to query — see below
    data_source="gdacs",
    start="2024-01-01",
    end="2024-01-31",
    lat_lim=[-90, 90],
    lon_lim=[-180, 180],
    path="./out",
).download()

print(len(alerts), "alerts")
print(alerts[["name", "alert_level", "from_date", "geometry"]].head())
```

`download()` returns a pyramids `FeatureCollection` (a
`geopandas.GeoDataFrame` subclass), so every pandas / geopandas method
works on it directly. It also writes `out/gdacs_alerts.gpkg`.

## Choosing the hazard type(s) — `variables`

For this backend `variables` is the list of GDACS **hazard types**, not
data-variable names. This is an intentional, documented overload: the
`EarthLens` facade makes `variables` a required argument on every call,
so adding a separate `hazards=` keyword would only force a redundant
placeholder. Pass one or more hazard codes:

```python
# one hazard type
EarthLens(variables=["EQ"], data_source="gdacs", ...)

# several — all come back in one request, in one FeatureCollection
EarthLens(variables=["EQ", "TC", "FL"], data_source="gdacs", ...)

# an empty list defaults to all six hazard types
EarthLens(variables=[], data_source="gdacs", ...)
```

Valid codes are `EQ`, `TC`, `FL`, `VO`, `WF`, `DR`. An unknown code
raises with a did-you-mean hint
(`Catalog().get_hazard("EQK")` → *Did you mean 'EQ'?*).

## Filtering the query

| Keyword | Meaning | Default |
|---|---|---|
| `alert_level` | list of `"Green"` / `"Orange"` / `"Red"` to keep | all three |
| `file_format` | `"gpkg"` or `"geojson"` | `"gpkg"` |
| `timeout` | per-request timeout, seconds | `60.0` |

The alert-level filter is the explicit `alert_level=` keyword (it is
*not* part of `variables`):

```python
# only the most serious alerts, all hazard types, for one month
EarthLens(
    variables=[],                 # all six hazard types
    data_source="gdacs",
    start="2024-09-01",
    end="2024-09-30",
    lat_lim=[-90, 90],
    lon_lim=[-180, 180],
    alert_level=["Orange", "Red"],
    path="./out",
).download()
```

The spatial window comes from `lat_lim` / `lon_lim` and the temporal
window from `start` / `end`. GDACS issues one query spanning the whole
`[start, end]` window — it does not chunk by day or month — so
`temporal_resolution` is irrelevant here (it carries the sentinel
`"all"`). GDACS has no server-side bounding-box filter, so the backend
fetches the window's alerts and **clips them to your
`lat_lim` / `lon_lim` client-side**.

## The returned FeatureCollection

CRS `EPSG:4326`, one row per alert. Columns: `event_id`, `episode_id`,
`hazard_type`, `name`, `alert_level`, `alert_score`, `from_date` (UTC),
`to_date` (UTC), `country`, `iso3`, `glide`, `severity`,
`severity_unit`, `severity_text`, `geometry`. An empty result (a quiet
window, or a box with no alerts) is returned as an empty
FeatureCollection with exactly these columns — not an error — so
downstream `concat` / `to_file` never breaks. A renamed or missing field
in the upstream feed degrades to a null cell rather than raising.

### Plotting

```python
import matplotlib.pyplot as plt

colours = {"Green": "green", "Orange": "orange", "Red": "red"}
alerts = EarthLens(variables=[], data_source="gdacs",
                   start="2024-01-01", end="2024-03-31",
                   lat_lim=[-90, 90], lon_lim=[-180, 180],
                   path="./out").download()

ax = alerts.plot(color=alerts["alert_level"].map(colours), markersize=20, alpha=0.6)
ax.set_title("GDACS alerts, Q1 2024 (coloured by alert level)")
plt.show()
```

### Writing to disk

`download()` writes `gdacs_alerts.gpkg` (or `.geojson`) automatically.
To write the collection yourself:

```python
alerts.to_file("alerts.gpkg", driver="GPKG")       # GeoPackage
alerts.to_file("alerts.geojson", driver="GeoJSON")  # GeoJSON
```

### Cross-referencing with GLIDE

The `glide` column carries the GLIDE disaster identifier where GDACS has
assigned one — a stable key shared with
[ReliefWeb](https://reliefweb.int/) and
[Copernicus EMS](https://emergency.copernicus.eu/). Filter to alerts
that carry one to join against those sources:

```python
with_glide = alerts[alerts["glide"].notna() & (alerts["glide"] != "")]
```

## Aggregation is not supported

GDACS output is vector, so the `aggregate=` argument is rejected:

```python
EarthLens(variables=["EQ"], data_source="gdacs", ...).download(aggregate=cfg)
# NotImplementedError: aggregate= is not supported ... (OUTPUT_KIND='vector')
```

The aggregator only reduces gridded raster outputs. Post-process the
returned FeatureCollection directly instead (it is a GeoDataFrame).

## A note on data quality

GDACS is an impact-**alert** feed, not an authoritative scientific
catalog (see [Introduction](introduction.md)). For rigorous seismic
data use the `fdsn` backend instead of GDACS's earthquake alerts.
