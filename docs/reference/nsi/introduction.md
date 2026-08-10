# US flood exposure & loss (NSI + FEMA) — introduction

earthlens ships a single `nsi` backend that fetches **US object-level flood
exposure and loss** from three keyless, public-domain US-federal REST services,
selected by a `source=` discriminator:

- **`structures`** (default) — the **USACE National Structure Inventory (NSI)**:
  building points carrying occupancy type, replacement value (structure /
  contents / vehicle), foundation type and height, area, year built, and the
  FEMA firm zone. A **vector** source.
- **`nfhl`** — the **FEMA National Flood Hazard Layer**: the regulatory flood
  zones (`FLD_ZONE` A/AE/X/VE…, the special-flood-hazard-area flag `SFHA_TF`)
  from the ArcGIS `S_Fld_Haz_Ar` layer. A **vector** source.
- **`nfip`** — the **FEMA NFIP redacted claims (v3)**: millions of
  flood-insurance claim records with paid amounts (building / contents / ICC),
  loss dates, cause, and flood zone, via the OpenFEMA OData endpoint. A
  **tabular** source — the largest open flood-loss dataset in existence.

## Why it matters here

Object-level exposure **with replacement values** is the binding constraint on
flood damage modelling worldwide, and the US is the one place it is fully open.
The `nsi` backend puts the exposure layer (NSI), the hazard layer (NFHL), and the
observed-loss record (NFIP) behind one facade so a damage study can assemble all
three for the same county in a few calls.

Like the other hazards backends (`gdacs`, `risk_indicators`, `emdat`), `nsi`
departs from the gridded backends in two ways:

- **The output is per instance.** `structures` and `nfhl` are **`vector`** —
  `download()` returns a [pyramids](https://github.com/serapeum-org/pyramids)
  `FeatureCollection` (a `geopandas.GeoDataFrame` subclass). `nfip` is
  **`tabular`** — `download()` returns a `pandas.DataFrame` (also written to the
  output directory as CSV or Parquet). `NSI.OUTPUT_KIND` is set from the resolved
  source at construction, and the facade reads it to know the return shape.
- **There is no meaningful gridded reduction**, so `aggregate=` is rejected.

## Scope: US only

All three sources are **US CONUS + territories** only. A request for an area
outside the US returns an **empty** result (an empty `FeatureCollection` for a
non-US structures box), not an error. For global building exposure use the
`overture`, `ghsl`, or `worldpop` backends instead.

## Bounded requests are required

None of the three sources may be pulled unbounded (NSI's no-argument call is an
HTTP 500, and NFIP is millions of rows). The backend **requires** a bound and
refuses the request otherwise:

- `structures` — a `fips=` code (2-digit state / 5-digit county / 11-digit tract
  / 15-digit block), **or** a `[lat_lim, lon_lim]` box.
- `nfhl` — a `[lat_lim, lon_lim]` box (the ArcGIS query envelope).
- `nfip` — a `filters=` mapping with at least one of `state`, `county`, `year`, `flood_event`. The
  paged fetch logs the total matching-record count so a large pull is visible,
  and `max_records=` caps it.

## Authentication & licence

None. All three services are keyless and in the **US public domain** — no
`auth.py`, no credentials, no optional SDK extra. The backend ships with
`earthlens-hazards` and needs only the core `requests` + `pandas` + `pyramids`
dependencies.

## A note on NFHL reachability

The FEMA NFHL host (`hazards.fema.gov`) is reachable from most networks but was
blocked from the environment this backend was built in. The `structures` and
`nfip` sources are fully live-verified; the `nfhl` source is implemented and
unit-tested against a canned fixture, and its live end-to-end test is `xfail` in
that environment — it runs green from a reachable network.

For the hands-on walkthrough see [Usage](usage.md); the rendered API is the
[Reference](nsi.md) page.
