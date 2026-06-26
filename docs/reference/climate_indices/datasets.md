# Climate indices — available indices

The shipped indices, one canonical source per id. Pass the id(s) in
`variables=` (e.g. `variables=["oni", "nao"]`). The `units` column is
`degC` for SST-anomaly indices and `std` for the standardised pattern
indices. The list below mirrors the bundled
`climate_indices_data_catalog.yaml`; query it live with
`earthlens.climate_indices.Catalog().available()`.

| id | source | dialect | units | description |
|---|---|---|---|---|
| `oni` | noaa-psl | psl | degC | Oceanic Niño Index (3-month running mean of Niño 3.4 ERSSTv5 SST anomaly) |
| `nina34` | noaa-psl | psl | degC | Niño 3.4 sea-surface-temperature anomaly |
| `soi` | noaa-psl | psl | std | Southern Oscillation Index |
| `nao` | noaa-psl | psl | std | North Atlantic Oscillation (CPC) |
| `ao` | noaa-psl | psl | std | Arctic Oscillation |
| `pna` | noaa-psl | psl | std | Pacific/North American teleconnection pattern |
| `pdo` | noaa-psl | psl | std | Pacific Decadal Oscillation |
| `tni` | noaa-psl | psl | degC | Trans-Niño Index |
| `censo` | noaa-psl | psl | std | Bivariate ENSO index (CENSO) |
| `amo` | knmi-climexp | climexp | degC | Atlantic Multidecadal Oscillation (detrended, ERSSTv5) |

## Sources and dialects

- **noaa-psl** (`psl` dialect) — NOAA PSL `correlation/<index>.data`
  series. Year + 12 calendar-month rows; the missing-value sentinel
  varies per file and is read from the lone numeric line after the data.
- **knmi-climexp** (`climexp` dialect) — KNMI Climate Explorer
  `<id>.dat` monthly grids. Year + 12 months (a trailing annual-mean
  column is dropped when present); sentinel `-999.9`.

## Notes

- **One canonical source per index.** Several of these indices (NAO, SOI,
  AMO, …) are published by *both* sources with slightly different
  definitions; the catalogue picks one canonical source per id. A
  per-request `source=` override is a future addition.

- **Not shipped (yet).** The bi-monthly **MEI.v2** series is excluded
  from this set because its columns are 2-month seasons (DJ, JF, …), not
  calendar months, so it does not fit the calendar-monthly schema. The
  `YYYYMMDD value` climexp long form (e.g. PDO on climexp) is likewise a
  follow-on; PDO here comes from NOAA PSL.
