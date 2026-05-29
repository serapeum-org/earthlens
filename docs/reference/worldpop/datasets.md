# WorldPop — Available datasets

earthlens curates the WorldPop population & demographic product families.
Each product (a top-level REST alias) offers one or more **sub-aliases**,
selected by the `(constrained, unadjusted, resolution, scope, generation,
level)` tuple. The default selectors resolve to the classic unconstrained
100 m per-country series (`pop` → `wpgp`).

This page is generated from the bundled
`worldpop_data_catalog.yaml`; regenerate the upstream index and re-validate
it with the [catalog tool](#catalog-tooling).

## Curated product / sub-alias matrix

| product | friendly | kind | demo | unit | sub-alias | constr | unadj | res | scope | gen | level | years |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `age_structures` | age_sex, age_structure, demographics | mixed | True | people/pixel | `aswpgp` | False | True | 100m | countries | R2021 | national | 2000-2020 |
|  | | | |  | `aswpgponekm` | False | True | 1km | global | R2021 | national | 2000-2020 |
|  | | | |  | `ascic_2020` | True | True | 100m | countries | R2021 | national | 2020 |
|  | | | |  | `ascicua_2020` | True | False | 100m | countries | R2021 | national | 2020 |
| `births` | births_count | raster | False | births/pixel | `bic` | False | True | 100m | countries | R2021 | national | 2000-2020 |
| `dependency_ratios` | dependency_ratio, dependency | raster | False | ratio | `drwc` | False | True | 1km | global | R2021 | national | 2000-2020 |
| `dug` | degree_of_urbanisation, degurba | raster | False | class | `dug_g2_v1` | True | True | 100m | countries | R2025A | national | 2015-2030 |
| `future_pop` | projections, population_projection, ssp | raster | False | people/pixel | `FPP_v02` | False | True | 1km | global | R2025A | national | 2025-2100 |
| `gbsg` | built_settlement_growth, settlement_growth | raster | False | class | `bsgm` | False | True | 100m | countries | R2021 | national | 2000-2020 |
| `pop` | population, population_counts, ppp | raster | False | people/pixel | `wpgp` | False | True | 100m | countries | R2021 | national | 2000-2020 |
|  | | | |  | `wpgpunadj` | False | False | 100m | countries | R2021 | national | 2000-2020 |
|  | | | |  | `wpic1km` | False | True | 1km | countries | R2021 | national | 2000-2020 |
|  | | | |  | `wpicuadj1km` | False | False | 1km | countries | R2021 | national | 2000-2020 |
|  | | | |  | `wpgp1km` | False | True | 1km | global | R2021 | national | 2000-2020 |
|  | | | |  | `cic2020_100m` | True | True | 100m | countries | R2021 | national | 2020 |
|  | | | |  | `cic2020_UNadj_100m` | True | False | 100m | countries | R2021 | national | 2020 |
|  | | | |  | `G2_CN_POP_R25A_100m` | True | True | 100m | countries | R2025A | national | 2015-2030 |
|  | | | |  | `G2_CN_POP_R25A_1km` | True | True | 1km | countries | R2025A | national | 2015-2030 |
|  | | | |  | `G2_MOS_POP_R25A_1km` | True | True | 1km | global | R2025A | national | 2015-2030 |
| `pop_density` | population_density, density, pd | raster | False | people/km2 | `pd_ic_1km` | False | True | 1km | countries | R2021 | national | 2000-2020 |
|  | | | |  | `pd_ic_1km_unadj` | False | False | 1km | countries | R2021 | national | 2000-2020 |
| `pregnancies` | pregnancies_count | raster | False | pregnancies/pixel | `individual_countries` | False | True | 100m | countries | R2021 | national | 2000-2020 |
| `pwd` | population_weighted_density | raster | False | people/km2 | `pwd_national_1km` | False | True | 1km | countries | R2021 | national | 2000-2020 |
|  | | | |  | `pwd_national_100m` | False | True | 100m | countries | R2021 | national | 2000-2020 |
|  | | | |  | `pwd_subnational_1km` | False | True | 1km | countries | R2021 | subnational | 2000-2020 |
|  | | | |  | `pwd_subnational_100m` | False | True | 100m | countries | R2021 | subnational | 2000-2020 |
| `urban_change` | urban, urbanisation | raster | False | class | `ucic` | False | True | 100m | countries | R2021 | national | 2000-2020 |

`demo = True` marks a demographic product whose per-cohort rasters are also
tabularised (only `age_structures`). `kind = mixed` follows.

## Age/sex cohorts (`age_structures`)

Each `age_structures` year ships **36 GeoTIFFs** = 2 sexes (`m`, `f`) ×
18 age bands. The band lower bounds are:

```
0 (<1), 1 (1–4), 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80 (80+)
```

The filename pattern is `{iso3}_{sex}_{age_low}_{year}.tif`
(e.g. `ken_f_0_2020.tif`); the AOI population sum per cohort is written to a
tidy table — see [Usage → Demographic tables](usage.md#demographic-tables).

## Global mosaics & archive products

The `scope="global"` mosaics of `pop` / `age_structures` (`wpgp1km`,
`aswpgponekm`, `G2_MOS_POP_R25A_1km`, …) **are fetchable**: the backend
resolves the per-year whole-world GeoTIFF via the hub's `?id=` detail
endpoint, downloads it, and crops to the AOI. WorldPop has no server-side
subsetting, so each global 1 km mosaic is a ~1.1 GB download per year.

`future_pop` (`FPP_v02`) and `dependency_ratios` (`drwc`) are marked
`archive: true` in the catalog — they ship as multi-file `.zip` (per-SSP) /
`.7z` (per-continent) archives, not per-year GeoTIFFs, so requesting them
raises `NotImplementedError`. They remain as accurate catalog metadata;
archive extraction + the SSP / continent axes are a follow-up.

## Not curated: `covariates`

The hub's `covariates` family (50+ heterogeneous named layers — VIIRS/DMSP
nightlights, SRTM slope, distance-to-roads/water, Global-2 built-up surface,
…) is **not** part of the curated selector catalog because its layers are
named one-offs that don't fit the
`(constrained × resolution × scope × generation)` model. The
[catalog tool](#catalog-tooling) lists every live covariate sub-alias for
reference.

## Catalog tooling

`tools/worldpop/` mirrors the GEE / CHC / tropycal catalog tooling — a
refresh, an audit, and a probe:

```bash
# refresh: crawl the hub and write the available_products index (every alias + sub-aliases)
python tools/worldpop/refresh_worldpop_catalog.py refresh -o available_products.yaml

# refresh: structural check of the curated catalog (offline) + upstream existence (--live)
python tools/worldpop/refresh_worldpop_catalog.py validate --live --check-files

# audit: diff the curated catalog vs the live hub; --strict exits non-zero on drift
python tools/worldpop/audit_worldpop_catalog.py --strict

# probe: capture the live REST shape (aliases, sub-aliases, a sample record) to JSON
python tools/worldpop/probe_worldpop_rest.py --alias pop --subalias wpgp --iso3 KEN -o probe.json
```

The `audit` report flags curated products / sub-aliases the hub no longer
serves and new upstream products that aren't curated (excluding the
deliberately out-of-scope specialty families). The `Catalog` also exposes a
runtime `health()` hygiene report and a `describe(product)` introspection
helper, matching the other backends' catalogs.

## Licence

All WorldPop data is **CC-BY-4.0** (attribution). The required citation is
carried per dataset in the REST record (`citation` field); the canonical
licence is at <https://hub.worldpop.org/data/licence.txt>.
