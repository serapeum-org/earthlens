"""Maintain the bundled GHSL catalog + tile grid (`src/earthlens/ghsl/`).

A single `argparse` subcommand CLI over the open JRC GHSL HTTPS file tree
(`jeodpp.jrc.ec.europa.eu`, Apache autoindex — no auth). Run with no args to
see the subcommand list:

    pixi run -e dev python tools/ghsl/refresh_ghsl_catalog.py --help

Subcommands:

* `refresh-tiles` — re-download the JRC 54009 land tile schema shapefile and
  regenerate `tile_schema.geojson` (the bundled 18x36 Mollweide tile index).
  Requires `geopandas`.
* `validate` — for every curated product/release, build a representative
  artefact URL (a real land tile for tiled fine resolutions, the whole-globe
  file for coarse ones, the latest-version table zip for tabular products) and
  HEAD it, and confirm every categorical product ships a legend. `--strict`
  exits 1 on any drift (a curated artefact the live tree returns non-200, or a
  categorical product with no legend).
* `probe <product>` — crawl one product family directory and print the
  (epoch, resolution) combinations the live tree actually offers (handy when
  curating a new product or release).

Exits 0 on success, 1 on any HTTP / drift error. Not part of the installed
package.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import requests  # noqa: E402

from earthlens.ghsl._helpers import (  # noqa: E402
    BASE_URL,
    TILE_SCHEMA_PATH,
    ghsl_url,
    latest_version_dir,
    list_remote_dir,
    tiles_for_bbox,
)
from earthlens.ghsl.catalog import Catalog  # noqa: E402

#: JRC 54009 land tile schema shapefile (redirects to the Copernicus mirror).
_TILE_SCHEMA_ZIP = (
    "https://ghsl.jrc.ec.europa.eu/download/GHSL_data_54009_shapefile.zip"
)
#: A small Moroccan-coast AOI inside the verified R6_C18 land tile.
_LAND_BBOX = (-9.0, 30.5, -8.5, 31.0)


def _head(url: str, session: requests.Session) -> int:
    """Return the HTTP status of a HEAD request (0 on a transport error)."""
    try:
        return session.head(url, timeout=60, allow_redirects=True).status_code
    except requests.RequestException:
        return 0


def _sample_urls(catalog: Catalog, code: str, release: str) -> list[str | None]:
    """Build a representative artefact URL for every block of a product/release.

    One URL per availability block (each block's first epoch + first
    resolution), so multi-block products — e.g. GHS-BUILT-S's separate 2018
    10 m layer — are all HEAD-checked, not just the first block. Tabular
    products contribute a single version-zip URL.
    """
    product = catalog.get(code)
    blocks = product.releases[release]
    family = product.family_token()
    if product.kind == "tabular":
        block = blocks[0]
        family_url = f"{BASE_URL}/{family}_{block.region}_{release}"
        version = latest_version_dir(family_url)
        zips = [
            n for n in list_remote_dir(f"{family_url}/{version}") if n.endswith(".zip")
        ]
        return [f"{family_url}/{version}/{zips[0]}" if zips else None]
    urls: list[str | None] = []
    for block in blocks:
        url_kw = dict(version=block.version, region=block.region, nested=block.nested)
        epoch = block.epochs[0]
        resolution = block.resolutions[0]
        if resolution in block.tiled():
            tiles = tiles_for_bbox(_LAND_BBOX)
            tile = tiles[0] if tiles else "R6_C18"
            urls.append(
                ghsl_url(family, code, epoch, release, resolution, tile=tile, **url_kw)
            )
        else:
            urls.append(ghsl_url(family, code, epoch, release, resolution, **url_kw))
    return urls


def _validate(args: argparse.Namespace) -> int:
    """HEAD a representative artefact per curated product + check legends."""
    catalog = Catalog()
    session = requests.Session()
    drift = 0
    for code in catalog.available_products():
        product = catalog.get(code)
        if product.categorical and not product.legend:
            # A legend-less categorical product (e.g. GHS_BUILT_C_VEG, codes
            # uncurated) is intentional — it still reprojects with NN. Note it,
            # don't fail: it carries no `colors`, just the categorical flag.
            note = "" if product.colors else " (uncurated codes — NN only)"
            print(f"note   {code}: categorical without legend{note}")
            if product.colors:
                print(f"DRIFT  {code}: has colors but no legend")
                drift += 1
        for release in product.releases:
            for url in _sample_urls(catalog, code, release):
                if url is None:
                    print(f"DRIFT  {code} ({release}): no artefact URL resolvable")
                    drift += 1
                    continue
                status = _head(url, session)
                flag = "ok   " if status == 200 else "DRIFT"
                if status != 200:
                    drift += 1
                print(f"{flag}  {code} ({release})  {status}  {url}")
    session.close()
    if drift:
        print(f"\n{drift} drift(s) found.")
        return 1 if args.strict else 0
    print("\nall curated artefacts resolve 200; legends present.")
    return 0


def _probe(args: argparse.Namespace) -> int:
    """Print the (epoch, resolution) combos the live family dir offers."""
    family_dir = args.family
    url = f"{BASE_URL}/{family_dir}"
    combos: set[tuple[str, str]] = set()
    for name in list_remote_dir(url):
        name = name.rstrip("/")
        if "_E" not in name:
            continue
        match = re.search(r"_E(\d{4})_GLOBE_\w+?_(\d+|\d+ss)$", name)
        if match:
            crs_res = name.rsplit("_", 1)[-1]
            combos.add((match.group(1), crs_res))
    if not combos:
        print(f"no per-epoch directories under {url}")
        return 1
    for epoch, res in sorted(combos):
        print(f"E{epoch}  {res}")
    return 0


def _refresh_tiles(args: argparse.Namespace) -> int:
    """Re-download the JRC tile schema and regenerate tile_schema.geojson."""
    import io
    import zipfile

    try:
        import geopandas as gpd
    except ImportError:
        print("refresh-tiles needs geopandas; install the dev environment.")
        return 1

    resp = requests.get(_TILE_SCHEMA_ZIP, timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        archive.extractall(Path(args.workdir))
    shp = next(Path(args.workdir).glob("*tile_schema_land*.shp"))
    gdf = gpd.read_file(shp)[["tile_id", "left", "top", "right", "bottom", "geometry"]]
    for col in ("left", "top", "right", "bottom"):
        gdf[col] = gdf[col].astype(int)
    gdf.to_file(TILE_SCHEMA_PATH, driver="GeoJSON")
    print(f"wrote {TILE_SCHEMA_PATH} with {len(gdf)} tiles")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the subcommand parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser(
        "validate", help="HEAD curated artefacts + check legends"
    )
    p_validate.add_argument("--strict", action="store_true", help="exit 1 on any drift")
    p_validate.set_defaults(func=_validate)

    p_probe = sub.add_parser("probe", help="list the live (epoch, resolution) combos")
    p_probe.add_argument("family", help="family dir, e.g. GHS_POP_GLOBE_R2023A")
    p_probe.set_defaults(func=_probe)

    p_tiles = sub.add_parser("refresh-tiles", help="regenerate tile_schema.geojson")
    p_tiles.add_argument("--workdir", default=".", help="scratch dir for the shapefile")
    p_tiles.set_defaults(func=_refresh_tiles)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
