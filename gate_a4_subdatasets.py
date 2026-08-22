"""Gate A4 - how does EEDAI expose an asset whose bands differ in resolution?

The driver documents that it "will expose subdatasets" when an asset's bands
differ in georeferencing, resolution, CRS or dimensions. pyramids-eo has no
subdataset handling at all, so the question is what actually happens today for
the obvious case: Sentinel-2, which mixes 10 m, 20 m and 60 m bands.

S2 is an ImageCollection, so there is no single asset id to open - a concrete
scene has to be discovered first, which is what the EEDA vector driver is for and
what pyramids-eo already does in `_discover_scenes`. This walks that same path,
then opens the scene and records: the subdataset list, what a plain open returns,
what the per-band resolutions are, and whether asking for bands of mixed
resolution together succeeds or fails.
"""

from __future__ import annotations

import json
import os

import pyramids as _pyramids_bootstrap  # noqa: F401  (activates the bundled osgeo)
from osgeo import gdal

gdal.UseExceptions()

KEY = os.environ["GEE_SERVICE_KEY"]
COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
# 10 m, 20 m and 60 m bands of the same scene.
MIXED = ["B4", "B11", "B1"]
AOI = (7.60, 45.93, 7.72, 46.02)
WINDOW = ("2024-07-01", "2024-07-31")


def _activate() -> None:
    """Point GDAL's EEDA auth at the service-account key."""
    with open(KEY, encoding="utf-8") as fh:
        info = json.load(fh)
    gdal.SetConfigOption("EEDA_PRIVATE_KEY", info["private_key"])
    gdal.SetConfigOption("EEDA_CLIENT_EMAIL", info["client_email"])


def _first_scene() -> str | None:
    """Find one scene connection string via the EEDA vector driver."""
    catalog = gdal.OpenEx(
        "EEDA:", gdal.OF_VECTOR | gdal.OF_VERBOSE_ERROR,
        open_options=[f"COLLECTION={COLLECTION}"],
    )
    if catalog is None:
        print(f"  EEDA could not open {COLLECTION}: {gdal.GetLastErrorMsg()}")
        return None
    layer = catalog.GetLayer(0)
    layer.SetAttributeFilter(
        f"startTime >= '{WINDOW[0]}T00:00:00' AND startTime < '{WINDOW[1]}T00:00:00'"
    )
    layer.SetSpatialFilterRect(*AOI)
    fields = [layer.GetLayerDefn().GetFieldDefn(i).GetName()
              for i in range(layer.GetLayerDefn().GetFieldCount())]
    print(f"  EEDA layer fields ({len(fields)}): {fields}")
    for feature in layer:
        connection = feature.GetFieldAsString("gdal_dataset")
        if connection:
            print(f"  scene: {feature.GetFieldAsString('id')}")
            print(f"    startTime      = {feature.GetFieldAsString('startTime')}")
            print(f"    band_count     = {feature.GetFieldAsString('band_count')}")
            print(f"    band_max_width = {feature.GetFieldAsString('band_max_width')}")
            print(f"    min_pixel_size = {feature.GetFieldAsString('band_min_pixel_size')}")
            props = feature.GetFieldAsString("other_properties")
            if props:
                try:
                    parsed = json.loads(props)
                    keys = sorted(parsed)[:8]
                    print(f"    other_properties: {len(parsed)} keys, e.g. {keys}")
                    for key in parsed:
                        if "CLOUD" in key.upper():
                            print(f"      {key} = {parsed[key]}")
                except json.JSONDecodeError:
                    print(f"    other_properties (unparsed): {props[:90]}")
            return connection
    print("  no scene found in that window/AOI")
    return None


def _report(label: str, connection: str, bands: list[str] | None) -> None:
    """Open a connection and describe its bands and subdatasets."""
    opts = ["BLOCK_SIZE=512"]
    if bands:
        opts.append("BANDS=" + ",".join(bands))
    print(f"\n  --- {label} ---")
    try:
        ds = gdal.OpenEx(connection, gdal.OF_RASTER | gdal.OF_VERBOSE_ERROR, open_options=opts)
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not recover
        print(f"    RAISED {type(exc).__name__}: {str(exc)[:110]}")
        return
    if ds is None:
        print(f"    returned None: {gdal.GetLastErrorMsg()[:110]}")
        return
    subs = ds.GetSubDatasets()
    print(f"    size={ds.RasterXSize}x{ds.RasterYSize} bands={ds.RasterCount} "
          f"subdatasets={len(subs)}")
    gt = ds.GetGeoTransform()
    print(f"    pixel size: {abs(gt[1]):g} x {abs(gt[5]):g} (CRS units)")
    for name, desc in subs[:8]:
        print(f"      SUB: {name}   |   {desc}")
    for i in range(1, min(ds.RasterCount, 8) + 1):
        b = ds.GetRasterBand(i)
        print(f"      band {i}: {b.GetDescription() or '(unnamed)'} "
              f"{gdal.GetDataTypeName(b.DataType)} block={b.GetBlockSize()} "
              f"overviews={b.GetOverviewCount()}")


def main() -> None:
    """Discover an S2 scene and record how EEDAI presents its mixed bands."""
    _activate()
    print(f"discovering a scene from {COLLECTION} over {AOI} in {WINDOW[0]}..{WINDOW[1]}")
    connection = _first_scene()
    if not connection:
        return
    print(f"\n  connection: {connection[:110]}")
    _report("plain open (all bands)", connection, None)
    _report(f"mixed resolutions {MIXED}", connection, MIXED)
    _report("single 10 m band [B4]", connection, ["B4"])
    _report("single 60 m band [B1]", connection, ["B1"])


if __name__ == "__main__":
    main()
