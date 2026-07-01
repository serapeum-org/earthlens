# SoilGrids — available properties

The `soilgrids` backend ships **11 curated soil properties**, each served as an
independent OGC WCS at `maps.isric.org`. Pick one or more with `variables=`.
Every property is a static global 250 m prediction; the default output CRS is
EPSG:4326.

## Properties, units, and scale factors

Values are **scaled integers** — divide a stored pixel value by the scale
factor to get the conventional unit (the backend does not rescale; the GeoTIFF
holds the raw integers). The no-data sentinel is `-32768`.

| `variables=` id | Property | Stored units | ÷ factor | Conventional units |
|---|---|---|---|---|
| `bdod` | Bulk density of the fine earth fraction | cg/cm³ | 100 | kg/dm³ |
| `cec` | Cation exchange capacity (pH 7) | mmol(c)/kg | 10 | cmol(c)/kg |
| `cfvo` | Volumetric fraction of coarse fragments (> 2 mm) | cm³/dm³ | 10 | cm³/100cm³ (vol %) |
| `clay` | Clay (< 0.002 mm) mass fraction | g/kg | 10 | % |
| `nitrogen` | Total nitrogen | cg/kg | 100 | g/kg |
| `ocd` | Organic carbon density | hg/m³ | 10 | kg/m³ |
| `ocs` | Organic carbon stock (0–30 cm) | t/ha | 10 | kg/m² |
| `phh2o` | Soil pH in H₂O | pH ×10 | 10 | pH |
| `sand` | Sand (0.05–2 mm) mass fraction | g/kg | 10 | % |
| `silt` | Silt (0.002–0.05 mm) mass fraction | g/kg | 10 | % |
| `soc` | Soil organic carbon content | dg/kg | 10 | g/kg |

## Depths

All properties except `ocs` publish the six **standard depth intervals**; pick
them with `depths=`:

`0-5cm`, `5-15cm`, `15-30cm`, `30-60cm`, `60-100cm`, `100-200cm`.

`ocs` (organic carbon stock) is the exception — it is published for a single
`0-30cm` interval only.

## Quantiles / layers

Each `(property, depth)` is served as five statistical layers; pick them with
`quantiles=` (the default is `mean`):

| `quantiles=` id | Layer |
|---|---|
| `mean` | Mean of the prediction (the default) |
| `Q0.05` | 5th percentile of the prediction |
| `Q0.5` | Median (50th percentile) |
| `Q0.95` | 95th percentile of the prediction |
| `uncertainty` | Prediction uncertainty (ratio of the 90 % interval to the median) |

A request expands to the Cartesian product of the selected properties × depths ×
quantiles, one GeoTIFF per cell named `<property>_<depth>_<quantile>.tif`. With
no `depths=` / `quantiles=`, the default is **every depth at the `mean` layer**.

The categorical WRB soil-classification service that SoilGrids also publishes is
out of scope for this numeric property/depth/quantile catalog.
