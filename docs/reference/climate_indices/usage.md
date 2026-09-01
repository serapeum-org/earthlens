# Climate indices — usage

The `climate-indices` backend downloads one or more monthly index series
and returns them as a long-format `pandas.DataFrame`. It needs no
credentials (both NOAA PSL and KNMI Climate Explorer are open). See
[Available indices](datasets.md) for the `variables=` ids and
[Introduction](introduction.md) for how the two ASCII dialects are parsed.

## A single index

```python
from earthlens.core import EarthLens

df = EarthLens(
    data_source="climate-indices",
    variables=["oni"],
    start="1990-01-01",
    end="2020-12-31",
    path="indices_out",
).download()

df.head()
#         date index  value    source
# 0 1990-01-01   oni   0.12  noaa-psl
# ...
```

`download()` returns the long-format frame (`date`, `index`, `value`,
`source`) and also writes it to a CSV under `path`
(`climate_indices_oni.csv`). The monthly `date` is the first of each
month; the missing-value sentinel becomes `NaN` (kept, not dropped).

## Several indices at once

Pass more than one id; the result concatenates them, distinguished by the
`index` column (and `source`, since indices may come from different
sources):

```python
df = EarthLens(
    data_source="climate-indices",
    variables=["oni", "nao", "amo"],
    start="1980-01-01",
    end="2020-12-31",
).download()

sorted(df["index"].unique())     # ['amo', 'nao', 'oni']
df.groupby("index")["source"].first()
# index
# amo    knmi-climexp
# nao        noaa-psl
# oni        noaa-psl
```

The `"climate_indices"` and `"climate-indices:teleconnections"` aliases route to the same
backend.

## Pivoting to wide form

The long frame pivots to a month × index table in one call:

```python
wide = df.pivot(index="date", columns="index", values="value")
wide[["oni", "nao"]].dropna().head()
```

## What this backend does *not* do

- **No bounding box.** Climate indices are global scalars, so
  `lat_lim` / `lon_lim` / `aoi` are accepted but ignored — the same result
  comes back with or without them.

- **No `aggregate=`.** The values are already monthly scalars, so a
  non-`None` `aggregate=` raises `NotImplementedError`. Do any rollup on
  the returned DataFrame instead:

    ```python
    annual = (
        df.assign(year=df["date"].dt.year)
        .groupby(["index", "year"])["value"]
        .mean()
    )
    ```

- **No deriving indices from gridded fields.** This backend fetches the
  **published** index series; it does not compute ONI from SST grids.

## Output format

Pass `output_format="parquet"` to write Parquet instead of CSV (needs
`pyarrow`):

```python
EarthLens(
    data_source="climate-indices",
    variables=["oni"],
    start="2000-01-01",
    end="2020-12-31",
    output_format="parquet",
).download()
```
