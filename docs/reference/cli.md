# Command-line interface (`earthlens`)

The `earthlens` console script is a **federated, read-first catalog tool**: it queries every backend's
bundled catalog from one shell entry point, and adds maintainer verbs for keeping those catalogs in sync
with what each provider serves live. It never downloads science data — for that you use the Python
`EarthLens` facade.

## Install & invoke

The CLI ships with the base package — `typer` + `rich` are core dependencies, so a plain install gives a
working `earthlens` command:

```bash
pip install earthlens
```

Two equivalent entry points:

```bash
earthlens --help                 # the installed console script
python -m earthlens.cli --help   # the same app, no script on PATH
```

The app has two command groups:

| Group | Purpose |
|-------|---------|
| `earthlens datasets …` | find, inspect, and maintain datasets across all providers |
| `earthlens providers …` | list the backends and probe their SDK / catalog health |

Running a group with no command (or `--help`) prints its usage.

## Concepts that apply to every command

### Provider selectors

Anywhere a command takes a provider, you can use the canonical id **or an alias**, and (for the
multi-provider verbs) a comma-separated list or the literal `all`:

```bash
earthlens datasets where era5 -p s3            # canonical id
earthlens datasets where era5 -p amazon-s3     # alias for s3
earthlens datasets refresh stac,openeo         # a comma list
earthlens datasets validate all                # every provider
```

Canonical ids: `airnow`, `argo`, `asf`, `bathymetry`, `caravan`, `catrare`, `chc`, `climate-indices`, `cmems`, `dem`, `earthdata`,
`ecmwf`,
`eea-aq`, `erddap`, `eumetsat`, `fabdem`, `fdsn`, `firms`, `flodis`, `flopros`, `gbif`, `gdacs`, `gee`, `ghsl`, `glaciers`, `goes`, `hanze`, `hdx`, `iucn`, `jaxa`, `jrc-flood`, `nwm`, `nwp`,
`obis`, `openaq`, `openeo`, `overture`, `pvgis`, `radar`, `risk-indicators`, `s3`, `sensor-community`, `sentinel_hub`, `soilgrids`,
`stac`, `tropycal`, `usgs_water`, `wdpa`, `worldpop`. Common aliases include `amazon-s3`, `chirps`,
`google-earth-engine`, `argo-floats`/`argopy`, `ioos` (erddap), `gebco`/`etopo` (bathymetry),
`climate_indices`/`teleconnections` (climate indices), `copernicus-dem`/`cop-dem`/`elevation` (dem),
`fab-dem`/`bare-earth-dem` (fabdem), `efhm`/`jrc-flood-hazard`/`european-flood-hazard` (jrc-flood),
`sentinel-hub`/`sentinelhub`, `nexrad`,
`nwis`/`usgs-water`, `world-pop`, `human-settlement`/`ghs`, `solar-pv` (pvgis),
`thinkhazard`/`inform`/`gfw`/`global-forest-watch` (risk indicators),
`rgi`/`glims`/`wgms` (glaciers), `isric` (soilgrids),
`caravan-grdc`/`grdc-caravan` (caravan - the open GRDC discharge route),
`earth-search`/`planetary-computer`/`cdse` (STAC endpoints). An unknown selector is a **usage error**
(exit code `2`).

### Output modes

Most read commands accept:

- *(default)* — a rich table to stdout, with a dim summary line to stderr.
- `--json` / `-j` — a parseable JSON array/object to stdout (for piping).
- `--ids-only` — bare `provider<TAB>id` lines (for shell pipelines).

`--json` and `--ids-only` are mutually exclusive (using both is a usage error).

### Provider precedence

Results are ordered by a configurable provider priority. Override it with the
`EARTHLENS_PROVIDER_PRIORITY` environment variable (a comma-separated provider list); providers not
listed sort after the listed ones:

```bash
EARTHLENS_PROVIDER_PRIORITY=gee,stac,s3 earthlens datasets where ndvi
```

### Exit codes

- `0` — success.
- `1` — a meaningful "no result / failed" outcome a pipeline can branch on (e.g. `where` found nothing;
  `curate` on an unsupported provider; `--strict` found drift / issues).
- `2` — a usage error (unknown provider, conflicting flags, missing argument).

### What hits the network

`where` / `search` / `list` / `show` / `facets` read only the **bundled** catalogs (offline, instant) —
unless you pass `--include-available`, which also scans each backend's full upstream id index. The
maintainer verbs `refresh` / `audit` / `probe` / `validate --live` go to the **network / SDKs**;
`validate` (without `--live`) and `curate` (without `--write`/`--hydrate`/`--fill-empty`) are otherwise
local. Credentialed paths read keys from the environment / `~/.cdsapirc` (e.g. `FIRMS_MAP_KEY`,
`OPENAQ_API_KEY`, GEE service account, CDS key) — see each backend's authentication page.

---

## `earthlens datasets`

### `where <name>` — which provider(s) expose a dataset

The headline use case. Scans every backend's catalog for `name`: an exact dataset-id match wins,
otherwise a case-insensitive substring match over id and title. Exits `1` (with a did-you-mean hint) when
nothing matches, so it composes in pipelines.

| Option | Meaning |
|--------|---------|
| `-p, --provider` | restrict to these providers (repeatable / comma-separated) |
| `--exact` | match the dataset id exactly (no substring search) |
| `--include-available` | also search each backend's full upstream id index (slower) |
| `-j, --json` | emit a JSON array of matches |
| `--ids-only` | emit `provider<TAB>id` lines |

```bash
earthlens datasets where era5                       # across all providers
earthlens datasets where era5 -p s3 --exact         # only the exact id, in s3
earthlens datasets where buildings -p overture -j   # JSON for piping
```

### `search [query]` — free-text + faceted search

Combines a free-text `query` (over provider / id / title) with repeatable `--filter facet=value`
narrowing (logical AND). A blank query matches everything.

| Option | Meaning |
|--------|---------|
| `-p, --provider` | restrict to these providers |
| `-f, --filter` | narrow by facet, e.g. `--filter cadence=daily` (repeatable; facets: `provider`, `cadence`, `resolution`, `license`) |
| `-n, --limit` | cap the rows shown (`0` = no cap) |
| `--count` | print only the match count (`{"count": N}` with `--json`) |
| `--facets-only` | print per-facet value counts instead of rows (honours `--json`) |
| `--include-available` | also search the full upstream id index |
| `-j, --json` / `--ids-only` | output modes |

```bash
earthlens datasets search precipitation --filter cadence=daily
earthlens datasets search -p gee --facets-only        # what facet values exist
earthlens datasets search -p s3 --count               # just a number
```

### `list` — list curated datasets

Lists the curated datasets (optionally widened with `--include-available`).

| Option | Meaning |
|--------|---------|
| `-p, --provider` | restrict to these providers |
| `--full` | add cadence + resolution columns |
| `--include-available` | include each backend's full upstream id index |
| `-j, --json` / `--ids-only` | output modes |

```bash
earthlens datasets list -p chc --full
earthlens datasets list -p gee --include-available --ids-only | wc -l
```

### `show <provider> <dataset>` — full record for one dataset

Prints the complete curated record (every field the backend's pydantic model carries).

| Option | Meaning |
|--------|---------|
| `-j, --json` | dump the full record as JSON |

```bash
earthlens datasets show chc chirps-daily
earthlens datasets show gee NASA/GDDP-CMIP6 --json
```

### `facets` — value distribution across facets

With no `--values`, prints each facet and its distinct-value count; with `--values <facet>`, enumerates
that facet's values and their counts.

| Option | Meaning |
|--------|---------|
| `--values` | enumerate the distinct values of this facet |
| `-p, --provider` | restrict to these providers |
| `-j, --json` | JSON output |

```bash
earthlens datasets facets -p s3                  # facet summary
earthlens datasets facets --values cadence -j    # distinct cadence values
```

### `refresh <providers>` — diff the bundled index against live, optionally persist

Goes to the network: fetches each provider's live id universe and diffs it against the bundled
`available_*` index, reporting new / removed ids. With `--write` it rewrites the bundled index in place
(a maintainer action — review the git diff before committing). Providers without a public listing endpoint
report `unsupported`.

| Option | Meaning |
|--------|---------|
| `--show-ids` | list the actual new / removed ids, not just counts |
| `--write` | rewrite the bundled `available_*` index from the live fetch |
| `--tiles` | regenerate a provider's bundled tile grid (ghsl) instead of the id index |
| `-j, --json` | JSON output |

**Safety**: a `--write` whose live fetch returns **zero ids** (a transient outage / unexpected body) is
refused rather than blanking a populated index — the skip is reported in the outcome detail.

```bash
earthlens datasets refresh gee                   # show drift counts
earthlens datasets refresh stac --show-ids       # which ids changed
earthlens datasets refresh gee --write           # regenerate available_datasets
earthlens datasets refresh ghsl --tiles          # regenerate the tile grid
```

### `audit <providers>` — curated-vs-live drift (and coverage)

Like `refresh`, but focused on the **curated** rows: flags `broken` curated ids the provider no longer
serves (the drift a CI gate fails on) and, informationally, live ids missing from the index.

| Option | Meaning |
|--------|---------|
| `--strict` | exit non-zero if any curated dataset is no longer served live |
| `--coverage` | switch to a **curation-coverage** report — classify the available universe into DONE / addressable / thin / table / missing, and list the highest-value ids to curate next (`gee`, `erddap`) |
| `--serveable` | switch to a **serveability** report — list the curated rows whose own selectors match no combination the store offers, so a download returns nothing (providers publishing the `serveability_auditor` role; `ecmwf` today) |
| `-j, --json` | JSON output |

`--serveable` answers a narrower question than drift. Drift asks whether the dataset still exists;
`--serveable` asks whether each row's request is answerable — a row can name a real variable with the
right unit and still fetch nothing, because the selectors it sends (its stanza's `extras` merged with
its own) match no single constraints block. It is unauthenticated and issues no retrieve.

```bash
earthlens datasets audit stac --strict            # CI drift gate
earthlens datasets audit gee --coverage           # what's worth curating next
earthlens datasets audit ecmwf --serveable        # rows whose request answers nothing
earthlens datasets audit ecmwf --serveable --strict --json   # as a CI gate, piped
```

### `validate <providers>` — per-entry checks (offline lint, optional live)

For curated-enumeration providers (no discoverable upstream index, so `refresh`/`audit` do not apply),
this checks each curated entry. Offline by default (a structural lint); `--live` adds a
network/SDK reachability check (an S3 object resolves, an Overture type serves a `sources` column, a GHSL
artefact HEADs 200, an openEO recipe's base collection / processes are live, an NWP model's latest cycle
is reachable, an ECMWF dataset's minimal request is constraint-valid, the NEXRAD feed is reachable).

| Option | Meaning |
|--------|---------|
| `--strict` | exit non-zero if any curated entry has an issue |
| `--live` | also run the live reachability check (network / SDK; opt-in) |
| `-j, --json` | JSON output |

```bash
earthlens datasets validate nwp                   # offline structural lint
earthlens datasets validate s3 --live --strict    # reachability gate for CI
```

### `probe <provider> <dataset>` — sample one dataset's schema

Samples a single dataset to seed its catalog metadata: the per-asset / band / variable / column schema.
The light path uses a public endpoint; `--deep` swaps in a credentialed sampler that reads the *real*
on-disk schema (e.g. CMEMS opens the NetCDF, ECMWF retrieves a tiny slice, Earthdata samples a granule).

| Option | Meaning |
|--------|---------|
| `--deep` | use the credentialed deep sampler instead of the light public probe |
| `-j, --json` | JSON output |

```bash
earthlens datasets probe stac sentinel-2-l2a      # band schema from a sample item
earthlens datasets probe cmems cmems_mod_glo_phy --deep   # real NetCDF variables
earthlens datasets probe nwp icon-global          # which band tokens are in the .idx
```

### `curate <provider> [upstream_id]` — author a curated row

The authoring companion to `probe`: fetches one upstream id's metadata and emits a paste-ready
`datasets:` YAML row (inferring `output_kind` / `format` / bands where it can). By default it prints the
row to vet and paste; `--write` appends it into the catalog file. Supported providers: `earthdata`,
`hdx`, `usgs_water`, `eumetsat`, `gee`, `jaxa`, `erddap`.
`caravan` is deliberately absent: its rows are pinned Zenodo records whose
archive layout a human has to inspect before bumping, so there is nothing to
seed a stanza from. It supports `refresh` and `validate`.

| Option | Meaning |
|--------|---------|
| `--key` | friendly catalog key for the row (default: the upstream id) |
| `--minimal` | emit a placeholder row without a live fetch (where supported) |
| `--hydrate` | gee: read bands live from Earth Engine (needs GEE creds) |
| `--fill-empty` | gee: bulk-hydrate **every** empty-band curated row in place (needs `--write` + creds; ignores `upstream_id`) |
| `--limit` | gee `--fill-empty`: only hydrate the first N rows (`0` = all) |
| `--write` | append the seeded row into the catalog file (else print only) |
| `--target` | per-family file stem under `catalog/` to write into (sharded providers; gee auto-picks, others default to `--daac` / `--group`) |
| `--version`, `--cmr-provider`, `--daac`, `--cloud-hosted` | earthdata seed options |
| `--name`, `--units`, `--group`, `--service` | usgs_water seed options |
| `--server` | erddap: ERDDAP base URL to look the dataset up on (defaults to the catalog's curated servers) |
| `-j, --json` | emit the seeded row as JSON |

```bash
earthlens datasets curate usgs_water 00060 --key discharge --units ft3/s
earthlens datasets curate gee NASA/GDDP-CMIP6 --write     # auto-categorised file
earthlens datasets curate earthdata GPM_3IMERGHH --version 07 --cmr-provider GES_DISC
earthlens datasets curate gee --fill-empty --write       # bulk-hydrate placeholders
earthlens datasets curate jaxa JAXA.AW3D30.v3.2           # jaxa-earth STAC seed
earthlens datasets curate jaxa 11001002                   # G-Portal numeric id seed
earthlens datasets curate erddap erdMBsstd8day            # seed from a server's /info
earthlens datasets curate erddap myDataset --server https://my.erddap/erddap --write --target coastwatch
```

---

## `earthlens providers`

### `list` — the backends and their health

Lists every backend (id, aliases, pip extra). With `--check` it additionally probes each backend's SDK
availability and bundled catalog dataset count.

| Option | Meaning |
|--------|---------|
| `--check` | probe SDK availability + catalog dataset counts (slower) |
| `-j, --json` | JSON output |

```bash
earthlens providers list
earthlens providers list --check          # which optional SDKs are installed
```

---

## Verb cheat-sheet

| I want to… | Use |
|------------|-----|
| find which provider has a dataset | `datasets where <name>` |
| explore / filter the catalog | `datasets search`, `datasets facets` |
| see one dataset's full record | `datasets show <provider> <id>` |
| check what the provider serves now vs the bundled index | `datasets refresh <p>` |
| gate CI on curated-vs-live drift | `datasets audit <p> --strict` |
| find curated rows the store cannot answer | `datasets audit <p> --serveable` |
| see what's worth curating next | `datasets audit gee --coverage` |
| lint / reachability-check curated entries | `datasets validate <p> [--live] [--strict]` |
| inspect a dataset's real schema | `datasets probe <p> <id> [--deep]` |
| author a new curated row | `datasets curate <p> <id> [--write]` |
| see backends + installed SDKs | `providers list [--check]` |
