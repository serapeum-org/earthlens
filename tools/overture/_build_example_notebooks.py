"""Build the Overture example notebooks under `docs/examples/overture/`.

Authoring the notebooks programmatically with `nbformat` keeps the JSON
valid and the cell content reviewable in one place. Run once to (re)write
the notebooks, then execute them with nbconvert / the `notebooks` nbval
task. Not part of the installed package.

    pixi run -e dev python tools/overture/_build_example_notebooks.py
"""

from __future__ import annotations

from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "examples" / "overture"

#: A tiny Times-Square-block bbox — small, fast, reliably non-empty.
BBOX_SETUP = (
    "LAT_LIM = [40.757, 40.759]  # [south, north]\n"
    "LON_LIM = [-73.987, -73.984]  # [west, east] — a Times Square block\n"
    'OUT = "_overture_out"'
)


def _nb(cells: list) -> nbformat.NotebookNode:
    nb = new_notebook(cells=cells)
    nb.metadata.update(
        {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
        }
    )
    return nb


def _catalog_explorer() -> nbformat.NotebookNode:
    return _nb(
        [
            new_markdown_cell(
                "# Overture — browse the catalog (no network)\n\n"
                "The bundled `Catalog` maps four friendly themes to their Overture "
                "feature types. This notebook explores it without touching the "
                "network — handy before you pick a theme + bbox to download."
            ),
            new_code_cell(
                "from earthlens.overture import Catalog\n\n"
                "cat = Catalog()\n"
                "cat.themes()"
            ),
            new_code_cell(
                "for name in cat.themes():\n"
                "    theme = cat.get_theme(name)\n"
                "    print(f\"{name:16s} types={theme.types} default={theme.default_type!r} \"\n"
                "          f\"geometry={theme.geometry}\")"
            ),
            new_markdown_cell(
                "Each theme records the licenses its rows typically carry — note "
                "that every theme can contain `ODbL-1.0` (OpenStreetMap-derived) "
                "rows, which the backend surfaces per row."
            ),
            new_code_cell("cat.get_theme('buildings').licenses"),
            new_markdown_cell(
                "All six Overture themes are curated. The `available_datasets` "
                "index lists every queryable feature type, and each curated "
                "theme's types are a subset of it:"
            ),
            new_code_cell(
                "curated = {t for theme in cat.datasets.values() for t in theme.types}\n"
                "available = set(cat.available_types())\n"
                "print('all types    :', cat.available_types())\n"
                "print('curated types:', sorted(curated))\n"
                "print('every type curated?', curated == available)"
            ),
            new_markdown_cell(
                "The informational release index (newest first). The SDK "
                "auto-targets the newest when you do not pin a `release`."
            ),
            new_code_cell("cat.available_releases, cat.latest_release()"),
            new_markdown_cell(
                "An unknown theme raises with a did-you-mean hint:"
            ),
            new_code_cell(
                "try:\n"
                "    cat.get_theme('building')\n"
                "except ValueError as exc:\n"
                "    print(exc)"
            ),
        ]
    )


def _places_quickstart() -> nbformat.NotebookNode:
    return _nb(
        [
            new_markdown_cell(
                "# Overture — Places quickstart (live)\n\n"
                "Fetch points of interest for a small bbox and read them back. "
                "Overture Places is public and anonymous — no credentials."
            ),
            new_code_cell(BBOX_SETUP),
            new_code_cell(
                "from earthlens.earthlens import EarthLens\n\n"
                "paths = EarthLens(\n"
                '    data_source="overture",\n'
                '    variables={"places": []},  # [] -> the theme primary type, "place"\n'
                "    lat_lim=LAT_LIM,\n"
                "    lon_lim=LON_LIM,\n"
                "    path=OUT,\n"
                ").download()\n"
                "paths"
            ),
            new_code_cell(
                "import geopandas as gpd\n\n"
                "gdf = gpd.read_parquet(paths[0])\n"
                "print(len(gdf), 'places;  CRS', gdf.crs.to_epsg())\n"
                "gdf[['id', 'license_id']].head()"
            ),
            new_markdown_cell(
                "Place names live in the nested `names` struct; pull the primary "
                "name out for a quick look."
            ),
            new_code_cell(
                "def primary_name(names):\n"
                "    if isinstance(names, dict):\n"
                "        return names.get('primary')\n"
                "    return None\n\n"
                "gdf['name'] = gdf['names'].apply(primary_name)\n"
                "gdf[['name', 'license_id']].dropna(subset=['name']).head(10)"
            ),
        ]
    )


def _buildings_footprints() -> nbformat.NotebookNode:
    return _nb(
        [
            new_markdown_cell(
                "# Overture — Building footprints (live)\n\n"
                "Fetch building footprints for a neighbourhood block and plot "
                "them. Buildings is the largest theme (2.3 B rows globally), so a "
                "**bounded bbox is required** — the backend guards against an "
                "oversized box."
            ),
            new_code_cell(BBOX_SETUP),
            new_code_cell(
                "from earthlens.earthlens import EarthLens\n\n"
                "paths = EarthLens(\n"
                '    data_source="overture",\n'
                '    variables={"buildings": []},\n'
                "    lat_lim=LAT_LIM,\n"
                "    lon_lim=LON_LIM,\n"
                "    path=OUT,\n"
                '    file_format="geoparquet",\n'
                ").download()\n"
                "paths"
            ),
            new_code_cell(
                "import geopandas as gpd\n\n"
                "gdf = gpd.read_parquet(paths[0])\n"
                "print(len(gdf), 'footprints;  geom', gdf.geometry.geom_type.unique())\n"
                "gdf[['id', 'license_id']].head()"
            ),
            new_markdown_cell("A quick plot of the footprints:"),
            new_code_cell(
                "ax = gdf.plot(figsize=(5, 5), edgecolor='black', linewidth=0.3)\n"
                "ax.set_title('Overture building footprints')\n"
                "ax.set_axis_off()"
            ),
            new_markdown_cell(
                "The guard rejects a whole-Earth buildings request rather than "
                "trying to read billions of rows:"
            ),
            new_code_cell(
                "try:\n"
                "    EarthLens(\n"
                '        data_source="overture",\n'
                '        variables={"buildings": []},\n'
                "        lat_lim=[-90, 90], lon_lim=[-180, 180],\n"
                "        path=OUT,\n"
                "    ).download()\n"
                "except ValueError as exc:\n"
                "    print(exc)"
            ),
        ]
    )


def _per_row_licensing() -> nbformat.NotebookNode:
    return _nb(
        [
            new_markdown_cell(
                "# Overture — per-row licensing (live)\n\n"
                "Overture aggregates many sources with different licenses. The "
                "backend surfaces a per-row `license_id` and warns when "
                "share-alike `ODbL-1.0` (OpenStreetMap-derived) rows are present "
                "— critical for commercial redistribution."
            ),
            new_code_cell(BBOX_SETUP),
            new_code_cell(
                "import warnings\n"
                "from earthlens.earthlens import EarthLens\n"
                "from earthlens.overture import LicenseWarning\n\n"
                "with warnings.catch_warnings(record=True) as caught:\n"
                "    warnings.simplefilter('always')\n"
                "    paths = EarthLens(\n"
                '        data_source="overture",\n'
                '        variables={"buildings": []},\n'
                "        lat_lim=LAT_LIM, lon_lim=LON_LIM, path=OUT,\n"
                "    ).download()\n\n"
                "for w in caught:\n"
                "    if issubclass(w.category, LicenseWarning):\n"
                "        print('LicenseWarning:', w.message)"
            ),
            new_code_cell(
                "import geopandas as gpd\n\n"
                "gdf = gpd.read_parquet(paths[0])\n"
                "gdf['license_id'].value_counts()"
            ),
            new_markdown_cell(
                "The derivation rule is exposed directly as `row_license` for "
                "ad-hoc analysis of a `sources` cell — ODbL wins whenever an "
                "OSM source is present, even if it is not listed first."
            ),
            new_code_cell(
                "from earthlens.overture import row_license\n\n"
                "print(row_license([\n"
                "    {'dataset': 'Overture', 'license': 'CDLA-Permissive-2.0'},\n"
                "    {'dataset': 'OpenStreetMap', 'license': 'ODbL-1.0'},\n"
                "]))\n"
                "print(row_license([{'dataset': 'Foursquare', 'license': 'Apache-2.0'}]))"
            ),
        ]
    )


def _transportation_network() -> nbformat.NotebookNode:
    return _nb(
        [
            new_markdown_cell(
                "# Overture — Transportation network (live)\n\n"
                "Fetch routable road segments for a block and inspect their road "
                "classes. Transportation is heavily OpenStreetMap-derived, so "
                "expect `ODbL-1.0` rows."
            ),
            new_code_cell(BBOX_SETUP),
            new_code_cell(
                "from earthlens.earthlens import EarthLens\n\n"
                "paths = EarthLens(\n"
                '    data_source="overture",\n'
                '    variables={"transportation": ["segment"]},\n'
                "    lat_lim=LAT_LIM, lon_lim=LON_LIM, path=OUT,\n"
                ").download()\n"
                "paths"
            ),
            new_code_cell(
                "import geopandas as gpd\n\n"
                "gdf = gpd.read_parquet(paths[0])\n"
                "print(len(gdf), 'segments;  geom', gdf.geometry.geom_type.unique())\n"
                "gdf[['id', 'subtype', 'class', 'license_id']].head()"
            ),
            new_markdown_cell("Road segments by `class`:"),
            new_code_cell("gdf['class'].value_counts()"),
            new_code_cell(
                "ax = gdf.plot(figsize=(5, 5), linewidth=0.8)\n"
                "ax.set_title('Overture road segments')\n"
                "ax.set_axis_off()"
            ),
        ]
    )


def _multi_theme() -> nbformat.NotebookNode:
    return _nb(
        [
            new_markdown_cell(
                "# Overture — multi-theme download (live)\n\n"
                "A single request can pull several themes / types at once; one "
                "file is written per feature type. Here we grab places and "
                "building footprints for the same block, pinning a release for "
                "reproducibility."
            ),
            new_code_cell(BBOX_SETUP),
            new_code_cell(
                "from earthlens.overture import Catalog\n"
                "release = Catalog().latest_release()  # pin for reproducibility\n"
                "release"
            ),
            new_code_cell(
                "from earthlens.earthlens import EarthLens\n\n"
                "paths = EarthLens(\n"
                '    data_source="overture",\n'
                '    variables={"places": [], "buildings": []},\n'
                "    lat_lim=LAT_LIM, lon_lim=LON_LIM, path=OUT,\n"
                "    release=release,\n"
                ").download()\n"
                "[p.name for p in paths]"
            ),
            new_code_cell(
                "import geopandas as gpd\n\n"
                "for p in paths:\n"
                "    gdf = gpd.read_parquet(p)\n"
                "    print(f'{p.name}: {len(gdf)} features, licenses={sorted(gdf.license_id.unique())}')"
            ),
        ]
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    notebooks = {
        "01_catalog_explorer.ipynb": _catalog_explorer(),
        "02_places_quickstart.ipynb": _places_quickstart(),
        "03_buildings_footprints.ipynb": _buildings_footprints(),
        "04_per_row_licensing.ipynb": _per_row_licensing(),
        "05_transportation_network.ipynb": _transportation_network(),
        "06_multi_theme_download.ipynb": _multi_theme(),
    }
    for name, nb in notebooks.items():
        nbformat.write(nb, OUT_DIR / name)
        print("wrote", OUT_DIR / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
