# National Water Model — catalog & tooling

## Configuration catalog

`earthlens.nwm` ships `nwm_data_catalog.yaml`, loaded by `NWMCatalog`. It
maps each NWM configuration key (`short_range`, `medium_range_mem1`, …)
to an `NWMConfig` row: run hours (`cycles_utc`), forecast horizon /
cadence, the product tokens it publishes, and a full `key_template` that
pins the exact S3 key.

It currently holds **42 forecast configurations** — every `fNNN`
(lead-time) configuration published on `noaa-nwm-pds`:

| Family | Count | Notes |
|--------|-------|-------|
| `short_range` | 6 | CONUS hourly + Alaska / Hawaii / Puerto Rico / coastal |
| `medium_range` | 14 | CONUS + Alaska ensemble members 1–6/7, `no_da` |
| `long_range` | 4 | members 1–4, 720 h, 6-hourly |
| `blend` | 4 | medium-range blend (CONUS / Alaska) |
| `coastal` | 6 | total-water-level (atlgulf / pacific) |
| `forcing` | 8 | meteorological forcing inputs |

The `tmNN` **analyses** (`analysis_assim*`) are intentionally excluded —
they use a time-minus axis that does not fit the `(cycle, step)` model.

```python
from earthlens.nwm import NWMCatalog

cat = NWMCatalog()
len(cat.datasets)                          # 42
sr = cat.get_config("short_range")
sr.products                                # ['channel_rt', 'land', 'reservoir', 'terrain_rt']
sr.cycles_utc, sr.horizon_h                # ([0, 1, ..., 23], 18)
```

!!! note "Sub-hourly regional configs"
    The Hawaii / Puerto Rico short-range configs publish **sub-hourly**
    5-digit steps (`f00015`, `f00030`, …); their `step_cadence_h` /
    `horizon_h` are the raw `fNNN` values, so the inventory's
    `valid_time` (computed as `cycle + step hours`) is only correct for
    the hourly CONUS configs. The fetched files are correct regardless.

## Tooling

| Tool | Purpose |
|------|---------|
| `refresh_nwm_catalog.py` | Enumerate the live bucket and regenerate the catalog. For each forecast config it probes the run hours, samples one cycle, and reconstructs `products` / `horizon` / `cadence` / `key_template` from real keys (so the member-on-token `channel_rt_1`, 5-digit regional steps, and dir-vs-token mismatches all reproduce exactly). |
| `audit_nwm_catalog.py` | Classify the catalog by family / domain / ensemble, and (with `--probe`) HEAD-check live availability for a recent cycle. |

```bash
pixi run -e dev python tools/nwm/refresh_nwm_catalog.py --dry-run
pixi run -e dev python tools/nwm/refresh_nwm_catalog.py            # rewrite the catalog
pixi run -e dev python tools/nwm/audit_nwm_catalog.py --probe      # classify + live check
```
