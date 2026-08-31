"""Solar & Wind Atlas backend — Global Solar Atlas + Global Wind Atlas layers.

`earthlens.solar_wind_atlas` serves bbox subsets of the Global Solar Atlas
(GHI / DNI / DIF / GTI / PVOUT / OPTA) and Global Wind Atlas (wind speed +
Weibull / capacity-factor / air-density) climatology layers as GeoTIFFs
(`OUTPUT_KIND="raster"`). Both atlases are keyless / CC-BY-4.0.

The backend uses **two transports**, one per atlas (pinned in the A1 gate,
the A1 gate captures): the Global Wind
Atlas layers are range-accessible COGs on figshare, read **windowed** over
`/vsicurl/` so only the AOI transfers; the Global Solar Atlas layers are
DEFLATE-compressed ZIP archives with no random access, so they are downloaded
once into a cache and read windowed from the local member. There is no auth
module — both atlases are public.
"""

from __future__ import annotations

from earthlens.solar_wind_atlas._helpers import (
    bbox_from_extent,
    download_cache_crop,
    download_zip,
    inner_tif,
    read_part_to_geotiff,
    vsicurl,
    window_crop,
)
from earthlens.solar_wind_atlas.backend import SolarWindAtlas
from earthlens.solar_wind_atlas.catalog import CATALOG_PATH, Catalog, Layer

__all__ = [
    "SolarWindAtlas",
    "Catalog",
    "Layer",
    "CATALOG_PATH",
    "bbox_from_extent",
    "vsicurl",
    "window_crop",
    "download_zip",
    "inner_tif",
    "download_cache_crop",
    "read_part_to_geotiff",
]
