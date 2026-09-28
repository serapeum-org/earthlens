# `geemap` — package analysis

What the `geemap` package is and what functionalities it provides, and how it
relates to the earthlens GEE backend. Companion to
[`gee-missing-functionalities.md`](./gee-missing-functionalities.md) and
[`gee-enhancement-tasks.md`](./gee-enhancement-tasks.md).

Based on introspection of **`geemap==0.37.2`** (installed and inspected).
`geemap` is **not** a dependency of earthlens — it appears in the planning docs
only as a "folium/geemap-style" reference point.

---

## What `geemap` is

`geemap` is a Python package for **interactive geospatial analysis and
visualization with Google Earth Engine**, built primarily for Jupyter
notebooks. Where the raw `earthengine-api` (`ee`) is a headless compute client,
geemap wraps it with **interactive maps, visualization, data conversion,
export, timelapse animation, charting, and ML helpers**. Created/maintained by
Qiusheng Wu; the de-facto interactive GEE toolkit in the research community.

It is a *large* package: **27 submodules, ~455 top-level functions, and a `Map`
class with 173 methods** (68 of them `add_*`).

Submodules: `ai, basemaps, cartoee, chart, cli, colormaps, common, conversion,
core, coreutils, datasets, deck, ee_tile_layers, examples, foliumap, geemap,
kepler, legends, map_widgets, maplibregl, ml, osm, plot, plotlymap, report,
timelapse, toolbar`.

---

## Functionalities

### 1. Interactive mapping (the core)

The `Map` class is an `ipyleaflet`-based interactive map. 68 `add_*` methods
layer almost anything:

- **GEE layers** — `addLayer` / `add_ee_layer` (with vis params),
  `add_time_slider`, `add_styled_vector`, `add_legend`, `add_colorbar`.
- **Local & web data** — `add_raster` (local GeoTIFF via localtileserver),
  `add_cog_layer` / `add_cog_mosaic` (Cloud-Optimized GeoTIFFs),
  `add_stac_layer`, `add_wms_layer`, `add_tile_layer` / `add_xyz_service`,
  `add_netcdf`, `add_pmtiles`, `add_vector` / `add_gdf` / `add_geojson` /
  `add_shp`.
- **UI controls** — `add_inspector` (click-to-query pixel values),
  `add_layer_manager`, `add_draw_control` (digitize geometries),
  `add_search_control` (geocode), `add_minimap`, `split_map` (before/after
  swipe), `add_time_slider`.
- **OSM** — `add_osm_from_{address,place,point,bbox,polygon,geocode,view}`.
- **Export the map** — `to_html`, `to_image`, `to_streamlit`, `to_gradio`.

**Multiple rendering backends**: `foliumap` (static/folium), `maplibregl`
(3D/MapLibre), `kepler` (kepler.gl), `deck` (pydeck), `plotlymap`, and
`cartoee` (publication-quality static maps via Matplotlib + Cartopy).

### 2. Basemaps & tile services

Hundreds of basemaps (via `xyzservices`), plus `get_wms_layers`,
`search_xyz_services`, `search_qms` (QuickMapServices), COG/STAC/PMTiles
tiling, and TiTiler endpoints.

### 3. Data conversion (a major strength)

Bidirectional converters between `ee` and essentially every geospatial format:

- **ee ↔ vector** — `ee_to_geojson` / `geojson_to_ee`, `ee_to_shp` / `shp_to_ee`,
  `ee_to_gdf` / `gdf_to_ee` (`geopandas_to_ee`), `ee_to_df` / `df_to_ee`
  (`pandas_to_ee`), `kml_to_ee`, `csv_to_ee`, `osm_to_ee`, `postgis_to_ee`.
- **ee ↔ raster/array** — `ee_to_geotiff`, `ee_to_numpy`, `ee_to_xarray` (via
  `xee`), `numpy_to_ee`, `numpy_to_cog`, `xarray_to_raster`.
- **Earth Engine Code Editor migration** — `js_to_python`, `js_snippet_to_py`,
  `js_to_python_dir` (convert JavaScript GEE scripts to Python) — a genuinely
  unique feature.

### 4. Export & download

Local and cloud export helpers wrapping `ee` batch tasks:

- `ee_export_image` / `ee_export_vector` / `ee_export_video` and their
  `_to_drive` / `_to_cloud_storage` / `_to_asset` / `_to_feature_view` variants.
- `download_ee_image` (handles the 32768-px cap by **tiling + mosaicking**),
  `download_ee_image_tiles_parallel`, `download_ee_image_collection`,
  `download_ned`, `download_3dep_lidar`.

### 5. Zonal statistics & extraction

- `zonal_stats` / `zonal_statistics` / `zonal_statistics_by_group` (image →
  per-polygon stats), `image_stats_by_zone`.
- `extract_values_to_points`, `extract_timeseries_to_point`, `extract_transect`,
  `extract_pixel_values`, `random_sampling`.

### 6. Timelapse & animation (a signature feature)

One-call animated GIFs/MP4s from major collections:

- `landsat_timelapse`, `sentinel1_timelapse`, `sentinel2_timelapse`,
  `modis_ndvi_timelapse`, `modis_ocean_color_timelapse`, `goes_timelapse` /
  `goes_fire_timelapse`, `naip_timelapse`, plus `create_timelapse`.
- GIF tooling — `add_text_to_gif`, `add_progress_bar_to_gif`,
  `combine_gif_with_chart`, `gif_to_mp4`, `reduce_gif_size`, `make_gif`.

### 7. Charting & plotting

- `geemap.chart` — feature/image time-series and histogram charts.
- `bar_chart`, `line_chart`, `pie_chart`, `histogram`, `plot_raster`,
  `plot_raster_3d`.
- `cartoee` — publication-quality maps with gridlines, scale bars, north
  arrows, colorbars.
- `ts_inspector` — interactive time-series inspector on the map.

### 8. Machine learning (`geemap.ml`)

Bridges scikit-learn and Earth Engine:

- `rf_to_strings` / `trees_to_csv` / `tree_to_string` — export a trained sklearn
  RandomForest to EE-compatible decision-tree strings.
- `strings_to_classifier` / `csv_to_classifier` / `fc_to_classifier` — rebuild
  an `ee.Classifier` from those, so you can **train locally and classify
  server-side** (or vice versa). `export_trees_to_fc`.

### 9. Cloud / COG / STAC utilities

`cog_bands` / `cog_bounds` / `cog_stats` / `cog_tile` / `cog_mosaic` /
`cog_validate`, `stac_assets` / `stac_bands` / `stac_stats` / `stac_tile` /
`stac_pixel_value`, `local_tile_*`.

### 10. Curated dataset helpers

Shortcuts for popular datasets: `dynamic_world` / `dynamic_world_s2`, NAIP
(`annual_NAIP`, `find_NAIP`), NWI wetlands, HUC watersheds
(`filter_HUC08/10`), US Census (`add_census_data`), Planet basemaps
(`planet_monthly`, `planet_quarterly`, …), MODIS, GOES.

### 11. LiDAR

`read_lidar`, `write_lidar`, `view_lidar`, `convert_lidar`,
`download_3dep_lidar` (via PDAL / laspy).

### 12. Colors, legends, colormaps

`geemap.colormaps`, `create_colorbar`, `create_legend`, `legend_from_ee`,
`get_palette_colors`, hex/rgb conversion, `save_colorbar`.

### 13. App / UI & misc plumbing

- Web-app integration — `to_streamlit`, `to_gradio`, `html_to_streamlit`,
  `html_to_gradio`.
- `ee_initialize` (auth convenience), `ee_search` / `search_ee_data` (dataset
  discovery), `geocode`, `create_grid` / `fishnet`, a CLI, a `toolbar`, report
  generation (`Report`), and an `ai` submodule (newer LLM-assisted features).

---

## How it relates to earthlens

They sit at **opposite ends** of the GEE workflow and are complementary:

| | **earthlens GEE backend** | **geemap** |
|---|---|---|
| Purpose | headless **data acquisition** (download analysis-ready GeoTIFFs) | interactive **exploration, visualization, analysis** in notebooks |
| Environment | scripts, CI, servers | Jupyter / Colab, Streamlit |
| Output | files on disk, offline catalog | live maps, charts, GIFs, thumbnails |
| Dependencies | minimal (`ee` + `pyramids`) | heavy (ipyleaflet, folium, geopandas, rasterio, localtileserver, xee, …) |

Notably, geemap **already implements much of what the gap analysis flagged as
missing** in earthlens — interactive tiles (`getMapId`), thumbnails,
timelapse/video, table/vector exports, zonal stats, the sklearn ↔
`ee.Classifier` ML bridge, and `ee` ↔ pandas/geopandas/xarray conversion. It
does it all in the interactive/notebook idiom, and pulls in a large dependency
tree to do so — which is exactly why earthlens keeps those out of its lean
acquisition core and leaves them to the `.client` escape hatch or a companion
tool.

### Overlap with the enhancement tasks

geemap is prior art for several tasks in `gee-enhancement-tasks.md`:

- **Task A1 (zonal statistics)** ↔ geemap `zonal_stats` / `zonal_statistics`.
- **Task A3 (thumbnails)** / **C2 (server thumbnails, tiles)** ↔ geemap
  `get_image_thumbnail`, `add_ee_layer`, `ee_tile_layer`.
- **Task B4 (supervised classification)** ↔ geemap `ml` (sklearn ↔
  `ee.Classifier`).
- **Task B2 (raster ↔ vector)** and IO conversions ↔ geemap `ee_to_*` / `*_to_ee`.
- **Task C3 (table/vector exports)** ↔ geemap `ee_export_vector_to_*`.
- **Task C4 (video/timelapse)** ↔ geemap timelapse family.

The design question these raise: for interactive/notebook use, depend on geemap
rather than reimplement; for the headless acquisition core, keep the lean
client-side (`pyramids`) implementations the tasks describe. A feature-by-feature
decision per task is worth recording before implementing the Tier C items.

---

*Generated from introspection of `geemap==0.37.2` vs. the earthlens GEE backend
source.*
