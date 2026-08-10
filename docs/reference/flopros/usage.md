# FLOPROS flood-protection standards — usage

The `flopros` backend needs **no credentials** — the FLOPROS shapefile is public
in the NHESS-2016 supplement. A request selects the protection layer(s) and,
optionally, a country name or a bounding box; the downloaded zip is cached, so
repeated requests reuse it.

For the concepts (what the data is, the layers, the licence) see
[Introduction](introduction.md); the rendered API is the [Reference](flopros.md)
page.

## Quick start

```python
from earthlens.core import EarthLens

# The merged riverine protection standard for every subnational unit.
fc = EarthLens(
    "flopros",
    layer="merged_riverine",
    path="flopros-data",
).download()

fc.head()  # a FeatureCollection: name, geonunit, type_en, merged_riverine, geometry
```

`download()` returns a pyramids `FeatureCollection` (a `geopandas.GeoDataFrame`
subclass) and also writes it to a GeoPackage under `path`.

## Selecting layers

Omit `layer=` to keep every FLOPROS layer, or pass one name or a list:

```python
# Just the two riverine layers most people want.
fc = EarthLens(
    "flopros",
    layer=["merged_riverine", "modelled_riverine"],
).download()
```

The layer names are `merged_riverine`, `modelled_riverine`,
`design_min_riverine` / `design_max_riverine`, `policy_min_riverine` /
`policy_max_riverine`, and the four `*_coastal` equivalents.

## Filtering by country or bounding box

```python
# One country by name (matches `name` or `geonunit`, case-insensitive).
germany = EarthLens("flopros", country="Germany").download()

# Or a bounding box — keeps every unit intersecting it.
low_countries = EarthLens(
    "flopros",
    lat_lim=[50.7, 53.6],
    lon_lim=[3.3, 7.2],
).download()
```

`country=` is an exact, case-insensitive match against the source spelling. When
it matches nothing the backend logs a warning and returns an empty collection —
if you are unsure of the spelling, filter by bounding box instead.

## Tabular output

Pass `geometry=False` to drop the geometry and get a plain `pandas.DataFrame` of
the units and their protection-standard columns:

```python
table = EarthLens("flopros", layer="merged_riverine", geometry=False).download()
```

## Using it as a defended-vs-undefended correction

FLOPROS is most useful joined to a flood-hazard layer: read the protection
standard per unit, then mask or threshold a hazard map (e.g. from `aqueduct` or
`gee`) so that only floods rarer than the local standard count as unprotected
exposure.
