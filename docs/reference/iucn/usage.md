# IUCN Red List — usage

The backend uses core `requests`, so there is **no extra to install**. You do
need an IUCN Red List v4 token.

## Authentication

Sign up for a free token at
[api.iucnredlist.org/users/sign_up](https://api.iucnredlist.org/users/sign_up),
then either set it in the environment:

```bash
export IUCN_TOKEN="your-token"     # PowerShell: $env:IUCN_TOKEN = "your-token"
```

or pass it as a keyword argument (`token="your-token"`). A missing token
raises `AuthenticationError` naming `IUCN_TOKEN`.

## Download assessments

Fetch a species' Red List assessments:

```python
from earthlens.core import EarthLens

frame = EarthLens(
    data_source="iucn",
    variables=["species:Panthera leo"],   # binomial -> genus + species
    start="2024-01-01",                   # not time-filtered; frames the request
    end="2024-12-31",
    lat_lim=[-90.0, 90.0],
    lon_lim=[-180.0, 180.0],
    path="out/iucn",
    # token="your-token",                 # or rely on IUCN_TOKEN
).download()

print(type(frame))                         # a pandas.DataFrame
print(frame[["scientific_name", "category", "year_published"]])
```

`download()` returns the assessment `DataFrame` and writes
`out/iucn/iucn_assessments.csv` (or `file_format="parquet"`).

## Country queries

List a country's assessments with an ISO alpha-2 selector:

```python
EarthLens(data_source="iucn", variables=["country:KE"], ...)
```

## Licensing

Red List data is `CC-BY-NC`; redistribution needs a written IUCN waiver.
**Every** download raises a `LicenseWarning`, and the data is never bundled
with earthlens — you fetch under your own token and agreement. Cite the Red
List as required by the
[Terms of Use](https://www.iucnredlist.org/terms/terms-of-use).
