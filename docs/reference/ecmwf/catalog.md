# Catalog & probe tooling

The ECMWF backend uses a curated YAML catalog (the
`src/earthlens/ecmwf/catalog/` directory: per-family `*.yaml` files —
`era5.yaml`, `carra.yaml`, … — plus an `_index.yaml` with the schema
header and `available_datasets:` list) to map user-friendly variable
codes to the request fields the store actually accepts. The loader
merges every file into one catalog at construction time. This page
documents the catalog's structure, how it is maintained, and the tools
that support catalog work.

One catalog covers **five** data stores — the three Copernicus ones
(CDS, ADS, EWDS) and the two ECMWF-hosted ones (ECDS, XDS). They all
run the same CADS software and take the same request vocabulary, so a
row differs only by its [`endpoint`](#endpoint) field; the `cds_*`
field names below are historical and apply to every store. See
[the five data stores](datastores.md) for the cross-store picture.

## At a glance

| File / tool | Role |
|---|---|
| `src/earthlens/ecmwf/catalog/` | The catalog itself — per-family `*.yaml` + `_index.yaml`; schema below |
| `src/earthlens/ecmwf/catalog.py` | The loader (`Catalog`, `Dataset`, `Variable`, …) |
| `earthlens datasets refresh ecmwf` | Diffs the `available_datasets:` index against every store's live catalogue API |
| `earthlens datasets audit ecmwf --coverage` | Classifies the available universe into DONE / addressable / thin / table / missing — coverage planning |
| `earthlens datasets validate ecmwf --live` | Builds a `constraints.json`-valid minimal request per dataset and runs the pre-flight `RequestValidator` (no submission) |
| `earthlens datasets probe ecmwf <id> --deep` | Submits a real tiny retrieve for one dataset and extracts its NetCDF short names and units |
| `earthlens datasets curate ecmwf <id> [--write]` | Seeds a loader-valid row from the live `form.json` (every variable, `unknown` placeholders); `--write` auto-files it into the family shard |
| `earthlens datasets curate ecmwf --all --write` | Bulk-seeds **every** uncurated dataset (`available_datasets − datasets`) into its family shard from live forms |
| `earthlens datasets curate ecmwf --fill-empty --write` | Bulk-hydrates the `units: unknown` placeholders in place from live tiny retrieves (licence-gated, best-effort) |

## Catalog structure

The catalog YAML has two top-level blocks plus a schema header
(comment) at the top.

### Schema skeleton

The full shape of the file with every field, using placeholder names
and value markers so the hierarchy is visible at a glance. Comments
describe each field; required vs optional is called out explicitly.

```yaml
version: 3                                       # catalog schema version (informational)

available_datasets:                              # inventory across all five stores (~174)
  - <dataset_name_1>                             #   one entry per dataset the stores publish
  - <dataset_name_2>                             #   diffed by `earthlens datasets refresh ecmwf`
  - ...

datasets:                                        # curated, user-addressable datasets (~174)

  <dataset_name_1>:                              # one block per dataset the package supports
    product_type: [<value>]                      # REQUIRED — CDS product_type request field
    pressure_level: [<level>, <level>, ...]      # optional — default for pressure-level datasets
    request_kind: <kind>                         # optional — "form" | "oceanic_monthly" | "carra_means"
    endpoint: <slug>                             # optional — "cds" (default) | ads | ewds | ecds | xds
    extras:                                      # optional — parent-level CDS request fields
      <key_1>: <value>                           #   merged into every variable's extras
      <key_2>: <value>                           #   per-variable overrides win on key collision
    monthly: <sibling_dataset_name>              # optional — cross-ref to monthly-means sibling
    monthly_product_type: [<value>]              # REQUIRED if `monthly:` is set — sibling's product_type
    variables:                                   # REQUIRED — per-variable map (≥ 1 entry)

      <variable_code_1>:                         # one block per variable
        nc_variable: <name>                      # REQUIRED — short name inside the returned NetCDF
        units: <string>                          # REQUIRED — raw ERA5 unit string (used in filename)
        cds_variable: <name>                     # optional — defaults to underscored form of <variable_code>
        types: <"flux" | "state">                # optional — accumulated flux or instantaneous state
        product_type: [<value>]                  # optional — per-row override of parent default
        cds_pressure_level: [<level>, ...]       # optional — per-row override of parent pressure_level
        request_kind: <kind>                     # optional — per-row override of parent request_kind
        extras:                                  # optional — per-row CDS request fields
          <key>: <value>                         #   merged with parent extras; row keys win on collision

      <variable_code_2>:
        ...

  <dataset_name_2>:
    ...
```

### Field reference

Each field below has its own subsection with a full description: what
it is, when to set it, what value to put in it, and how it interacts
with other fields. Fields are grouped by hierarchy level.

#### Top-level fields

##### `version`

*Optional.* Catalog schema version number, currently `3`. Purely
informational — the loader does not branch on its value, and changing
it does not change runtime behaviour. Useful as a marker for downstream
tooling that needs to detect catalog format changes between releases.
Bump it when the schema gains an incompatible new field.

##### `available_datasets`

*Optional, but conventionally always present.* A flat list of every
dataset short name the five stores currently publish — roughly 174
entries today, keyed under a per-store block in `_index.yaml`. The
list is maintained by `earthlens datasets refresh ecmwf`, which hits
each store's live catalogue API
(`<store-root>/catalogue/v1/collections`), filters for the entries the
package targets, and reports the diff against the bundled index.

The runtime download path does **not** read `available_datasets` —
nothing under `Catalog.download` or `_api()` consults it. The block is
purely a discovery / inventory hint, surfaced as the
`Catalog.available_datasets` attribute so tooling (notably
`earthlens datasets audit ecmwf --coverage`) can iterate the full
inventory without hitting the network itself. Users who want to know
"what do these stores host that I might be able to ask for?" can read
this list offline; users who want to actually request data can only
use datasets that also appear under `datasets:`.

`Catalog.store_for(<dataset_id>)` resolves any id in this list to the
store that serves it.

##### `datasets`

*Required.* The curated map from dataset short name to a `Dataset`
block. Roughly 174 entries today — one per dataset the package
actually supports with variable definitions. The runtime download path
uses this exclusively. Adding a new entry here (typically after
probing the store for the right variable shapes) is what makes a
dataset usable through the package's `ECMWF` backend.

The map must be non-empty and at least one entry must declare at
least one variable, or `Catalog()` raises `ValueError` at load time.

#### Dataset-level fields (under `datasets.<dataset_name>:`)

##### `product_type`

*Required.* List of strings. The CDS `product_type` request parameter
that selects which flavor of data the dataset's request returns.
Different datasets accept different `product_type` enumerations:

- ERA5 daily datasets: `[reanalysis]` (deterministic, the most common),
  `[ensemble_members]` / `[ensemble_mean]` / `[ensemble_spread]`.
- ERA5 monthly-means datasets: `[monthly_averaged_reanalysis]`,
  `[monthly_averaged_reanalysis_by_hour_of_day]`, etc.
- CARRA: `[analysis]` or `[forecast_based]`.
- ORAS5: `[consolidated]`, `[operational]`.
- CMIP6: `[climate_projection]`.

This becomes the default for every variable under this dataset and is
written into each `Variable.product_type` field at load time. A
per-variable row may override it (see the variable-level
`product_type` field below). Check the dataset's live `constraints.json`
if you're unsure which values are valid — `earthlens datasets audit
ecmwf --coverage` will show you.

##### `pressure_level`

*Optional.* List of strings (CDS expects strings, not integers, even
for numeric levels). Default pressure-level set for pressure-level
datasets, e.g. `["1000"]` for ERA5 pressure-levels at 1000 hPa.
Propagated to each variable's `cds_pressure_level` at load time;
per-variable overrides win.

Set this **only** on pressure-level datasets — single-level datasets
do not accept the `pressure_level` request parameter, and CDS will
reject the request if it leaks through. The catalog loader does not
enforce that; it's the YAML author's responsibility.

##### `request_kind`

*Optional.* String tag, `"form"` by default. Drives the
`_REQUEST_KIND_STRIPS` table in `src/earthlens/ecmwf/backend.py` — at
request-build time, the named template-default fields are stripped
from the request because the dataset rejects them. A few kinds also
rewrite the date keys.

| Kind | Strips | Use for |
|---|---|---|
| `form` (default) | — | ERA5-style datasets |
| `oceanic_monthly` | `day`, `time`, `area` | ORAS5 and other global-monthly ocean datasets that support neither bbox cropping nor sub-daily slicing |
| `carra_means` | `time` | CARRA-means and other pre-aggregated datasets whose aggregation already covers the window |
| `glofas` | `time` | GloFAS (EWDS) — the horizon comes from `leadtime_hour` in `extras`, not a time-of-day |
| `glofas_hindcast` | `time` | GloFAS/EFAS reforecast — **renames** `year`/`month`/`day` to `hyear`/`hmonth`/`hday` |
| `s2s_reforecast` | — | S2S reforecast (ECDS) — **copies** `month`/`day` into `hmonth`/`hday`, keeping both date axes |
| `seasonal` | `day`, `time` | Seasonal forecasts keyed by year/month + a lead |
| `seasonal_hindcast` | `day`, `time` | EFAS seasonal reforecast — renames to `hyear`/`hmonth`, no `hday` |
| `cams_date` | — | CAMS grid datasets (ADS) — a single `date` range string replaces year/month/day |
| `cams_inversion` | `day`, `time`, `area` | CAMS GHG inversion + European air-quality reanalyses (global-gridded, reject `area`) |
| `fire` | `time` | CEMS fire danger (EWDS) — daily date plus a `grid` selector |
| `satellite_cdr` | `time` | Satellite Climate Data Records — zip-of-NetCDF response, no `data_format` choice |

The two reforecast kinds differ in a way worth knowing: `glofas_hindcast`
**renames** the date keys, so the original model-cycle date is gone,
while `s2s_reforecast` **copies** them, because that dataset requires
both the model cycle *and* the reforecast date. See
[the ECDS page](ecds.md#reforecasts-have-two-date-axes).

To add a new request-kind category, extend `_REQUEST_KIND_STRIPS` in
code with the new key and the field tuple to strip, then use the new
key in YAML.

##### `endpoint`

*Optional.* String slug naming which of the five stores serves this
dataset — `"cds"` (the default when omitted), `"ads"`, `"ewds"`,
`"ecds"`, or `"xds"`. It is the only field that differs between two
otherwise-identical rows on different stores.

The slug resolves to an API root and a credential through the
`ENDPOINTS` table in `src/earthlens/ecmwf/endpoints.py`:

| Slug | Store | API root | URL override | Key override |
|---|---|---|---|---|
| `cds` | Climate Data Store | `https://cds.climate.copernicus.eu/api` | `CDSAPI_URL` | `CDSAPI_KEY` |
| `ads` | Atmosphere Data Store | `https://ads.atmosphere.copernicus.eu/api` | `ADS_URL` | `ADS_KEY` |
| `ewds` | Early Warning Data Store | `https://ewds.climate.copernicus.eu/api` | `EWDS_URL` | `EWDS_KEY` |
| `ecds` | ECMWF Data Store | `https://ecds.ecmwf.int/api` | `ECDS_URL` | `ECDS_KEY` |
| `xds` | ECMWF Cross Data Store | `https://xds.ecmwf.int/api` | `XDS_URL` | `XDS_KEY` |

The URL override applies to the catalog tooling as well as the client.
Credentials fall back in the order *key override* → `CDSAPI_KEY` →
`~/.cdsapirc`, so **one Personal Access Token authenticates all five**
— there is no separate registration per store.

The backend keeps one `cdsapi.Client` per endpoint, so a single
`download()` spanning datasets on different stores works without any
extra configuration.

##### `extras`

*Optional.* Free-form dictionary of CDS request fields that don't fit
the standard ERA5 schema. Merged into every variable's `extras` at
catalog load time; per-variable keys win on collision. Use this for
family-wide selectors that always apply to every variable in the
dataset:

- CARRA needs `domain: east` (or `west`) and `leadtime_hour: '1'`.
- CMIP6 needs `experiment: ssp585` and `model: ec_earth3` (or whatever
  experiment / model the user is targeting).
- ORAS5 needs `vertical_resolution: single_level`.

The merged dict is what reaches `cdsapi.Client.retrieve()` after the
template defaults are stripped per `request_kind`. Fields named here
that overlap with the template defaults (e.g. `product_type` itself)
will override those defaults — that's how non-ERA5 families
historically supplied their non-default `product_type` before the
top-level field existed.

##### `monthly`

*Optional.* String. The short name of a **sibling** CDS dataset,
typically the monthly-means variant (e.g.
`reanalysis-era5-single-levels-monthly-means`).

**This field does not describe this dataset; it describes a
different, sibling dataset.** When set, the catalog loader's
auto-synthesis loop materializes a second top-level entry under
`<sibling_name>` with the same `variables:` block, rebranding each
variable's `cds_dataset` to the sibling name and its `product_type`
to whatever `monthly_product_type:` declares. The shim exists purely
to avoid duplicating the variables block in YAML.

If you find this confusing — you should. See the "Where the current
YAML breaks the hierarchy" section below for the full explanation,
and the "Status today" subsection for why the shim is still in use
despite the cost.

##### `monthly_product_type`

*Required if `monthly:` is set; absent otherwise.* List of strings.
The `product_type` to assign to the auto-synthesized sibling
dataset's variables, e.g. `[monthly_averaged_reanalysis]` for the
ERA5 monthly-means siblings.

Like `monthly:`, this field describes the **sibling**, not the
dataset whose YAML block hosts it. The catalog loader fails loudly
with a `ValueError` if `monthly:` is set without
`monthly_product_type:` — there is no hardcoded fallback.

##### `variables`

*Required.* A map from variable short code (the YAML key) to a
`Variable` block. At least one entry must be present, or the loader
raises `ValueError`.

The variable short code is the slug-cased name the user passes in the
`variables` argument to `ECMWF(...)` — e.g. `"2m-temperature"`. By
default the code is also used to derive the `cds_variable` request
name (replacing hyphens with underscores), so `"2m-temperature"` →
`cds_variable: "2m_temperature"` automatically. Override by setting
`cds_variable` explicitly on the row.

#### Variable-level fields (under `variables.<variable_code>:`)

##### `nc_variable`

*Required.* String. The short name of the variable as it appears
inside the NetCDF file CDS returns, e.g. `"t2m"` for 2-metre
temperature, `"tp"` for total precipitation, `"sp"` for surface
pressure. Post-processing tooling reads it via
`fh.read_array(variable=nc_variable)` to extract the data array.

`nc_variable` is **not** derivable from the request name — CDS picks
the NetCDF variable name following ECMWF's GRIB short-name convention,
which is sometimes the request name with underscores collapsed (e.g.
`2m_temperature` → `t2m`) but often something different (e.g.
`total_precipitation` → `tp`). Discover the right value by submitting
a probe via `earthlens datasets probe ecmwf <id> --deep`, which downloads
a real NetCDF and prints the variable's short name and units.

##### `units`

*Required.* String. The raw unit string CDS emits for this variable
in the NetCDF, e.g. `"K"` for temperature in Kelvin, `"m"` for
precipitation in metres of water equivalent, `"J m**-2"` for
radiation accumulations, `"%"` for cloud cover, `"m s**-1"` for wind
speed.

The package returns values in their **native ERA5 units** — no unit
conversion happens during download. The string is documentation only:
the output filename is built from `cds_variable` and the dataset id, and
nothing else in the package reads `units`. Downstream code is responsible
for any conversion the user wants (e.g. K → °C, m → mm).

The value is transcribed from the producer's own `units` attribute and is
**not normalised**, so the catalog spans many spellings of one unit —
`W m**-2`, `W m-2`, `W/m^2`; `K` and `kelvin`; `1`, `%`, and an empty
string for a ratio. Rows of a single stanza can disagree, because the file
they describe does: `satellite-surface-radiation-budget` labels `SRS`
`W/m^2` and `SOL` `W m-2`. Rewriting either would make this row misdescribe
the granule it documents, so the spelling is left as found.
**Compare units by parsing them, never by string equality.**

##### `cds_variable`

*Optional.* String. The exact `variable` request name CDS expects,
e.g. `"2m_temperature"`, `"total_precipitation"`. Defaults to the
variable code (the YAML key) with hyphens replaced by underscores —
that's correct ~95% of the time.

Set this explicitly only when the slug-derived default is wrong.
Common cases:

- The catalog uses a disambiguator suffix that the request name
  doesn't carry: YAML key `"2m-temperature-seasonal"` →
  `cds_variable: 2m_temperature` (the dataset is what makes it
  "seasonal", not the variable name).
- The CDS request name uses a different word ordering or capitalization
  that hyphen-to-underscore can't recover.

##### `types`

*Optional.* `"flux"` or `"state"`. Marks whether this variable's
values are accumulations over a time window (flux) or instantaneous
samples (state). Drives the `op="auto"` resolver in
`earthlens.aggregate` — see the `aggregate` reference for the full
walkthrough.

###### State variables

Examples: `2m-temperature`, `surface-pressure`,
`2m-dewpoint-temperature`, `10m-u-component-of-wind`, `temperature`
(pressure-levels), `relative-humidity`, `geopotential`.

Each NetCDF sample is the **instantaneous** value of the field at
that timestamp — e.g. the air temperature at exactly 06:00 UTC. The
natural per-window reduction is the **mean** (or sometimes min/max
for daily extrema). No multiplication by window length is needed
because state values are not accumulations.

`op="auto"` on a state variable resolves to `"mean"`.

###### Flux variables

Examples: `total-precipitation`, `evaporation`, `surface-runoff`,
`surface-net-solar-radiation`, `surface-latent-heat-flux`,
`evaporation-from-bare-soil` (ERA5-Land).

Each NetCDF sample is the **accumulation since the previous
post-processing step** — typically a 6-hour accumulation in legacy
CDS daily ERA5 (4 slots/day at 00:00, 06:00, 12:00, 18:00) or a
1-hour accumulation in CDS-Beta. Each slot already carries units
of "amount over its window" (e.g. m of water equivalent for
evaporation), not a rate.

To get the per-window total, you **sum** the per-slot accumulations
that fell into the window. Multiplying a *mean* of the slots by the
window length under-counts by the number of slots per day (4× for
6-hourly, 24× for hourly).

`op="auto"` on a flux variable resolves to `"sum"`.

###### Worked example — daily evaporation

Suppose CDS returns the four 6-hourly slots for one pixel on
2009-01-01 (in metres of water equivalent):

| Slot   | Value (m) | Physical meaning |
|--------|-----------|------------------|
| 00:00  | 0.001     | evaporation 18:00 (prev day) → 00:00 |
| 06:00  | 0.002     | evaporation 00:00 → 06:00 |
| 12:00  | 0.005     | evaporation 06:00 → 12:00 |
| 18:00  | 0.004     | evaporation 12:00 → 18:00 |

The physically correct daily total is the sum of the four 6-hour
accumulations:

```
daily total = 0.001 + 0.002 + 0.005 + 0.004 = 0.012 m
```

`op="auto"` (which routes flux to `"sum"`) produces exactly that.
A `"mean"` reduction would yield `0.003 m` — the average 6-hour
accumulation, **not** the daily total.

###### Default when unset

When `types` is omitted, the variable is treated as **state**
(`is_flux` is `False`). Catalog rows for accumulation-style
variables must set `types: flux` explicitly so `op="auto"` can route
them correctly.

Consumed by `earthlens.aggregate.aggregate_netcdf` (and indirectly by
`ECMWF.download(aggregate=...)` and the
`examples/post_process_ecmwf_netcdf.py` CLI). Not used by the
request builder — `_api()` ignores `types`.

##### `product_type` (per-row)

*Optional.* List of strings. Per-row override of the parent dataset's
`product_type`. Use this when a single variable wants a non-default
flavor:

- `[ensemble_mean]` for an ensemble-mean variant of a normally-
  `[reanalysis]` ERA5 variable.
- `[analysis_based]` for one CARRA-means variable while the rest of
  the family uses the parent default `[forecast_based]`.

When absent, the variable inherits the parent's `product_type`. When
present, the row's value wins entirely (no merging — `product_type`
is a flat list, not a dict).

##### `cds_pressure_level`

*Optional.* List of strings. Per-row override of the parent's
`pressure_level`. Most pressure-level variables inherit the parent
default; use this only when one specific variable should pull a
different level set than the rest of its dataset family. Rare.

##### `unhydratable`

*Optional.* String. Why a live probe can never fill this row's `units` /
`nc_variable`. Absent means the row is simply not filled yet, and
`earthlens datasets curate ecmwf --fill-empty` will try it; present means no
retrieve can answer it, and the sweep skips the row rather than spending a
request and a queue slot to rediscover that.

Write the key only when the row is terminal. `unhydratable: null` (and `~`,
and an empty value) load as absent, so they mean *pending* — the sweep will
still probe such a row. There is no spelling of the key that means "skip me"
other than a reason string.

The only value in use is `pseudo-slug`: the row is keyed `all` or
`all-variables`, naming every variable its dataset serves rather than one of
them. That is resolvable only where the dataset serves exactly one variable —
`satellite-precipitation` and `satellite-sea-surface-temperature` are hydrated
that way and carry no mark. The mark records that this row has more than one
candidate and so can never be pinned to a single variable.

Without this, a placeholder waiting for a sweep and one no sweep can ever
answer look identical, so every re-run pays for the second kind again.

##### `request_kind` (per-row)

*Optional.* Per-row override of the parent's `request_kind`. Very
rare — usually the whole dataset shares one request-shape category,
but the hook exists if you ever need a single variable to follow a
different stripping rule than its siblings.

##### `extras` (per-row)

*Optional.* Free-form dictionary of CDS request fields, merged with
the parent's `extras` at load time (row keys win on collision). Use
for variable-specific overrides:

- One CARRA row needs `leadtime_hour: '3'` while the rest of the
  family stays at the parent default `leadtime_hour: '1'`.
- One CMIP6 row needs `experiment: historical` while the rest are
  `experiment: ssp585`.

The merged result is what the variable's `Variable.extras` holds at
runtime, and what eventually overrides the request template's
template-default fields when `_api()` builds the `cdsapi.retrieve()`
call.

### Concrete example

The same shape as the schema skeleton above, with real values
substituted in. This is what an actual ERA5 dataset entry looks like
in the `era5.yaml` shard:

```yaml
datasets:
  reanalysis-era5-single-levels:
    product_type: [reanalysis]                                   # required
    monthly: reanalysis-era5-single-levels-monthly-means         # optional cross-ref
    monthly_product_type: [monthly_averaged_reanalysis]          # required if `monthly:` set
    pressure_level: ["1000"]                                     # optional, pressure-level datasets only
    extras:                                                      # optional parent-level CDS request fields
      domain: east
      leadtime_hour: '1'
    request_kind: form                                           # optional; "form" | "oceanic_monthly" | "carra_means"
    variables:
      "2m-temperature":
        cds_variable: 2m_temperature      # optional — defaults to slug-with-underscores form of the key
        nc_variable: t2m                  # required — variable name inside the returned NetCDF
        units: K                          # required — raw ERA5 unit string
        types: state                      # optional — "flux" | "state"
        product_type: [ensemble_mean]     # optional per-row override of the parent default
        extras:                           # optional per-row overrides; row keys win on collision
          experiment: ssp585
```

### Inheritance rules (parent → variable)

Quick-glance summary of what propagates from a dataset block to its
variables at catalog load time. See the field reference subsections
above for the details and edge cases.

- `product_type` — parent default; per-row `product_type` wins.
- `pressure_level` → `cds_pressure_level` on each variable; per-row
  `cds_pressure_level` wins.
- `extras` — dict merged into each variable's `extras`; row keys win
  on collision.
- `request_kind` — parent default; per-row `request_kind` wins.

The auto-synthesis machinery on top of these rules (`monthly:` /
`monthly_product_type:`) is documented separately below.

### The expected hierarchy

The natural reading of the YAML is:

```text
datasets:
  <dataset name>:
    <its own properties>
    variables:
      <variable code>:
        <its own properties>
```

Everything under a dataset key is metadata about *that* dataset. Each
variable under `variables:` is metadata about *that* variable. Clean
two-level hierarchy.

### Where the current YAML breaks the hierarchy: auto-synthesis

Two fields under a dataset key do **not** describe that dataset — they
describe a **different, sibling dataset** that the catalog loader
materializes at runtime:

```yaml
datasets:
  reanalysis-era5-single-levels:                                  # ← dataset #1 (daily)
    product_type: [reanalysis]                                    # ✓ property of THIS dataset
    pressure_level: [...]                                         # ✓ property of THIS dataset
    extras: { ... }                                               # ✓ property of THIS dataset
    request_kind: form                                            # ✓ property of THIS dataset
    variables:                                                    # ✓ property of THIS dataset
      "2m-temperature": { ... }
      "total-precipitation": { ... }

    monthly: reanalysis-era5-single-levels-monthly-means          # ✗ NAMES A DIFFERENT DATASET
    monthly_product_type: [monthly_averaged_reanalysis]           # ✗ PROPERTY OF THAT OTHER DATASET
```

`monthly:` does not say "this dataset has monthly data." It says
"there exists a separate dataset named `…-monthly-means`; please
materialize it in the catalog with the same variables as me."
`monthly_product_type:` carries the `product_type` that the *sibling*
needs.

This violates the natural hierarchy: those two fields are smuggled
inside dataset #1's block but they describe dataset #2.

### Why the shim exists: YAML compactness

ERA5's daily and monthly-means datasets share the same set of
variables — same names, same `nc_variable`, same units. The shim lets
the YAML store the variables block **once**; the loader's
auto-synthesis loop generates the sibling catalog entry at load time,
rebranding each variable's `cds_dataset` to the monthly name and
copying the `monthly_product_type` over.

Without the shim, the YAML would have to declare both datasets as
fully separate top-level entries with duplicated `variables:` blocks
— roughly +1700 lines for the three ERA5 pairs (single-levels,
pressure-levels, land).

### The honest alternative

Drop auto-synthesis and let each dataset have its own first-class
top-level entry:

```yaml
datasets:
  reanalysis-era5-single-levels:                       # dataset #1
    product_type: [reanalysis]
    pressure_level: ...
    extras: ...
    request_kind: form
    variables: { ... }                                 # ~250 vars, daily

  reanalysis-era5-single-levels-monthly-means:         # dataset #2 — separate top-level entry
    product_type: [monthly_averaged_reanalysis]
    pressure_level: ...
    extras: ...
    request_kind: form
    variables: { ... }                                 # exact same ~250 vars, just a different cds_dataset
```

Two datasets, two top-level entries, every field under each one
belongs to that one. No `monthly:`, no `monthly_product_type:`, no
auto-synthesis loop in code.

### Status today

The current YAML keeps the shim (`monthly:` / `monthly_product_type:`)
because the catalog automation that regenerates this file already
manages the duplication-vs-shim trade-off. Both styles produce
identical runtime behaviour: `cat.datasets["…-single-levels"]` and
`cat.datasets["…-single-levels-monthly-means"]` both resolve, with
matching variables and the right `product_type` on each side.

If you encounter the wart (e.g. seeing two `product_type`-named fields
on one dataset and wondering why), the answer is "auto-synthesis
shim" — the second one is the product_type of the sibling.

## Maintenance workflow

The two top-level blocks have **different ownership**:

| Block | Authored by | Refreshed via |
|---|---|---|
| `available_datasets:` | The stores | `earthlens datasets refresh ecmwf` |
| `datasets:` | Hand or catalog automation | `earthlens datasets curate ecmwf <id> --write` — see below |

When a store publishes a new dataset, `refresh` reports it as missing
from `available_datasets:`. Adding it to `datasets:` (so the package
can actually request data from it) is a separate step requiring probes
— see "Adding a new dataset" below.

## Discovery & probe tools

All catalog tooling lives in the `earthlens datasets` CLI; there are no
standalone scripts. See [the CLI reference](../cli.md).

### `earthlens datasets refresh ecmwf`

Pulls the live catalogue from each of the five stores
(`<store-root>/catalogue/v1/collections`), filters for the entries the
package targets, and diffs the result against the bundled
`available_datasets:` index — reporting per store what is new upstream
and what the bundle lists that upstream no longer serves.

```bash
earthlens datasets refresh ecmwf
```

Run before each release so the index reflects whatever the stores host
on release day.

### `earthlens datasets audit ecmwf --coverage`

For each short name in `available_datasets:`, hits the store's public
`constraints.json` endpoint via `fetch_constraints` and classifies it:

- whether constraints are public,
- how many distinct `variable` values appear,
- which extra request fields beyond the ERA5 standard set are required.

```bash
earthlens datasets audit ecmwf --coverage
```

Output is bucketed (`DONE` / `addressable` / `thin` / `table` /
`missing`) so you can see at a glance which datasets are ready to add
and which need bespoke modelling (typically extras keys like `domain`,
`experiment`, `leadtime_hour`, …). It also names the next id to curate.

### `earthlens datasets validate ecmwf --live`

Per-dataset request-validity check. For each curated dataset it asks
`Catalog.minimal_valid_request` for a known-valid request derived from
`constraints.json` and runs the same pre-flight `RequestValidator` the
backend uses — flagging datasets whose minimal request fails. It is
stateless: no credentials and no queue submission (datasets that
publish no constraints are skipped).

```bash
earthlens datasets validate ecmwf --live
```

The offline half of `validate` is not wired for this provider — ecmwf
is live-only, because a row's validity is defined by the store's
`constraints.json` rather than by anything checkable on disk.

To confirm a single dataset actually serves data (and read its NetCDF
short names / units), submit a real tiny retrieve with `probe --deep`:

```bash
earthlens datasets probe ecmwf <dataset-short-name> --deep
```

`probe --deep` walks the returned NetCDF and reports each variable's
`long_name`, `units`, and short name — the values you copy into the
`datasets:` block as `nc_variable` and `units`.

## Bulk curation (gated dataset families)

For dataset families like CARRA-means and ORAS5 that gate variables
by `level_type` / `product_type` / `time_aggregation`, manual row
authoring is tedious — each variable needs the right gating extras.
Two `curate` flags automate it.

### `earthlens datasets curate ecmwf --all --write`

Walks each uncurated id (`available_datasets − datasets`), reads its
live `form.json`, and seeds a loader-valid row into the right family
shard — every variable present, `units: unknown` as a placeholder.
Per-row `extras` override the parent's `level_type` / `product_type` /
`time_aggregation`, so one dataset key can host multiple
`level_type`-scoped variables. Idempotent: existing rows are skipped.

### `earthlens datasets curate ecmwf --fill-empty --write`

Replaces those `units: unknown` placeholders in place, hydrating each
from a live tiny retrieve. Licence-gated and best-effort — a dataset
whose licence you have not accepted is left untouched rather than
guessed at.

**One probe per placeholder.** A dataset's constraints partition it into
blocks, and a variable is only retrievable under the selectors of a block
that lists it, so the sweep asks for each placeholder variable by name in
turn rather than probing the dataset once. That is what lets a
multi-variable dataset finish: a single probe only ever describes whichever
variable its constraints list first, so a stanza with four placeholders
could never be completed by one.

Because the probe names the variable it wants, a lone data variable coming
back identifies that row outright — the correspondence is established by
the request rather than inferred from the names, and none of the matching
rules below is consulted. A probe that answers with several data variables,
or with none, falls through to those rules, which decline rather than guess.

**Per-variable selectors are recorded.** The block that serves a variable
carries the selectors that variable needs, so where they differ from the
stanza's own `extras:` the difference is written as a per-variable override.
GloFAS is the case this exists for — river discharge and runoff are served
under `timespan: time_mean`, snow depth and soil wetness only under
`instantaneous`:

```yaml
      snow-depth-water-equivalent:
        cds_variable: snow_depth_water_equivalent
        nc_variable: sd
        units: kg m-2
        extras:
          timespan: [instantaneous]
```

**Cost.** One retrieve per placeholder row, each under the same per-request
deadline, so a wide dataset is a long sweep. A dataset whose first probe is
refused is abandoned rather than retried for every remaining row, and rows
filled before that are kept — the pass writes each shard as it goes, so
re-running continues where the last one stopped. Use `--limit` to work
through the catalog in batches.

A dataset whose constraints do not partition by variable at all has no block
to look a row up in; those fall back to a single whole-dataset probe and the
matching rules below.

Matching is deliberately conservative. The confident rules come first: an
exact short-name match, then a token-subset match against the variable's
`long_name`. Only a single leftover slug facing a single unused variable
reaches the last-resort rule, and it pairs them only when the two names
carry evidence of describing the same quantity — shared tokens covering
at least half the slug, or an initialism (`sst` for
`sea-surface-temperature`).

Two limits of that last rule are worth knowing before you trust its
output. Its evidence is a filter, not a proof. The coverage bar rejects
the single-generic-word coincidence — `land-sea-mask` shares only `sea`
with mean sea level pressure — but it cannot police the initialism arm,
which has no shared tokens to measure and will still read `msl` as
m(ask) s(ea) l(and). And it leans heavily on the retrieved variable's
`long_name` — across the curated catalog, 89% of the rows that need more
than an exact name match would fail the check if the retrieve carried no
`long_name` at all. A NetCDF name that an already-hydrated row of the same
dataset claims is withheld from it, so a re-run cannot make two rows fight
over one variable, but most datasets reaching this rule have no hydrated
row yet and so withhold nothing.

A slug that stays ambiguous keeps its `unknown` placeholder, because a
wrong `nc_variable` silently mis-extracts at `aggregate=` time, which is
worse than an obvious gap. Those rows are counted separately in the
command's summary, so they can be curated by hand.

## Adding a new dataset

The typical sequence to extend the catalog with a brand-new dataset
the package doesn't yet support:

1. **Surface the gap.**

    ```bash
    earthlens datasets refresh ecmwf
    earthlens datasets audit ecmwf --coverage
    ```

   The audit prints which datasets in `available_datasets:` have no
   corresponding `datasets:` entry yet, and what their
   `constraints.json` looks like (helps spot the gating extras you'll
   need).

2. **Probe a known-good variable** to discover its NetCDF
   short name and units:

    ```bash
    earthlens datasets validate ecmwf --live      # check every dataset's request is constructible
    earthlens datasets probe ecmwf <dataset-short-name> --deep
    ```

   `probe --deep` submits a real tiny retrieve and reports each
   variable's `nc_variable` and `units`.

3. **Author the YAML entry** under `datasets:`. At minimum:

    ```yaml
    datasets:
      <dataset-short-name>:
        product_type: [<product-type-from-constraints>]
        request_kind: form     # or oceanic_monthly / carra_means
        endpoint: cds          # omit for CDS; ads / ewds / ecds / xds otherwise
        extras: { ... }        # any gating fields the audit flagged
        variables:
          "<short-code>":
            nc_variable: <from the probe>
            units: <from the probe>
            types: state        # or flux for accumulated quantities
    ```

   For gated families with many variables, run
   `earthlens datasets curate ecmwf --all --write` followed by
   `--fill-empty --write` instead.

4. **Verify** with a unit test using `Catalog().get_variable(...)`,
   then a live e2e test that actually downloads a small slice via the
   `ECMWF` backend.

## Adding a single variable to an existing dataset

If the dataset already exists under `datasets:` and you just want to
add one more variable:

1. Probe it for `nc_variable` / `units`:

    ```bash
    earthlens datasets probe ecmwf reanalysis-era5-single-levels --deep
    ```

2. Add the row under the dataset's `variables:` block in the YAML.

3. Confirm: `Catalog().get_variable("reanalysis-era5-single-levels",
   "surface-pressure")` returns the new `Variable`.

## Catalog runtime contract

The runtime contract that the catalog YAML feeds:

| Driver | Picks |
|---|---|
| User's `variables` dict key | Dataset name (must match a `datasets:` entry) |
| User's `variables` dict value | Variable codes (must match `variables:` keys under that dataset) |
| Catalog row's `product_type` | The flavor flag CDS sees |
| Catalog row's `extras` | Last-writer-wins overrides for any request field |
| `temporal_resolution` constructor arg | `pd.date_range` freq + request `time` slots + `day` field presence |

`temporal_resolution` is **purely** a request-shape selector — it does
**not** influence dataset selection, variable selection, or
`product_type`. Those three come exclusively from the catalog.

## See also

- [ECMWF backend reference](ecmwf.md) — class-level API reference.
- `src/earthlens/ecmwf/catalog.py` — loader source; `Catalog.model_post_init` is the authoritative description of how YAML rows become `Variable` instances.
- [The five data stores](datastores.md) — how one catalog spans CDS, ADS, EWDS, ECDS and XDS.
