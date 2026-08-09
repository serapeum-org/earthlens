# US flood exposure & loss (NSI + FEMA) — usage

All three sources go through the `EarthLens` facade. Pick the source with the
`source=` argument on the `nsi` key, or use the source-pinning aliases `nfip` /
`nfhl`.

## Structures (USACE NSI) — by FIPS

```python
from earthlens.core import EarthLens

# Every structure in one census tract (11-digit FIPS) as a FeatureCollection.
structures = EarthLens(
    "nsi",
    source="structures",
    fips="22071012700",  # an Orleans Parish, LA tract
).download()

print(len(structures), "buildings")
print(structures[["occtype", "st_damcat", "val_struct", "found_type"]].head())
```

`fips=` accepts a 2-digit state, 5-digit county, 11-digit tract, or 15-digit
block code. To select by an area instead, pass a bounding box — the backend POSTs
a GeoJSON polygon (the NSI `?bbox=` query does not work):

```python
structures = EarthLens(
    "nsi",
    source="structures",
    lat_lim=[29.95, 29.96],
    lon_lim=[-90.07, -90.06],
).download()
```

## Flood zones (FEMA NFHL) — by bounding box

```python
zones = EarthLens(
    "nfhl",  # alias for source="nfhl"
    lat_lim=[29.95, 29.96],
    lon_lim=[-90.07, -90.06],
).download()

print(zones[["FLD_ZONE", "SFHA_TF"]].head())
```

## Flood-insurance claims (FEMA NFIP v3) — by attribute filter

```python
claims = EarthLens(
    "nfip",  # alias for source="nfip"
    county="22071",   # 5-digit county FIPS
    year=2005,        # loss year
    max_records=500,  # cap the pull
).download()

print(claims.shape)
print(claims[["date_of_loss", "rated_flood_zone", "building_paid"]].head())
```

`nfip` accepts any combination of `state=` (two-letter), `county=` (5-digit
FIPS), `year=`, and `flood_event=`. At least one is required. The result is a
`DataFrame` with friendly column names and is also written to the output
directory (`output_format="csv"` by default, or `"parquet"`).

## Combining the three

A flood-damage study for one county typically overlays the exposure, the hazard,
and the observed losses:

```python
county_fips = "22071"
box = dict(lat_lim=[29.95, 29.96], lon_lim=[-90.07, -90.06])

structures = EarthLens("nsi", source="structures", fips=county_fips).download()
zones = EarthLens("nfhl", **box).download()
claims = EarthLens("nfip", county=county_fips, year=2005, max_records=500).download()
```

## What you cannot do

- **No unbounded national pull** — every source requires a spatial or attribute
  bound (see [Introduction](introduction.md)).
- **No non-US areas** — a request outside the US returns an empty result.
- **No `aggregate=`** — these are records, not gridded rasters, so a gridded
  reduction is rejected.
