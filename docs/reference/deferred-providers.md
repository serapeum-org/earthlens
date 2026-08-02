# Deferred providers

Some data sources were **evaluated and deliberately not integrated** into earthlens. They are listed here so the
project surface stays honest: these are *known and considered*, not overlooked. The usual reasons are a **licence
trap** (non-commercial / restricted redistribution), **no stable programmatic access** (no API or no maintained
Python client), or **commercial-only** access.

If you need one of these datasets, follow the manual route in its row. Always **confirm the current licence and
terms** on the provider's own site before use — terms change, and the notes below reflect the state at evaluation
time, not a legal guarantee.

## Summary

| Provider | Domain | Why deferred | How to get the data |
|---|---|---|---|
| **GVP** (Smithsonian Global Volcanism Program) | Volcanoes / eruptions | Reference database, no stable bulk API / maintained Python client; reuse is attribution-bound | Browse or export from the [Volcanoes of the World](https://volcano.si.edu/) site; a WFS/GeoServer endpoint exists for one-off pulls |
| **GRDC** (Global Runoff Data Centre) | River discharge (in-situ) | Raw portal has **no API** (web map + form → email delivery) and **prohibits redistribution**, so the *full* database stays deferred. **But** GRDC's openly-licensed subset ships as **GRDC-Caravan** (CC-BY-4.0, redistributable) | **Open route (recommended):** `earthlens.caravan` with `dataset="grdc"` (or the `grdc-caravan` facade key) fetches it from Zenodo — DOI [`10.5281/zenodo.15349031`](https://doi.org/10.5281/zenodo.15349031) (v0.6, 5,356 catchments, 1950–2023). The remaining restricted stations are only available manually via the [GRDC portal](https://grdc.bafg.de/) |
| **IBAT** (Integrated Biodiversity Assessment Tool) | Protected areas / KBA / Red List | **Commercial / subscription** for most access tiers; free tier is limited and NC | Use an [IBAT](https://www.ibat-alliance.org/) subscription; for the underlying open layers use `earthlens.wdpa` (Protected Planet) and `earthlens.iucn` instead |
| **Map of Life** (MoL) | Species range maps | Per-dataset licensing; many layers are **NC / research-only** | Use the [Map of Life](https://mol.org/) portal / its API under the specific dataset's terms; for open occurrences use `earthlens.gbif` / `earthlens.obis` |
| **IQAir** (AirVisual) | Air quality (ground) | **Commercial API** with paid tiers; free tier heavily rate-limited; redistribution restricted | Use the paid [IQAir/AirVisual API](https://www.iqair.com/air-pollution-data-api); for open AQ use `earthlens.openaq` |
| **Renewables.ninja** | Solar / wind power time series | **CC-BY-NC** (non-commercial) + rate-limited; academic use | Register at [renewables.ninja](https://www.renewables.ninja/) and pull per-site series under the NC licence; for open solar/wind resource use `earthlens.pvgis` / `earthlens.nrel` / `earthlens.solar_wind_atlas` |

## Notes

- **NC (non-commercial) sources** — MoL (many layers), Renewables.ninja, and IBAT's free tier — are excluded from
  the default integration path because earthlens makes no assumption about a user's commercial status. Where an
  **open-licensed** alternative covers the same need, it is named in the table (e.g. WDPA/IUCN for protected areas,
  GBIF/OBIS for occurrences, OpenAQ for air quality, PVGIS/NREL for renewables).
- **Restricted-access sources** — the **raw GRDC portal** has no API (form → email delivery) and prohibits
  redistribution, so the *full* database cannot be wrapped behind an anonymous `download()`. **However**, GRDC's
  openly-licensed stations are published as the **GRDC-Caravan** extension (CC-BY-4.0), which *is* fetchable and
  redistributable from Zenodo — that is what [`earthlens.caravan`](caravan/introduction.md) wraps, and it is the
  recommended route for open GRDC discharge. Only the restricted stations remain portal-only. Note GRDC-Caravan is
  a **versioned historical archive**, not a live feed; for current discharge use `earthlens.usgs_water` (US
  near-real-time) or GloFAS via `earthlens.ecmwf`.
- **No-stable-client sources** — GVP — could be revisited if a maintained API/Python client and clear bulk-reuse
  terms appear.

These deferrals are tracked as `L15` in the low-tier roadmap. Re-evaluate a row if its licence or access model
changes.
