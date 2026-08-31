# FDSN seismic events — introduction

<img src="../../_images/logos/fdsn.png" alt="FDSN logo" height="60">

The [FDSN](https://www.fdsn.org/) (International Federation of Digital
Seismograph Networks) **event web service** is the common standard that
seismological data centres expose for querying earthquake (and other
seismic-event) catalogs. earthlens ships a single `fdsn` backend that
speaks this standard to six networks through one
[`obspy`](https://docs.obspy.org/) client — because they all answer the
identical FDSN-event protocol, one code path covers them all.

This page orients the backend. For the hands-on download walkthrough
see [Usage](usage.md); the rendered API is the [Reference](fdsn.md)
page.

## Why it matters here

Every other earthlens backend returns **gridded** data — a raster or a
NetCDF/Zarr array (CHC rainfall, ERA5, CMEMS ocean fields, GEE
imagery). FDSN is different in two ways that shape the backend:

- **The output is a vector table, not a grid.** A seismic-event query
  returns a list of point features — one row per event, each with a
  time, a location, a depth, a magnitude, and a `Point` geometry. So
  FDSN is the package's first **`vector`** backend
  (`FDSN.OUTPUT_KIND == "vector"`), and `download()` returns a
  [pyramids](https://github.com/serapeum-org/pyramids)
  `FeatureCollection` (a `geopandas.GeoDataFrame` subclass) rather than
  writing a raster. Because there is no meaningful gridded reduction of
  an event table, an `aggregate=` argument is refused with
  `NotImplementedError` — by the shared guard in
  `AbstractDataSource`, so a direct `FDSN(...).download(aggregate=...)`
  is rejected exactly like one made through the `EarthLens` facade.
  Opt-in, `with_shakemap=True` additionally writes each USGS event's
  gridded ShakeMap as a GeoTIFF. That does not change `OUTPUT_KIND`:
  `download()` still returns the event FeatureCollection, and the
  rasters are a side effect on disk. See
  [Usage](usage.md#shakemap-rasters-usgs-only).

- **There is no dataset catalog to curate.** FDSN is a *fixed query
  protocol*, not a 600-dataset archive. The entire "catalog" is a
  six-row provider-dispatch table mapping a user-facing network name
  to an `obspy` URL mapping. There is no growth task — adding another
  network later (ORFEUS, GFZ, …) is a hand-edit of one YAML row. The
  backend still ships `refresh` and `validate` handlers
  (`earthlens datasets refresh fdsn`, `… validate fdsn`); `refresh`
  diffs the data centres `obspy` can reach against the curated rows, so
  a centre `obspy` gains or drops is surfaced rather than missed.
  `audit fdsn` works too, derived from the refresher's drift axis rather
  than from an FDSN-owned handler. There is no `probe` handler.

## The networks

| Key (`variables=[...]`) | Network | Best for |
|---|---|---|
| `USGS` | USGS ComCat (United States Geological Survey) | the canonical **global** earthquake catalog; the default |
| `EMSC` | EMSC seismicportal | **European-Mediterranean** events; fast preliminary solutions |
| `INGV` | Istituto Nazionale di Geofisica e Vulcanologia | **Italy** + volcano monitoring; reports much smaller magnitudes |
| `EARTHSCOPE` | EarthScope (ex-IRIS DMC) | the global archive; `IRIS` is the legacy alias and still resolves |
| `ISC` | International Seismological Centre | the global **reviewed** bulletin (definitive, but lags real time) |
| `GEONET` | GeoNet | **New Zealand** seismic network |

Each is selected by passing its key in `variables` — for this backend
`variables` is the list of seismic **networks**, not data-variable
names (an intentional, documented overload; see [Usage](usage.md)).

## Authentication

**None for the common path.** All six networks expose *public* FDSN
event services, so a default query needs no credentials at all. The
single exception is EarthScope, whose restricted endpoints can take an
optional access token; earthlens resolves it from an explicit
`earthscope_token=` argument, the `EARTHSCOPE_TOKEN` environment
variable, or a `~/.earthscope_token` file, and only uses it for a
provider whose catalog row declares `needs_token: true`. None of the
bundled public networks do.

## What a query returns

One `FeatureCollection` (CRS `EPSG:4326`), unioned across the requested
networks, with these columns:

| Column | Type | Meaning |
|---|---|---|
| `event_id` | str | provider-side event identifier |
| `time` | datetime (UTC) | origin time |
| `longitude` / `latitude` | float | epicentre, degrees |
| `depth_km` | float | hypocentre depth in **kilometres** (obspy reports metres; converted here) |
| `magnitude` | float | preferred magnitude value |
| `magnitude_type` | str | e.g. `Mw`, `mb`, `Ml` |
| `event_type` | str | e.g. `earthquake`, `explosion` |
| `status` | str | origin evaluation status (`preliminary` / `reviewed` / …) |
| `provider` | str | the network key the row came from |
| `geometry` | Point | `shapely.Point(longitude, latitude)` |

As a side effect, `download()` also writes one vector file per network
to the output directory (GeoPackage by default, or GeoJSON).

## Cost

**Free.** The FDSN event services are public, operated by the
respective data centres; usage terms and recommended attribution are
documented per network (USGS ComCat, EMSC, INGV, EarthScope).

## References

- FDSN web services specification: <https://www.fdsn.org/webservices/>
- obspy FDSN client: <https://docs.obspy.org/packages/obspy.clients.fdsn.html>
- USGS ComCat: <https://earthquake.usgs.gov/fdsnws/event/1/>
- EMSC seismicportal: <https://www.seismicportal.eu/fdsnws/event/1/>
- INGV: <https://webservices.ingv.it/fdsnws/event/1/>
- EarthScope: <https://service.earthscope.org/fdsnws/event/1/>
- earthlens FDSN usage: [Usage](usage.md)
- earthlens FDSN API: [Reference](fdsn.md)
