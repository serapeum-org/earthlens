"""Per-dataset S3 key resolvers for the AWS Open-Data backend.

Turns a uniform request — a `Dataset`, its resolved `Variable` rows, an
AOI bbox, and a date index — into the concrete list of S3 objects to
download, expressed as :class:`~earthlens.base.RemoteProduct` rows
(`href` carries the S3 key). Two layout families, dispatched by the
dataset's `params["builder"]` token:

* **deterministic_tiles** (`copernicus_dem`, `esa_worldcover`) — compute
  the lat/lon tile names covering the bbox with pure arithmetic; no
  listing call.
* **prefix_listing** (`era5`, `sentinel2`, `goes`) — compute an S3
  prefix from the variable + date (+ MGRS tile for Sentinel-2), list it,
  and match the variable token in the returned keys.

The Sentinel-2 resolver derives MGRS 100 km tiles from the bbox using
`utm` (which supplies the UTM zone, latitude band, and easting/northing)
plus a compact 100 km-square-letter computation; the result was verified
against the live `sentinel-cogs` bucket for points across both
hemispheres.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Callable, Iterable

from loguru import logger

from earthlens.base import RemoteProduct

if TYPE_CHECKING:  # pragma: no cover
    from earthlens.s3.catalog import Dataset, Variable

__all__ = ["plan_products"]

# MGRS 100 km square letters. Column letters cycle in three 8-letter sets
# by `(zone - 1) % 3`; row letters are a 20-letter sequence offset by 5 for
# even-numbered zones. Verified against sentinel-cogs (Paris->31UDQ,
# Cairo->36RUU, NYC->18TWL, Nairobi->37MBU).
_MGRS_COLUMN_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_MGRS_ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"


def plan_products(
    dataset: Dataset,
    variables: list[Variable],
    bbox: tuple[float, float, float, float],
    dates: Iterable[Any],
    client: Any = None,
) -> list[RemoteProduct]:
    """Resolve a request to the list of S3 products to download.

    Args:
        dataset: The resolved :class:`~earthlens.s3.catalog.Dataset`.
        variables: The resolved :class:`~earthlens.s3.catalog.Variable`
            rows the caller asked for.
        bbox: AOI as `(west, south, east, north)` in degrees (EPSG:4326).
        dates: The request's date index (each item exposes `.year`,
            `.month`, `.day`, `.timetuple()`); ignored for static
            datasets.
        client: A `boto3` S3 client, required by the prefix-listing
            builders (`era5`, `sentinel2`, `goes`) and unused by the
            deterministic-tile builders.

    Returns:
        One :class:`~earthlens.base.RemoteProduct` per object to fetch;
        `href` is the S3 key and `metadata` carries `bucket`, the native
        variable token, and (where applicable) the date/tile.

    Raises:
        ValueError: If the dataset names an unknown builder, or a
            passthrough spec lacks a usable `key_template`.

    Examples:
        - Plan the Copernicus DEM tile keys for a bbox (deterministic, no client):
            ```python
            >>> from earthlens.s3 import Catalog
            >>> from earthlens.s3.layouts import plan_products
            >>> dem = Catalog().get_dataset("copernicus-dem")
            >>> products = plan_products(dem, dem.resolve_variables(None), (6.4, 0.4, 6.6, 0.6), [])
            >>> products[0].href
            'Copernicus_DSM_COG_10_N00_00_E006_00_DEM/Copernicus_DSM_COG_10_N00_00_E006_00_DEM.tif'

            ```
        - A bbox spanning two 1-degree tiles plans one product per tile:
            ```python
            >>> from earthlens.s3 import Catalog
            >>> from earthlens.s3.layouts import plan_products
            >>> dem = Catalog().get_dataset("copernicus-dem")
            >>> products = plan_products(dem, dem.resolve_variables(None), (5.5, 0.5, 7.5, 0.5), [])
            >>> [p.metadata["tile"] for p in products]
            ['N00E005', 'N00E006', 'N00E007']

            ```
    """
    # Registered datasets carry an explicit builder; a passthrough spec has
    # none and is always resolved through the generic key-template path.
    builder = dataset.params.get("builder") or "generic_template"
    fn = _BUILDERS.get(builder)
    if fn is None:
        raise ValueError(
            f"no S3 key resolver for builder {builder!r}; "
            f"known builders: {sorted(_BUILDERS)}."
        )
    return fn(dataset, variables, bbox, list(dates), client)


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------


def _tile_origins(low: float, high: float, step: int) -> list[int]:
    """Integer tile-origin coordinates of a `step`-degree grid covering `[low, high]`."""
    start = math.floor(low / step) * step
    stop = math.floor(high / step) * step
    return list(range(int(start), int(stop) + step, step))


def _fmt_lat(value: int) -> str:
    """Format an integer latitude tile-origin as `N00` / `S05` (2-digit)."""
    hemi = "N" if value >= 0 else "S"
    return f"{hemi}{abs(value):02d}"


def _fmt_lon(value: int) -> str:
    """Format an integer longitude tile-origin as `E006` / `W074` (3-digit)."""
    hemi = "E" if value >= 0 else "W"
    return f"{hemi}{abs(value):03d}"


def _year_months(dates: list[Any]) -> list[tuple[int, int]]:
    """Distinct `(year, month)` pairs across the date index, in order."""
    seen: dict[tuple[int, int], None] = {}
    for date in dates:
        seen.setdefault((date.year, date.month), None)
    return list(seen)


def _days(dates: list[Any]) -> list[Any]:
    """Distinct calendar days across the date index, in order (by `(y, m, d)`)."""
    seen: dict[tuple[int, int, int], Any] = {}
    for date in dates:
        seen.setdefault((date.year, date.month, date.day), date)
    return list(seen.values())


def _mgrs_square(zone: int, easting: float, northing: float) -> str:
    """Return the MGRS 100 km square id (two letters) for a UTM coordinate."""
    column = _MGRS_COLUMN_SETS[(zone - 1) % 3][int(easting // 100_000) - 1]
    shift = 0 if zone % 2 == 1 else 5
    row = _MGRS_ROW_LETTERS[(int(northing // 100_000) + shift) % 20]
    return f"{column}{row}"


def _mgrs_tiles(bbox: tuple[float, float, float, float]) -> list[tuple[str, str, str]]:
    """Distinct `(zone, lat_band, square)` MGRS tiles covering the bbox.

    Samples the bbox on a ~0.2-degree grid (corners always included) and
    collects the unique tiles each sample point falls in — enough to
    cover an AOI smaller than several MGRS tiles without an MGRS polygon
    library.
    """
    import utm

    west, south, east, north = bbox
    lon_steps = _sample_axis(west, east)
    lat_steps = _sample_axis(south, north)
    tiles: dict[tuple[str, str, str], None] = {}
    for lat in lat_steps:
        for lon in lon_steps:
            easting, northing, zone, band = utm.from_latlon(lat, lon)
            tiles.setdefault(
                (str(zone), band, _mgrs_square(zone, easting, northing)), None
            )
    return list(tiles)


def _sample_axis(low: float, high: float, step: float = 0.2) -> list[float]:
    """Sample `[low, high]` at `step` spacing, always including both ends."""
    if high < low:
        low, high = high, low
    count = int(math.floor((high - low) / step))
    points = [low + i * step for i in range(count + 1)]
    if not points or points[-1] < high:
        points.append(high)
    return points


def _payer(request_payer: bool) -> dict[str, str]:
    """`RequestPayer` kwargs for a paginator call, empty unless requester-pays."""
    return {"RequestPayer": "requester"} if request_payer else {}


def _list_keys(
    client: Any, bucket: str, prefix: str, request_payer: bool = False
) -> list[tuple[str, int]]:
    """List `(object key, size in bytes)` under `prefix` (paginated).

    Zero-size objects (directory placeholders) are skipped. The size is
    carried alongside the key so callers can record it on the planned
    product for the cross-region egress warning, at no extra request.
    """
    keys: list[tuple[str, int]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix=prefix, **_payer(request_payer)
    ):
        keys.extend(
            (obj["Key"], obj["Size"])
            for obj in page.get("Contents", [])
            if obj.get("Size", 0)
        )
    return keys


def _list_prefixes(
    client: Any, bucket: str, prefix: str, request_payer: bool = False
) -> list[str]:
    """List the common (directory) prefixes immediately under `prefix`."""
    prefixes: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix=prefix, Delimiter="/", **_payer(request_payer)
    ):
        prefixes.extend(p["Prefix"] for p in page.get("CommonPrefixes", []))
    return prefixes


# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


def _era5_products(dataset, variables, bbox, dates, client) -> list[RemoteProduct]:
    """ERA5 (nsf-ncar-era5): one monthly NetCDF per (variable, month)."""
    default_stream = dataset.params["default_stream"]
    out: list[RemoteProduct] = []
    for var in variables:
        stream = var.stream or default_stream
        for year, month in _year_months(dates):
            prefix = f"{stream}/{year}{month:02d}/"
            token = f".{var.native}."
            for key, size in _list_keys(
                client, dataset.bucket, prefix, dataset.requester_pays
            ):
                if token in key.rsplit("/", 1)[-1] and key.endswith(".nc"):
                    out.append(
                        RemoteProduct(
                            id=f"{var.native}_{year}{month:02d}",
                            href=key,
                            metadata={
                                "bucket": dataset.bucket,
                                "variable": var.native,
                                "year": year,
                                "month": month,
                                "size_bytes": size,
                            },
                        )
                    )
    return out


def _scene_index(scene_prefix: str) -> int:
    """Numeric trailing index of a Sentinel-2 scene prefix (`.../6/10/` -> 10)."""
    tail = scene_prefix.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else -1


def _sentinel2_products(dataset, variables, bbox, dates, client) -> list[RemoteProduct]:
    """Sentinel-2 L2A (sentinel-cogs): one COG per (band, scene) over the MGRS tiles.

    bbox->scene discovery has no cloud filter; a wide AOI / long window can
    match many scenes. `params["max_scenes"]` (the `max_scenes=` request arg)
    caps the scenes kept per (tile, month) — the highest-indexed ones — with a
    warning when truncating. The scene index is not a strict recency signal, so
    use the STAC backend for true latest / cloud-cover filtering.
    """
    collection = dataset.params["collection_prefix"]
    max_scenes = dataset.params.get("max_scenes")
    out: list[RemoteProduct] = []
    for zone, band, square in _mgrs_tiles(bbox):
        for year, month in _year_months(dates):
            month_prefix = f"{collection}/{zone}/{band}/{square}/{year}/{month}/"
            scenes = _list_prefixes(
                client, dataset.bucket, month_prefix, dataset.requester_pays
            )
            if max_scenes is not None and len(scenes) > max_scenes:
                logger.warning(
                    f"sentinel-2: {len(scenes)} scenes under {month_prefix}; "
                    f"keeping the {max_scenes} highest-indexed (max_scenes={max_scenes})."
                )
                scenes = sorted(scenes, key=_scene_index)[-max_scenes:]
            for scene_prefix in scenes:
                for var in variables:
                    out.append(
                        RemoteProduct(
                            id=f"{zone}{band}{square}_{year}{month:02d}_"
                            f"{scene_prefix.rstrip('/').rsplit('/', 1)[-1]}_{var.native}",
                            href=f"{scene_prefix}{var.native}.tif",
                            metadata={
                                "bucket": dataset.bucket,
                                "variable": var.native,
                                "tile": f"{zone}{band}{square}",
                                "scene": scene_prefix,
                            },
                        )
                    )
    return out


def _goes_products(dataset, variables, bbox, dates, client) -> list[RemoteProduct]:
    """GOES ABI (noaa-goes16/18): one NetCDF per (channel, day) at the first hour/frame."""
    product = dataset.params["default_product"]
    out: list[RemoteProduct] = []
    for date in _days(dates):
        doy = date.timetuple().tm_yday
        day_prefix = f"{product}/{date.year}/{doy:03d}/"
        hour_prefixes = _list_prefixes(
            client, dataset.bucket, day_prefix, dataset.requester_pays
        )
        if not hour_prefixes:
            continue
        # One frame per day: the first frame of the first hour. The day has many
        # frames (every ~10-15 min); the rest are intentionally not planned.
        sizes = dict(
            _list_keys(client, dataset.bucket, hour_prefixes[0], dataset.requester_pays)
        )
        for var in variables:
            token = f"{var.native}_G"
            match = next((k for k in sizes if token in k.rsplit("/", 1)[-1]), None)
            if match is not None:
                logger.info(
                    f"goes: selected frame {match.rsplit('/', 1)[-1]} for "
                    f"{date.year}-{doy:03d} {var.native} (one frame/day)."
                )
                out.append(
                    RemoteProduct(
                        id=f"{var.native}_{date.year}{doy:03d}",
                        href=match,
                        metadata={
                            "bucket": dataset.bucket,
                            "variable": var.native,
                            "year": date.year,
                            "doy": doy,
                            "size_bytes": sizes[match],
                        },
                    )
                )
    return out


def _copernicus_dem_products(
    dataset, variables, bbox, dates, client
) -> list[RemoteProduct]:
    """Copernicus DEM (copernicus-dem-30m): one COG per 1-degree tile covering the bbox."""
    west, south, east, north = bbox
    step = int(dataset.params["tile_degrees"])
    token = dataset.params["resolution_token"]
    out: list[RemoteProduct] = []
    for lat in _tile_origins(south, north, step):
        for lon in _tile_origins(west, east, step):
            name = (
                f"Copernicus_DSM_COG_{token}_{_fmt_lat(lat)}_00_"
                f"{_fmt_lon(lon)}_00_DEM"
            )
            out.append(
                RemoteProduct(
                    id=name,
                    href=f"{name}/{name}.tif",
                    metadata={
                        "bucket": dataset.bucket,
                        "variable": variables[0].native if variables else "DEM",
                        "tile": f"{_fmt_lat(lat)}{_fmt_lon(lon)}",
                    },
                )
            )
    return out


def _esa_worldcover_products(
    dataset, variables, bbox, dates, client
) -> list[RemoteProduct]:
    """ESA WorldCover (esa-worldcover): one COG per 3-degree tile covering the bbox."""
    west, south, east, north = bbox
    step = int(dataset.params["tile_degrees"])
    epoch = dataset.params["default_epoch"]
    version = dataset.params["epochs"][epoch]
    out: list[RemoteProduct] = []
    for lat in _tile_origins(south, north, step):
        for lon in _tile_origins(west, east, step):
            tile = f"{_fmt_lat(lat)}{_fmt_lon(lon)}"
            key = (
                f"{version}/{epoch}/map/"
                f"ESA_WorldCover_10m_{epoch}_{version}_{tile}_Map.tif"
            )
            out.append(
                RemoteProduct(
                    id=f"worldcover_{epoch}_{tile}",
                    href=key,
                    metadata={
                        "bucket": dataset.bucket,
                        "variable": variables[0].native if variables else "Map",
                        "tile": tile,
                        "epoch": epoch,
                    },
                )
            )
    return out


def _generic_template_products(
    dataset, variables, bbox, dates, client
) -> list[RemoteProduct]:
    """Passthrough: format `params['key_template']` per (variable, date).

    The template may reference `{variable}`, `{year}`, `{month}`,
    `{day}`, and `{doy}`. With no date placeholders it is emitted once
    per variable.
    """
    template = dataset.params.get("key_template")
    if not template:
        raise ValueError(
            "passthrough dataset needs params['key_template'] to resolve keys."
        )
    # Scene/tile identifiers (e.g. a NAIP quad path) supplied via the
    # request are exposed to the template alongside the date fields.
    base_fields: dict[str, Any] = {}
    for name in ("scene", "tile"):
        if dataset.params.get(name) is not None:
            base_fields[name] = dataset.params[name]
    stamps = _days(dates) or [None]
    out: list[RemoteProduct] = []
    for var in variables or [None]:
        native = var.native if var is not None else ""
        for date in stamps:
            fields: dict[str, Any] = {"variable": native, **base_fields}
            if date is not None:
                fields.update(
                    year=date.year,
                    month=f"{date.month:02d}",
                    day=f"{date.day:02d}",
                    doy=f"{date.timetuple().tm_yday:03d}",
                )
            key = template.format(**fields)
            out.append(
                RemoteProduct(
                    id=key,
                    href=key,
                    metadata={"bucket": dataset.bucket, "variable": native},
                )
            )
    return out


_LANDSAT_SENSORS: dict[str, str] = {
    "LC08": "oli-tirs",
    "LC09": "oli-tirs",
    "LO08": "oli",
    "LO09": "oli",
    "LT08": "tirs",
    "LT09": "tirs",
    "LE07": "etm",
    "LT05": "tm",
    "LT04": "tm",
}


def _landsat_sensor(scene: str) -> str:
    """Map a Landsat scene id's sensor prefix to its bucket folder token."""
    sensor = _LANDSAT_SENSORS.get(scene[:4])
    if sensor is None:
        raise ValueError(
            f"unrecognised Landsat sensor prefix {scene[:4]!r} in scene {scene!r}; "
            f"known: {sorted(_LANDSAT_SENSORS)}."
        )
    return sensor


def _landsat_products(dataset, variables, bbox, dates, client) -> list[RemoteProduct]:
    """USGS Landsat Collection-2 (requester-pays): one COG per band of a scene.

    Addressed by an explicit Collection-2 scene id (the `scene=` request
    argument), e.g. `LC08_L2SP_039037_20210901_20210910_02_T1` — its
    sensor / path / row / year are parsed from the id. Bbox-driven scene
    discovery (WRS-2 path/row from a lat/lon box) is out of scope; use the
    STAC backend for that.
    """
    scene = dataset.params.get("scene")
    if not scene:
        raise ValueError(
            "usgs-landsat needs scene= (a Collection-2 scene id, e.g. "
            "'LC08_L2SP_039037_20210901_20210910_02_T1')."
        )
    sensor = _landsat_sensor(scene)
    path, row, year = scene[10:13], scene[13:16], scene[17:21]
    out: list[RemoteProduct] = []
    for var in variables:
        key = (
            f"collection02/level-2/standard/{sensor}/{year}/{path}/{row}/"
            f"{scene}/{scene}_{var.native}.TIF"
        )
        out.append(
            RemoteProduct(
                id=f"{scene}_{var.native}",
                href=key,
                metadata={
                    "bucket": dataset.bucket,
                    "variable": var.native,
                    "scene": scene,
                },
            )
        )
    return out


_BUILDERS: dict[str, Callable[..., list[RemoteProduct]]] = {
    "era5": _era5_products,
    "sentinel2": _sentinel2_products,
    "goes": _goes_products,
    "copernicus_dem": _copernicus_dem_products,
    "esa_worldcover": _esa_worldcover_products,
    "landsat": _landsat_products,
    "generic_template": _generic_template_products,
}
