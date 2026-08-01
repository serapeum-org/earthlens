# Caravan — introduction

[**Caravan**](https://github.com/kratzert/Caravan) is an open community dataset of large-sample hydrology: for
each catchment it publishes a daily **streamflow** series, **ERA5-Land** meteorological forcing, static catchment
**attributes**, and a **basin polygon** — all standardised onto one schema so catchments from a dozen national
datasets can be modelled together. It accompanies
[Kratzert et al., *Scientific Data* **10**, 61 (2023)](https://www.nature.com/articles/s41597-023-01975-w).

`earthlens.caravan` fetches it from the static Zenodo archives and hands back a `pandas.DataFrame`, one row per
catchment-day.

## Why this backend exists: the GRDC route

The **Global Runoff Data Centre** is the reference archive for global river discharge, but its portal has **no
API** — you fill in a web form and receive files by email a day later — and its licence **prohibits
redistribution**. That combination cannot sit behind an anonymous `download()`, which is why raw GRDC stays in
[deferred providers](../deferred-providers.md).

GRDC does, however, publish the stations whose national services permit sharing as the **GRDC-Caravan** extension:
**CC-BY-4.0, redistributable, 5,356 catchments across 25 countries, 1950–2023**, as an ordinary Zenodo download.

**That makes `earthlens.caravan` the legal, scriptable route to open GRDC discharge.**

```python
from earthlens.core import EarthLens

flow = EarthLens(
    "grdc-caravan",
    variables=["streamflow", "total_precipitation"],
    start="2000-01-01", end="2000-12-31",
    lat_lim=[-90, 90], lon_lim=[-180, 180],
    gauge_ids=["GRDC_1159100"],
).download()
```

!!! warning "This is the open subset, not the whole GRDC database"
    GRDC-Caravan covers only the stations whose national services allow redistribution. The remaining restricted
    stations — a large share of the full archive — are still portal-only and are **not** reachable here. Do not
    describe a GRDC-Caravan result as "the GRDC database".

## Extensions and source datasets are different things

This distinction decides how you address the data, and conflating them is the usual first mistake.

- An **extension** is a separate **Zenodo record** — what you pass as `dataset=`. There are five:
  `base`, `grdc`, `germany`, `denmark`, `israel`.
- A **source dataset** is a directory *inside* an archive. `base` alone bundles seven of them — CAMELS-US,
  CAMELS-AUS, CAMELS-BR, CAMELS-CL, CAMELS-GB, HYSETS and LamaH-CE.

So there is no `dataset="hysets"`: HYSETS lives inside `base` and is reached by requesting `base` and selecting
its catchments. A `gauge_id` carries its source as a prefix (`hysets_01010070`, `camelsdk_100006`,
`il_12130`) — and note **GRDC alone uses an uppercase prefix**, `GRDC_1159100`.

## A historical archive, not a live feed

Caravan is **not near-real-time, and neither is GRDC itself.** National services submit to GRDC at irregular
intervals, often lagging by a year or more, and Caravan then republishes as versioned snapshots every 4–12
months. Verified series ends: GRDC `2023-05-19`, Israel `2024-09-30`, Denmark and Germany `2020-12-31`.

Every catalog row therefore pins a **specific Zenodo version record**, never the moving concept DOI, so a request
made today is reproducible tomorrow.

!!! tip "If you need current discharge, use something else"
    Reach for [`earthlens.usgs_water`](../usgs-water/introduction.md) for US near-real-time gauge data, or GloFAS
    through [`earthlens.ecmwf`](../ecmwf/introduction.md) for global modelled discharge. Caravan is for
    analysis-ready *archive* hydrology.

## Nothing is downloaded

Each archive is 0.3–9 GB, and `base` is 25–29 GB. The backend does not download them.

Every extension ships as a **ZIP**, which stores its file directory at the tail, so the archive is read in place
over HTTP **Range** requests. Measured against the 8.84 GB GRDC archive:

| operation | requests | transferred |
|---|---|---|
| index all 5,356 catchments | 4 | 0.81 MB |
| + one catchment's 73-year daily series | 2 | 2.10 MB |
| **one-catchment request, total** | **6** | **≈ 2.9 MB** |

The one exception is `base` at its current version, which ships as a `.tar.gz` — a single gzip stream with no
directory, so it cannot be seeked and must be fetched whole. That row is gated behind `allow_full_download=True`,
and `version="1.2"` offers a range-readable alternative. See [usage](usage.md#the-base-extension-is-opt-in).

## Licensing

Every wrapped extension is **CC-BY-4.0**. Cite Caravan itself plus the source datasets you used; each archive
ships its own `licenses/<source>/license_<source>.md`, and GRDC additionally ships a per-country
`LicensesCaravan.xlsx`. The catalog carries the attribution string for each row.
