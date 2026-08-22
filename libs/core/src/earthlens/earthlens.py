"""Front-end facade that routes downloads to a concrete data-source backend.

The :class:`EarthLens` class is the user-facing entry point of the
package. It keeps the choice of backend (CHIRPS, ERA5 on AWS S3, ECMWF
on the Copernicus Climate Data Store, Google Earth Engine) behind a
single string key so callers do not have to import each backend module
directly.

Each backend's runtime SDK is an optional dependency
(`pip install earthlens[ecmwf]`, `[s3]`, `[gee]`); the registry below
imports the backend module on first dispatch and rewrites a missing
SDK into a friendly `ImportError` naming the extra to install.
"""

from __future__ import annotations

import atexit
import difflib
import importlib
import inspect
import shutil
import tempfile
import warnings
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from earthlens._backends import discover_backends
from earthlens.base import split_time
from earthlens.base.spatial import resolve_aoi
from earthlens.config import output_dir

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.base import AbstractCatalog, AbstractDataSource, RemoteProduct


#: Default longitude bounds used when `lon_lim` is not supplied
#: (whole-Earth coverage).
DEFAULT_LONGITUDE_LIMIT = [-180.0, 180.0]

#: Default latitude bounds used when `lat_lim` is not supplied
#: (whole-Earth coverage).
DEFAULT_LATITUDE_LIMIT = [-90.0, 90.0]

#: Raster file suffixes that :func:`_load_path` reads into a pyramids object.
#: A `.nc` is read as a `NetCDF`; every other suffix as a `Dataset`.
_RASTER_SUFFIXES = frozenset(
    {".tif", ".tiff", ".cog", ".nc", ".nc4", ".bil", ".vrt", ".jp2", ".img"}
)


def _load_path(path: Path) -> Any:
    """Read a written raster `path` into a native pyramids object.

    Args:
        path: A path to a raster written by a backend's `download`.

    Returns:
        A `pyramids.NetCDF` for a `.nc` / `.nc4` path, else a
        `pyramids.Dataset`.
    """
    if path.suffix.lower() in {".nc", ".nc4"}:
        from pyramids.netcdf import NetCDF

        return NetCDF.read_file(str(path))
    from pyramids.dataset import Dataset

    return Dataset.read_file(str(path))


def _load_result(result: Any) -> Any:
    """Turn a `download` result into in-memory objects for :meth:`EarthLens.load`.

    A list of paths has each raster entry read into a pyramids object (via
    :func:`_load_path`); non-raster entries (e.g. a `.csv` table) stay as their
    `Path`. A non-list result (a `FeatureCollection` / `GeoDataFrame` /
    `DataFrame` a backend already returns in memory) is passed through.

    Args:
        result: The value returned by `EarthLens.download`.

    Returns:
        The in-memory representation described above.
    """
    if not isinstance(result, list):
        return result
    return [
        (
            _load_path(item)
            if isinstance(item, Path) and item.suffix.lower() in _RASTER_SUFFIXES
            else item
        )
        for item in result
    ]


#: `load()` temp directories deferred to the process-exit sweep — only the rare
#: case where the result hands back a raw `Path` the caller still reads (a mixed
#: backend's `.csv`). Raster results are detached in-memory and removed at once.
_LOAD_TEMP_DIRS: set[str] = set()


@atexit.register
def _sweep_load_tempdirs() -> None:
    """Remove any deferred `load()` temp dir still pending when the process exits."""
    for temp_dir in list(_LOAD_TEMP_DIRS):
        shutil.rmtree(temp_dir, ignore_errors=True)
        _LOAD_TEMP_DIRS.discard(temp_dir)


def _detach_and_cleanup(temp_dir: Path, result: Any) -> Any:
    """Detach a `load()` result from its temp dir, then remove the directory.

    `load()` writes a backend's incidental files to a throwaway temp directory
    when `path` was omitted. The rasters it returns (`pyramids.Dataset` /
    `NetCDF`) otherwise read from those files lazily and hold an open handle —
    on Windows that even locks the files. Each raster is therefore replaced with
    an in-memory `copy()` and its file handle closed, so the directory can be
    removed immediately and deterministically (no reliance on garbage-collection
    timing). A raw `Path` the caller still owns (a mixed backend's `.csv`) cannot
    be detached, so the directory is deferred to the process-exit sweep instead.

    Args:
        temp_dir: The directory created by
            :meth:`EarthLens._redirect_output_to_tempdir`.
        result: The value produced by :func:`_load_result`.

    Returns:
        The result with each file-backed raster swapped for an in-memory copy.
    """
    # A passthrough in-memory object (GeoDataFrame / DataFrame) holds no handle.
    if not isinstance(result, list):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return result

    detached: list[Any] = []
    caller_owns_path = False
    for item in result:
        if isinstance(item, (str, Path)):
            caller_owns_path = True
            detached.append(item)
        elif hasattr(item, "copy") and hasattr(item, "close"):
            # A pyramids raster: deep-copy into memory, then release the file.
            try:
                in_memory = item.copy()
                item.close()
                detached.append(in_memory)
            except Exception:  # noqa: BLE001 — keep the original; defer cleanup
                caller_owns_path = True
                detached.append(item)
        else:
            detached.append(item)  # already in memory (e.g. a GeoDataFrame)

    if caller_owns_path:
        _LOAD_TEMP_DIRS.add(str(temp_dir))  # defer to the process-exit sweep
    else:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return detached


def _import_backend_module(
    module_name: str,
    label: str,
    extras: str,
    *,
    subject: str = "Backend",
) -> Any:
    """Import a backend module, rewriting a missing SDK into a friendly error.

    Both entry points that import a backend on demand — indexing the lazy
    registry and loading a backend's catalog — need the same rewrite: the
    raw `ImportError` names the missing SDK, not the earthlens extra that
    installs it. Keeping one helper means the two paths cannot drift into
    telling the user different things.

    Args:
        module_name: The dotted module to import (e.g. `earthlens.gee`).
        label: The data-source key to name in the message (e.g. `"gee"`).
        extras: The pip extra that provides the SDK, or `""` when the
            backend needs none (then no install hint is added).
        subject: What is unavailable, used to open the message. The default
            reads `Backend 'gee' is unavailable`; the catalog path passes
            `"Backend catalog for"`.

    Returns:
        The imported module.

    Raises:
        ImportError: When the module cannot be imported. The message names
            the key and, when `extras` is set, the `pip install` line that
            fixes it; the original error is chained as `__cause__`.

    Examples:
        - An installed backend imports normally:
            ```python
            >>> from earthlens.earthlens import _import_backend_module
            >>> module = _import_backend_module("earthlens.chc", "chc", "")
            >>> module.__name__
            'earthlens.chc'

            ```
        - A failure *inside* earthlens is re-raised untouched, so a bug in our
          own code is never reported as somebody's missing dependency:
            ```python
            >>> from earthlens.earthlens import _import_backend_module
            >>> _import_backend_module("earthlens.no_such_backend", "gee", "gee")
            Traceback (most recent call last):
                ...
            ModuleNotFoundError: No module named 'earthlens.no_such_backend'

            ```

        When the missing module is a third-party SDK instead — `ee` for gee,
        `cdsapi` for ecmwf — the raised `ImportError` reads
        `Backend 'gee' is unavailable — its runtime dependency is not
        installed. Install with `pip install earthlens[gee]`.` That path needs
        the SDK to be genuinely absent, so it is covered by the unit tests
        rather than a doctest.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        if not _is_missing_dependency(exc, module_name):
            # A bug inside the backend's own code — a typo'd relative import,
            # a broken sibling module. Telling the user to install an extra
            # they already have sends them after the wrong thing entirely, so
            # the real error is re-raised untouched.
            raise
        hint = f" Install with `pip install earthlens[{extras}]`." if extras else ""
        raise ImportError(
            f"{subject} {label!r} is unavailable — its runtime "
            f"dependency is not installed.{hint}"
        ) from exc


def _is_missing_dependency(exc: ImportError, module_name: str) -> bool:
    """Report whether `exc` is a genuinely absent third-party dependency.

    Distinguishes the two very different failures that both surface as
    `ImportError` when a backend module is imported:

    * the optional SDK is not installed — the user needs the pip extra;
    * something inside the backend package is broken — the user needs a bug
      report, and an "install the extra" message actively misleads.

    Two conditions must both hold, and each rules out a real misreport:

    * the exception is a `ModuleNotFoundError` — a module was genuinely
      absent. A plain `ImportError` means the module imported fine but a
      *name* inside it did not (`from ee import Missing`), which is a
      version skew or a bug, never something `pip install` fixes. Note
      `ImportError.name` is set to the *present* module in that case, so
      the name alone would misread it as absent;
    * `ImportError.name` is outside our own namespace. `module_name` is
      always a backend under `earthlens.*`, so an absent module sharing that
      root is our code — a typo'd relative import, a missing sibling — and
      the user needs the real error, not an install hint.

    A `None` name means the exception was raised by hand rather than by the
    import system, so it is treated as internal: the conservative choice,
    since it surfaces the original message rather than replacing it.

    An absent *transitive* dependency of the SDK (`No module named 'grpc'`
    while importing `ee`) satisfies both conditions and keeps the hint, which
    is correct — reinstalling the extra is what repairs it.

    Args:
        exc: The `ImportError` raised while importing the backend.
        module_name: The backend module that was being imported.

    Returns:
        bool: `True` when the failure is a missing external dependency.

    Examples:
        - A missing SDK is a dependency problem:
            ```python
            >>> from earthlens.earthlens import _is_missing_dependency
            >>> exc = ModuleNotFoundError("No module named 'ee'", name="ee")
            >>> _is_missing_dependency(exc, "earthlens.gee")
            True

            ```
        - A broken import inside the backend is not, so the real error shows:
            ```python
            >>> from earthlens.earthlens import _is_missing_dependency
            >>> exc = ModuleNotFoundError(
            ...     "No module named 'earthlens.gee._helpers'",
            ...     name="earthlens.gee._helpers",
            ... )
            >>> _is_missing_dependency(exc, "earthlens.gee")
            False

            ```
        - Neither is a missing *name* in a module that imported fine — that is
          a version skew, and `pip install` would not fix it:
            ```python
            >>> from earthlens.earthlens import _is_missing_dependency
            >>> exc = ImportError("cannot import name 'Missing' from 'ee'", name="ee")
            >>> _is_missing_dependency(exc, "earthlens.gee")
            False

            ```
        - So is an ImportError raised by hand, with no module name at all:
            ```python
            >>> from earthlens.earthlens import _is_missing_dependency
            >>> _is_missing_dependency(ImportError("hand-rolled"), "earthlens.gee")
            False

            ```
    """
    if not isinstance(exc, ModuleNotFoundError):
        return False
    missing = getattr(exc, "name", None)
    if not missing:
        return False
    root = module_name.split(".")[0]
    return not (missing == root or missing.startswith(f"{root}."))


class _LazyRegistry(Mapping):
    """Maps a data-source key to its backend class, importing on demand.

    A read-only :class:`collections.abc.Mapping` over the registered
    backend keys: containment, iteration, `len()`, `.keys()` /
    `.values()` / `.items()` all work. The value is resolved on
    `__getitem__`, so backends whose optional SDK is not installed do
    not crash at package import time — a missing SDK surfaces as an
    `ImportError` naming the extra to install.

    Attributes:
        _mapping: Internal `key -> (module, class_name, extras_hint,
            default_kwargs)` table populated at construction. `default_kwargs`
            is merged under the user's `**backend_kwargs` by
            :meth:`EarthLens.__init__`, so an alias key can pre-bind a
            constructor argument (e.g. the STAC `"cdse"` alias pre-binds
            `endpoint="cdse"`) while a user-supplied value still wins.
    """

    def __init__(
        self, mapping: dict[str, tuple[str, str, str, dict[str, object]]]
    ) -> None:
        self._mapping = mapping

    def __contains__(self, key: object) -> bool:
        return key in self._mapping

    def __iter__(self):
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    def default_kwargs(self, key: str) -> dict[str, object]:
        """Return a copy of the constructor kwargs pre-bound to `key`.

        Args:
            key: A registered data-source key.

        Returns:
            The per-key default kwargs (empty for keys that pre-bind nothing).

        Examples:
            - An endpoint alias pre-binds the STAC `endpoint`:
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> EarthLens.DataSources.default_kwargs("cdse")
                {'endpoint': 'cdse'}

                ```
            - A plain key pre-binds nothing:
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> EarthLens.DataSources.default_kwargs("stac")
                {}

                ```
        """
        return dict(self._mapping[key][3])

    def entries(self) -> Iterator[tuple[str, str, str]]:
        """Yield `(key, module, extras)` for every registered backend key.

        A stable public view over the registry's internals — callers that
        need each key's backing module / pip-extra (e.g. tooling that
        enumerates the backends) should use this rather than reaching into
        the private mapping, so the internal tuple shape can change freely.

        Yields:
            One `(key, module_name, extras)` triple per registered key.
            `extras` is the empty string for SDK-free backends.

        Examples:
            - Every key resolves to its backend module and pip extra:

                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> entries = dict(
                ...     (key, (module, extra))
                ...     for key, module, extra in EarthLens.DataSources.entries()
                ... )
                >>> entries["chc"]
                ('earthlens.chc', '')
                >>> entries["gee"]
                ('earthlens.gee', 'gee')

                ```
        """
        for key, (module, _class_name, extras, _defaults) in self._mapping.items():
            yield key, module, extras

    def __getitem__(self, key: str) -> type[AbstractDataSource]:
        module_name, class_name, extras, _defaults = self._mapping[key]
        module = _import_backend_module(module_name, key, extras)
        return cast("type[AbstractDataSource]", getattr(module, class_name))


class EarthLens:
    """Facade that routes a download to the requested backend.

    The class-level :attr:`DataSources` mapping resolves a string key
    (`"chc"`, `"amazon-s3"`, `"ecmwf"`, or `"gee"` / its alias
    `"google-earth-engine"`) to the concrete
    :class:`AbstractDataSource` subclass that owns the request shape,
    authentication, and post-processing for that provider. Each
    backend's SDK is an optional dependency, so :attr:`DataSources`
    is a :class:`_LazyRegistry`: indexing it imports the backend on
    demand and rewrites a missing SDK into a friendly
    `ImportError` naming the extra to install
    (e.g. `pip install earthlens[ecmwf]`).

    Attributes:
        DataSources: Class-level lazy registry of registered backends.
            Keys are the user-facing names accepted by `data_source`;
            values resolve at access time to the corresponding
            subclasses of
            :class:`earthlens.base.AbstractDataSource`.
        datasource: Instance attribute set by :meth:`__init__` —
            holds the concrete backend that :meth:`download` routes to.

    Examples:
        - Inspect the registered backends:

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> sorted(EarthLens.DataSources)  # doctest: +NORMALIZE_WHITESPACE
            ['admin', 'admin-boundaries', 'airnow', 'alaska-satellite-facility', 'amazon-s3', 'aqueduct',
             'aqueduct-flood-risk', 'aqueduct-floods', 'argo', 'argo-floats', 'argopy', 'asf',
             'bare-earth-dem', 'bathymetry', 'bdc', 'brazil-data-cube', 'caravan', 'caravan-grdc',
             'catrare', 'cdse', 'chc', 'chirps', 'climate-indices', 'climate-projections',
             'climate_indices', 'cmems', 'cmip6', 'cop-dem', 'copernicus-dem', 'dea',
             'deafrica', 'dem', 'digital-earth-africa', 'digital-earth-australia', 'drought', 'earth-search',
             'earthdata', 'ecmwf', 'edo', 'eea-aq', 'efhm', 'elevation',
             'emdat', 'eodc', 'erddap', 'etopo', 'eumetsat', 'european-flood-hazard', 'fab-dem',
             'fabdem', 'fdsn', 'firms', 'flodis', 'flopros', 'g-portal',
             'gbif', 'gdacs', 'gdis', 'gdo', 'gebco', 'gee',
             'geoboundaries', 'gfw', 'ghs', 'ghsl', 'glaciers', 'glims',
             'global-forest-watch', 'global-solar-atlas', 'global-wind-atlas', 'gloh2o', 'goes', 'google-earth-engine',
             'grdc-caravan', 'gsa', 'gwa', 'hanze', 'hdx', 'himawari',
             'human-settlement', 'inform', 'insar', 'ioos', 'isimip', 'isric',
             'iucn', 'jaxa', 'jaxa-earth', 'jrc-flood', 'jrc-flood-hazard', 'landsat',
             'mswep', 'mswx', 'national-water-model', 'natural-earth', 'nexrad', 'nfhl',
             'nfip', 'nrel', 'nsi', 'nsrdb', 'nwis', 'nwm',
             'nwp', 'obis', 'ohsome', 'openaq', 'openeo', 'openstreetmap',
             'osm', 'overpass', 'overture', 'pangeo-cmip6', 'planetary-computer', 'protected-planet',
             'ptree', 'pvgis', 'radar', 'radklim', 'radolan', 'redlist',
             'rgi', 'risk-indicators', 'sensor-community', 'sentinel-hub', 'sentinelhub', 'soilgrids',
             'solar-pv', 'solar-wind-atlas', 'stac', 'teleconnections', 'thinkhazard', 'tiger',
             'tropycal', 'usdm', 'usgs-landsat', 'usgs-nwis', 'usgs-water', 'veda',
             'wdpa', 'wgms', 'wind-toolkit', 'world-pop', 'worldpop']

            ```
        - Asking for an unknown backend raises `ValueError`:

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> EarthLens(  # doctest: +ELLIPSIS
            ...     variables=[], data_source="not-a-real-source"
            ... )
            Traceback (most recent call last):
                ...
            ValueError: 'not-a-real-source' is not a supported data source. ...

            ```

    See Also:
        :class:`earthlens.chc.CHIRPS`: CHIRPS rainfall over FTP.
        :class:`earthlens.s3.S3`: AWS Open-Data datasets over public S3
            (ERA5, Sentinel-2, GOES, Copernicus DEM, ESA WorldCover) +
            an arbitrary-bucket passthrough.
        :class:`earthlens.asf.ASF`: Alaska Satellite Facility SAR
            search and InSAR baseline `stack()` via `asf_search`;
            reuses NASA Earthdata Login auth from
            :class:`earthlens.earthdata.EarthdataAuth`. Keys
            `"asf"` / `"alaska-satellite-facility"` / `"insar"`.
        :class:`earthlens.cmems.CMEMS`: Copernicus Marine ocean
            datasets via `copernicusmarine`.
        :class:`earthlens.earthdata.Earthdata`: NASA EOSDIS granules
            across 9 DAACs via `earthaccess` + CMR; the first backend
            whose `OUTPUT_KIND` is per-dataset (raster / vector /
            tabular), not fixed.
        :class:`earthlens.ecmwf.ECMWF`: ERA5 via the Copernicus
            Climate Data Store (cdsapi).
        :class:`earthlens.eumetsat.EUMETSAT`: EUMETSAT Data Store
            products (MTG / MSG / Metop / Sentinel-3/-5P/-6 mirrors)
            via `eumdac`; per-collection `OUTPUT_KIND`; key `"eumetsat"`.
        :class:`earthlens.fdsn.FDSN`: seismic events from the FDSN
            networks (USGS / EMSC / INGV / EarthScope / ISC / GeoNet)
            via `obspy`; the first `vector`-output backend.
        :class:`earthlens.gee.GEE`: imagery from Google Earth Engine
            (`earthengine-api`); keys `"gee"` / `"google-earth-engine"`.
        :class:`earthlens.gdacs.GDACS`: GDACS multi-hazard disaster
            alerts (public feed, no credentials); key `"gdacs"`.
        :class:`earthlens.emdat.EMDAT`: CRED EM-DAT disaster events and
            their geocoded locations — the UCLouvain Dataverse archive
            (`emdat:events`, anonymous, `tabular`, 1900 onwards, natural
            and technological) and GDIS (`gdis:points` / `gdis:polygons`,
            Earthdata Login, `vector`, 1960-2018). Per-instance
            `OUTPUT_KIND`; the archive carries a restricted-use
            `LicenseWarning`; keys `"emdat"` / `"gdis"`.
        :class:`earthlens.openaq.OpenAQ`: ground-station air-quality
            measurements from OpenAQ v3 (tabular `DataFrame`).
        :class:`earthlens.openeo.OpenEO`: server-side openEO process graphs
            (defaults to CDSE openEO); `raster` output, `aggregate=` is a
            native `aggregate_temporal_period` node; key `"openeo"`.
        :class:`earthlens.sentinel_hub.SentinelHub`: server-side Sentinel
            Hub render on CDSE (Process / Async / Batch raster, Statistical /
            Batch-Statistical tabular); `mixed` output, evalscript-driven;
            keys `"sentinel-hub"` / `"sentinelhub"`.
        :class:`earthlens.tropycal.TropicalCyclone`: tropical-cyclone
            best tracks via `tropycal` (`vector` output); key
            `"tropycal"`.
        :class:`earthlens.firms.FIRMS`: NASA FIRMS active-fire
            detections (MODIS / VIIRS) as a `vector` FeatureCollection;
            free `MAP_KEY`, no extra; key `"firms"`.
        :class:`earthlens.nwm.NWM`: NOAA National Water Model hydrologic
            output — per-reach streamflow (`chrtout`, `tabular`) and
            gridded land surface (`ldasout`, `raster`) — fetched whole
            from the anonymous `noaa-nwm-pds` bucket; tabular subsetting +
            the retrospective Zarr read via pyramids `LabeledDataset`. Keys
            `"nwm"` / `"national-water-model"`.
        :class:`earthlens.cmip6.CMIP6`: Raw CMIP6 climate projections — the
            full `model x scenario x variable x member` archive as
            analysis-ready Zarr on the open Pangeo `gs://cmip6` bucket. A
            facet tuple (`source_id` / `experiment_id` / `variable_id` /
            `table_id`) resolves to the store(s) and pyramids writes a
            bbox/time NetCDF subset (`raster`); anonymous, no extra. Keys
            `"cmip6"` / `"pangeo-cmip6"` / `"climate-projections"`.
        :class:`earthlens.goes.GOES`: NOAA GOES-R ABI geostationary imagery
            fetched whole (raw NetCDF granules, `raster`) from the anonymous
            `noaa-goes19` / `noaa-goes18` / `noaa-goes16` buckets by
            satellite / product / domain / scan-time window; rides the `[s3]`
            extra (unsigned boto3), no auth, decode is downstream
            (pyramids / satpy); key `"goes"`.
        :class:`earthlens.hdx.HDX`: Humanitarian Data Exchange resources
            via CKAN (`hdx-python-api`); the first `mixed`-output
            backend (downloads CSV / GeoTIFF / GeoPackage / … files
            as-is); public, no credentials; key `"hdx"`.
        :class:`earthlens.overture.Overture`: Overture Maps Foundation
            GeoParquet (buildings / places / transportation / divisions)
            over public S3 via `overturemaps`; `vector` FeatureCollection
            output with a per-row `license_id` column (and an ODbL
            `LicenseWarning`); no credentials; key `"overture"`.
        :class:`earthlens.nwp.NWP`: open numerical-weather-prediction
            forecasts (NOAA NODD / ECMWF Open Data / DWD) on a forecast
            `(cycle, step)` axis, returned as bbox-cropped COGs; key
            `"nwp"`.
        :class:`earthlens.radar.Radar`: NEXRAD Level-II radar volumes
            assembled from the real-time chunk feed (`vector` inventory);
            keys `"radar"` / `"nexrad"`.
        :class:`earthlens.usgs_water.USGSWater`: USGS NWIS / Water Data
            per-site water observations (discharge, gage height,
            water quality, …) via `dataretrieval` as a `tabular`
            `DataFrame`; optional `API_USGS_PAT`, anonymous works; keys
            `"usgs-water"` / `"usgs-nwis"` / `"nwis"`.
        :class:`earthlens.ghsl.GHSL`: JRC Global Human Settlement Layer
            (population / built-up / settlement-model grids + R2025A WUP
            projections) over open HTTPS, reprojected / mosaicked / cropped
            to the AOI via `pyramids` as `raster` GeoTIFFs (one per
            product × epoch; `aggregate=` reduces across epochs); no
            credentials; keys `"ghsl"` / `"ghs"` / `"human-settlement"`.
        :class:`earthlens.glaciers.Glaciers`: glacier outlines / fluctuations
            over three open sources — RGI 7.0 per-region outlines (UNESCO
            IHP-WINS) and GLIMS WFS time-series outlines as `vector`
            `FeatureCollection`s clipped to the AOI, and the WGMS Fluctuations
            of Glaciers (mass balance / front variation / state) as a
            `tabular` `DataFrame`; per-instance `OUTPUT_KIND`, `aggregate=`
            rejected; no credentials; keys `"glaciers"` / `"rgi"` / `"glims"`
            / `"wgms"`.
        :class:`earthlens.worldpop.WorldPop`: WorldPop open population data
            hub (CC-BY-4.0, no credentials) — per-country / global gridded
            population, density, age/sex, births, projections; mosaic +
            crop to the AOI via `pyramids` with a tidy age/sex table for
            demographic products; `mixed` output; keys `"worldpop"` /
            `"world-pop"`.
        :class:`earthlens.gbif.GBIF`: GBIF species occurrences via
            `pygbif` (anonymous); `vector` occurrence-point
            FeatureCollection; key `"gbif"`.
        :class:`earthlens.obis.OBIS`: OBIS marine occurrences via
            `pyobis` (anonymous); `vector` occurrence-point
            FeatureCollection; key `"obis"`.
        :class:`earthlens.drought.Drought`: Drought-indicator backend over
            three live services — US Drought Monitor (vector polygons via
            GeoJSON), Copernicus EDO/GDO (raster via the Copernicus drought
            `GetCoverage` REST endpoint), and CSIC SPEIbase (raster via
            NetCDF). Per-instance `OUTPUT_KIND`. No SDK extra. Four
            discoverability keys (`"drought"` / `"usdm"` / `"edo"` /
            `"gdo"`) all resolve to the same backend; all require an
            explicit `dataset=` kwarg.
        :class:`earthlens.wdpa.WDPA`: Protected Planet (WDPA)
            protected-area polygons via the direct v4 REST API
            (`?token=`); `vector` polygon FeatureCollection; keys
            `"wdpa"` / `"protected-planet"`.
        :class:`earthlens.iucn.IUCN`: IUCN Red List v4 assessments via
            the direct REST shim (Bearer token); `tabular` `DataFrame`
            (always a CC-BY-NC `LicenseWarning`); keys `"iucn"` /
            `"redlist"`.
        :class:`earthlens.argo.ARGO`: Argo autonomous-float ocean
            profiles (temperature / salinity / pressure, plus BGC
            parameters) via `argopy` as a `tabular` long-format
            `DataFrame`; region / `float:` / `profile:` selectors, open
            data (no auth); keys `"argo"` / `"argo-floats"` / `"argopy"`.
        :class:`earthlens.osm.OSM`: OpenStreetMap features over two public,
            keyless protocols — `overpy` (Overpass, current-state) +
            `ohsome` (OSM history / analytics) — as a `vector`
            FeatureCollection with an ODbL `LicenseWarning`; named-query
            catalog (`overpass:hospitals`, `ohsome:buildings`) + raw
            `query=` / `filter=` override; keys `"osm"` /
            `"openstreetmap"` / `"overpass"` / `"ohsome"`.

    """

    DataSources = _LazyRegistry(discover_backends())

    def __init__(
        self,
        data_source: str = "chc",
        variables: dict[str, list[str]] | list[str] | None = None,
        temporal_resolution: str = "daily",
        start: str | datetime | date | None = None,
        end: str | datetime | date | None = None,
        path: Path | str | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        fmt: str = "%Y-%m-%d",
        aoi: Any = None,
        buffer: float | None = None,
        dataset: str | None = None,
        cadence: str | None = None,
        time: Any = None,
        **backend_kwargs: object,
    ):
        """Resolve the backend and construct it with the user's parameters.

        Validates `data_source` against :attr:`DataSources`, fills in
        whole-Earth defaults for missing `lat_lim` / `lon_lim`, and
        instantiates the concrete backend bound to `self.datasource`.

        Args:
            data_source: Backend key. One of the registered keys in
                :attr:`DataSources` — `"chc"` (alias `"chirps"`),
                `"climate-indices"` (aliases `"climate_indices"` /
                `"teleconnections"`),
                `"amazon-s3"`, `"asf"` (aliases
                `"alaska-satellite-facility"` / `"insar"`),
                `"cmems"`, `"earthdata"`, `"ecmwf"`,
                `"eumetsat"`, `"fdsn"`, `"firms"`, `"gdacs"`, `"gee"`
                (alias `"google-earth-engine"`), `"ghsl"` (aliases
                `"ghs"` / `"human-settlement"`), `"glaciers"` (aliases
                `"rgi"` / `"glims"` / `"wgms"`), `"hdx"`,
                `"nrel"` (aliases `"nsrdb"` / `"wind-toolkit"`), `"nwp"`,
                `"openaq"`, `"openeo"`, `"overture"`, `"radar"` (alias
                `"nexrad"`), `"sentinel-hub"` (alias `"sentinelhub"`),
                `"pvgis"` (alias `"solar-pv"`),
                `"stac"` (with endpoint aliases `"planetary-computer"` /
                `"earth-search"` / `"cdse"`), `"tropycal"`,
                `"usgs-water"` (aliases `"usgs-nwis"` / `"nwis"`),
                `"worldpop"` (alias `"world-pop"`), `"argo"` (aliases
                `"argo-floats"` / `"argopy"`), or `"admin"` (aliases
                `"admin-boundaries"` / `"geoboundaries"` /
                `"natural-earth"` / `"tiger"`). See
                `sorted(EarthLens.DataSources)` for the live list.
                Defaults to `"chc"`.
            temporal_resolution: The download cadence — `"daily"` or
                `"monthly"` for most backends; the GEE backend also accepts
                `"raw"` and `"yearly"`. Prefer the `cadence=` alias for the
                download-cadence backends (CHIRPS / S3 / ECMWF / GEE). The
                concrete backend may accept a narrower
                set; check its `temporal_resolution` handling. Note the
                meaning is backend-specific:

                * a **download-loop cadence** that spaces the per-step
                  requests — CHIRPS, S3, ECMWF, GEE;
                * an **advisory label** only — NWP (the real cadence
                  comes from each model's metadata);
                * a **server-side rollup selector** — OpenAQ (picks the
                  measurements vs. hourly/daily endpoint);
                * a **service selector** — USGS Water (sub-daily maps to
                  the instantaneous service);
                * **ignored / forced to `"all"`** for the snapshot
                  backends with no per-step time axis — Overture,
                  Tropycal, FDSN, FIRMS, GDACS, Radar.

                For the first (download-cadence) group, `cadence` is the
                clearer spelling. Defaults to `"daily"`.
            cadence: Clearer alias for `temporal_resolution` in its
                download-cadence role (CHIRPS / S3 / ECMWF / GEE). When
                given, it overrides `temporal_resolution`. Defaults to
                `None`.
            start: Inclusive start date. A string (parsed with `fmt`,
                falling back to ISO-8601), or a `datetime` / `date` /
                `pandas.Timestamp` object. Defaults to `None`.
            end: Inclusive end date, same accepted types as `start`.
                Defaults to `None`.
            time: A single time range, the ergonomic alternative to the
                `start` / `end` pair (STAC `"a/b"` / earthaccess `(a, b)`
                idiom). Accepts a `"start/end"` string, a `(start, end)`
                2-sequence, a `slice`, or a single date (an instant). Splits
                into `start` / `end`; both bounds are required (an
                open-ended interval such as `"2020-01-01/"` raises), and
                passing it together with `start` / `end` raises
                `ValueError`. Defaults to `None`.
            path: Output directory. Created by the backend if it does
                not exist. When omitted (`None`), defaults to
                `<output_dir()>/<data_source>/` — the directory configured by
                `set_output_dir()` / `EARTHLENS_DATA_DIR`, else
                `~/.earthlens/data` — rather than the current working
                directory; pass `path=""` to opt into the CWD.
            dataset: Explicit dataset / collection key, the ergonomic
                alternative to keying it into `variables`. When given
                with a plain `variables` list, the facade composes the
                backend's request for you — the S3 backend receives it
                as a native `dataset` argument, and the dataset-keyed
                backends (ECMWF, GEE, CHC, …) receive the composed
                `{dataset: variables}` dict. Passing `dataset` together
                with a dict `variables` raises `ValueError` for the
                dataset-keyed backends. Defaults to `None` (the legacy
                nested-dict `variables` call is unchanged).
            variables: Backend-specific variable specification.
                Shape depends on the backend:

                * ECMWF: `dict[str, list[str]]` mapping CDS dataset
                  short name to a list of variable codes drawn from
                  that dataset, e.g.
                  `{"reanalysis-era5-single-levels": ["2m-temperature"]}`.
                * GEE: `dict[str, list[str]]` mapping an Earth Engine
                  asset id to a list of band ids, e.g.
                  `{"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]}`.
                * CHIRPS: either `list[str]` of variable codes
                  (legacy — auto-routed to the `"global-daily"` /
                  `"global-monthly"` dataset key via
                  `temporal_resolution`), or `dict[str, list[str]]`
                  mapping a CHIRPS catalog dataset key (e.g.
                  `"africa-pentad"`, `"chirps-v3-global-monthly"`)
                  to a list of variable codes drawn from that
                  dataset, e.g. `{"africa-monthly": ["precipitation"]}`.
                  See `Catalog().list_datasets()` for the curated
                  dataset keys.
                * S3 / ERA5: `list[str]` of variable codes from the
                  S3 backend's catalog.

                Defaults to `None`.
            lat_lim: Legacy latitude pair `[lat_min, lat_max]` — prefer the
                single `aoi=` channel, which also accepts a bbox. Defaults to
                :data:`DEFAULT_LATITUDE_LIMIT` (whole Earth). Mutually
                exclusive with `aoi`.
            lon_lim: Legacy longitude pair `[lon_min, lon_max]` — prefer the
                single `aoi=` channel. Defaults to
                :data:`DEFAULT_LONGITUDE_LIMIT` (whole Earth). Mutually
                exclusive with `aoi`.
            fmt: `strptime` format tried first when `start` / `end` are
                strings; a non-matching string falls back to an ISO-8601
                parse, and `datetime` / `date` objects ignore it. An
                optional override rather than a requirement. Defaults to
                `"%Y-%m-%d"`.
            aoi: A single area-of-interest, the ergonomic alternative to
                the `lat_lim` / `lon_lim` pair. Accepts a bbox
                `[min_lon, min_lat, max_lon, max_lat]` (GeoJSON W, S, E,
                N order), a bbox mapping, a `(lon, lat)` point (with
                `buffer`), a shapely geometry, any `__geo_interface__`
                object, a GeoJSON geometry / Feature, a WKT string, or a
                `GeoDataFrame` / `GeoSeries`. Reduced to `lat_lim` /
                `lon_lim` by :func:`earthlens.base.spatial.normalize_aoi`.
                Passing both `aoi` and `lat_lim` / `lon_lim` raises
                `ValueError`. Defaults to `None`.
            buffer: Half-width in degrees applied to a `(lon, lat)` point
                `aoi` to grow it into a square box. Only valid together
                with a point `aoi`. Defaults to `None`.
            **backend_kwargs: Extra keyword arguments forwarded
                verbatim to the chosen backend's constructor — for
                backend-specific options the facade does not name
                explicitly (e.g. ECMWF's `skip_constraints`, or GEE's
                `scale` / `crs` / `reducer` / `export_via` /
                `drive_folder` / `gcs_bucket` / `region` / `cloud_mask` /
                `filters` / `engine` / `cog`). Credentials are
                not constructor kwargs: backends that defer auth take them
                on `authenticate()` instead (e.g. GEE's `service_account`
                / `service_key` / `project`), so forwarding those through
                the facade raises `TypeError`. A kwarg the backend does
                not accept is its `TypeError`, not the facade's.

        Raises:
            ValueError: If `data_source` is not a key of
                :attr:`DataSources`, if both `aoi` and
                `lat_lim` / `lon_lim` are given, if `buffer` is given
                without a point `aoi`, or if `aoi` is malformed.
            AuthenticationError: For backends that defer auth
                (ECMWF, GEE, STAC, CMEMS, …) the network handshake is
                lazy, so an auth failure surfaces on the first
                `authenticate()` / `download()` / `search()`, not at
                construction. GEE resolves and opens its Earth Engine
                connection lazily too: its *offline* precondition (no
                `service_account` + `service_key` / `project` and no
                matching environment variable) and its actual Earth
                Engine errors (invalid key, unregistered project) all
                surface on first use, not here.
            ImportError: If the chosen backend's optional SDK is not
                installed (e.g. `data_source="gee"` without
                `pip install earthlens[gee]`).

        Examples:
            - The DataSources registry resolves the backend class
              before construction. Inspect what each key points to:

                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> EarthLens.DataSources["chc"].__name__
                'CHIRPS'
                >>> EarthLens.DataSources["chirps"].__name__  # alias
                'CHIRPS'
                >>> EarthLens.DataSources["ecmwf"].__name__
                'ECMWF'
                >>> EarthLens.DataSources["gee"].__name__
                'GEE'

                ```
            - An unknown `data_source` is rejected before any backend
              code runs:

                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> EarthLens(  # doctest: +ELLIPSIS
                ...     variables=[], data_source="bogus"
                ... )
                Traceback (most recent call last):
                    ...
                ValueError: 'bogus' is not a supported data source. ...

                ```
            - Construct an ECMWF-backed facade. Marked
              `# doctest: +SKIP` because it builds a real
              :class:`cdsapi.Client`, which requires
              `~/.cdsapirc`:

                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> earthlens = EarthLens(  # doctest: +SKIP
                ...     data_source="ecmwf",
                ...     temporal_resolution="daily",
                ...     start="2022-01-01",
                ...     end="2022-01-01",
                ...     variables={
                ...         "reanalysis-era5-single-levels": ["2m-temperature"],
                ...     },
                ...     lat_lim=[4.0, 5.0],
                ...     lon_lim=[-75.0, -74.0],
                ...     path="examples/data/era5",
                ... )

                ```

        See Also:
            :meth:`download`: Triggers the actual retrieval.
        """
        # Back-compat for the legacy positional order
        # `EarthLens(variables, data_source, ...)`. `data_source` is now
        # the first positional; a caller who passed `variables` first sends
        # a list / dict where a source key (a `str`) is expected, so detect
        # that and swap, with a deprecation warning. Runs before
        # `_check_source` so the (now-correct) key is the one validated.
        if not isinstance(data_source, str):
            warnings.warn(
                "EarthLens(variables, data_source, ...) is deprecated; pass "
                "data_source first: EarthLens(data_source, variables=...).",
                DeprecationWarning,
                stacklevel=2,
            )
            data_source, variables = (
                variables if isinstance(variables, str) else "chc",
                data_source,
            )
        self._check_source(data_source)

        # Most backends take a `variables` (or `dataset`) request axis. A few —
        # currently only CMIP6 — address their data purely by facet keywords
        # (`variable_id=`, `source_id=`, ...) and declare no `variables`
        # parameter; those neither require nor accept `variables=`/`dataset=`.
        # A backend is treated as facet-only *only* when its constructor
        # explicitly declares neither `variables` nor a catch-all `**kwargs`
        # (a `MagicMock` test double, or any pass-through signature, keeps the
        # historical variables-required behaviour).
        backend_cls = self.DataSources[data_source]
        backend_params = inspect.signature(backend_cls.__init__).parameters
        has_var_keyword = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in backend_params.values()
        )
        requires_variables = "variables" in backend_params or has_var_keyword
        if requires_variables:
            if variables is None and dataset is None:
                raise ValueError(
                    "variables= is required (or pass dataset= for a "
                    "dataset-keyed backend), e.g. "
                    "EarthLens('chc', variables=['precipitation'])."
                )
        elif variables is not None or dataset is not None:
            raise ValueError(
                f"the {data_source!r} backend addresses its data by facet "
                "keyword arguments (e.g. variable_id=, source_id=), not "
                "variables=/dataset=."
            )

        # `cadence=` is the clearer alias for the download-cadence role of
        # `temporal_resolution`; when given it simply overrides it.
        if cadence is not None:
            temporal_resolution = cadence

        # A single `time=` range supersedes the separate `start` / `end`
        # pair (STAC `"a/b"` / earthaccess `(a, b)` idiom). Split it into
        # the `start` / `end` the backend already consumes.
        if time is not None:
            if start is not None or end is not None:
                raise ValueError("pass either time= or start=/end=, not both")
            start, end = split_time(time)
            # An open-ended interval (`"2020-01-01/"`) would leave a `None`
            # bound that the backend silently expands to "now" — a surprise
            # multi-year span. Require both bounds explicitly.
            if start is None or end is None:
                raise ValueError(
                    "time= needs both bounds; got an open-ended interval. "
                    "Pass an explicit start and end (e.g. "
                    "time='2020-01-01/2020-01-31')."
                )

        # A single `aoi=` supersedes the legacy `lat_lim` / `lon_lim`
        # pair. It accepts a bbox, a point (+ `buffer`), a shapely /
        # GeoJSON / WKT geometry, or a GeoDataFrame, and is reduced to
        # the `[min, max]` pairs every backend already consumes. A backend
        # that declares its own richer `aoi` parameter (e.g. WorldPop's
        # ISO3 / bbox / GeoDataFrame) instead receives `aoi` verbatim and
        # interprets it itself.
        clip_geometry = None
        if aoi is not None and "aoi" in backend_params:
            if buffer is not None:
                raise ValueError(
                    f"buffer= is not supported by the {data_source!r} backend, "
                    "which interprets aoi= itself"
                )
            backend_kwargs = {**backend_kwargs, "aoi": aoi}
        elif aoi is not None:
            if lat_lim is not None or lon_lim is not None:
                raise ValueError("pass either aoi= or lat_lim=/lon_lim=, not both")
            lat_lim, lon_lim, clip_geometry = resolve_aoi(aoi, buffer=buffer)
        elif buffer is not None:
            raise ValueError(
                "buffer= only applies to a point aoi=(lon, lat); pass aoi= too"
            )

        if lat_lim is None:
            lat_lim = DEFAULT_LATITUDE_LIMIT
        if lon_lim is None:
            lon_lim = DEFAULT_LONGITUDE_LIMIT

        # An omitted `path` makes earthlens manage the location: `download()`
        # persists to a named per-source subdirectory of the configured output
        # directory rather than scattering files into the cwd, while `load()`
        # (which only needs the in-memory object) redirects to a throwaway temp
        # dir and removes the empty default afterwards. An explicit `path=""`
        # still means the CWD (a deliberate choice).
        self._explicit_path = path is not None
        if path is None:
            path = output_dir() / data_source
            logger.info(
                f"No `path` given; download() writes {data_source!r} output under "
                f"{path}/ (load() uses a temp dir)."
            )

        # Per-key defaults (e.g. the STAC endpoint aliases pre-bind
        # `endpoint=`) are merged *under* the user's kwargs, so an
        # explicit value always wins.
        merged_kwargs: dict[str, Any] = {
            **self.DataSources.default_kwargs(data_source),
            **backend_kwargs,
        }

        # Reject an unknown backend kwarg here, with a did-you-mean, rather
        # than letting it surface as a raw TypeError from deep in the
        # backend constructor.
        self._check_backend_kwargs(data_source, backend_params, merged_kwargs)

        # `dataset=` + a plain `variables` list is resolved into the
        # shape each backend wants — a native `dataset` kwarg for the S3
        # backend, or the legacy `{dataset: variables}` dict for the
        # dataset-keyed backends. With `dataset=None` this is a no-op.
        from earthlens.base._requests import normalize_dataset_variables

        request_kwargs = (
            normalize_dataset_variables(backend_cls, dataset, variables)
            if requires_variables
            else {}
        )

        self.datasource = backend_cls(
            # The facade accepts str/datetime/date/None and the concrete
            # backends' `_check_input_dates` parse the broader forms; the base
            # signature only declares `str`.
            start=cast("str", start),
            end=cast("str", end),
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            temporal_resolution=temporal_resolution,
            path=path,
            fmt=fmt,
            **request_kwargs,
            **merged_kwargs,
        )
        # A polygon `aoi=` records its mask on the backend's spatial extent
        # so raster backends clip the fetched bbox to the exact shape.
        if clip_geometry is not None:
            self.datasource._attach_clip_geometry(clip_geometry)

    @classmethod
    def _check_source(cls, data_source: str) -> None:
        """Validate `data_source` against the registry, with a did-you-mean hint.

        Args:
            data_source: The backend key to validate.

        Raises:
            ValueError: If `data_source` is not a registered key. The
                message names the closest registered key (via `difflib`)
                and lists the known keys.
        """
        if data_source not in cls.DataSources:
            close = difflib.get_close_matches(data_source, list(cls.DataSources), n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{data_source!r} is not a supported data source. "
                f"Known: {sorted(cls.DataSources)}.{hint}"
            )

    #: Constructor parameter names the facade owns and supplies itself.
    #: Everything else a backend declares is a backend-specific option,
    #: surfaced by :meth:`options_for` and accepted as a kwarg.
    _FACADE_PARAMS = frozenset(
        {
            "self",
            "start",
            "end",
            "variables",
            "dataset",
            "lat_lim",
            "lon_lim",
            "temporal_resolution",
            "path",
            "fmt",
            "aoi",
            "buffer",
        }
    )

    @classmethod
    def options_for(cls, data_source: str) -> list[str]:
        """List a backend's extra constructor options.

        The backend-specific keyword arguments a backend accepts beyond
        the facade's own parameters — the discoverable surface for
        `**backend_kwargs` (e.g. GEE's `scale` / `crs` / `service_account`,
        ECMWF's `skip_constraints`).

        Args:
            data_source: A registered backend key.

        Returns:
            The sorted backend-specific option names.

        Raises:
            ValueError: If `data_source` is not a registered key.

        Examples:
            - GEE exposes its export knobs as options:
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> options = EarthLens.options_for("gee")
                >>> "scale" in options and "crs" in options
                True

                ```
        """
        cls._check_source(data_source)
        params = inspect.signature(cls.DataSources[data_source].__init__).parameters
        return sorted(
            name
            for name, parameter in params.items()
            if name not in cls._FACADE_PARAMS
            and parameter.kind not in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL)
        )

    @classmethod
    def _check_backend_kwargs(
        cls, data_source: str, backend_params: Any, kwargs: dict[str, object]
    ) -> None:
        """Reject an unexpected backend kwarg with a did-you-mean hint.

        Args:
            data_source: The backend key (for the error message).
            backend_params: The backend `__init__` parameters mapping.
            kwargs: The merged backend kwargs about to be forwarded.

        Raises:
            TypeError: If a key in `kwargs` is not an accepted backend
                parameter (skipped when the backend declares `**kwargs`).
        """
        if any(p.kind == p.VAR_KEYWORD for p in backend_params.values()):
            return
        accepted = {name for name in backend_params if name != "self"}
        for name in kwargs:
            if name not in accepted:
                close = difflib.get_close_matches(name, sorted(accepted), n=1)
                hint = f" Did you mean {close[0]!r}?" if close else ""
                raise TypeError(
                    f"the {data_source!r} backend got an unexpected keyword "
                    f"argument {name!r}.{hint} Backend options: "
                    f"{cls.options_for(data_source)}."
                )

    @classmethod
    def catalog(cls, data_source: str) -> AbstractCatalog:
        """Return the dataset / variable catalog for a backend.

        The catalogs hold the curated dataset and variable metadata
        (cadence, extent, bands) each backend ships, so they are the
        natural place to discover valid `dataset=` / `variables=` values
        without constructing the backend or hitting the network.

        Args:
            data_source: A registered backend key (see
                `sorted(EarthLens.DataSources)`).

        Returns:
            The backend's `Catalog` instance, loaded from its bundled
            data.

        Raises:
            ValueError: If `data_source` is not a registered key.
            ImportError: If the backend's optional SDK is required to
                import its module and is not installed.
            NotImplementedError: If the backend ships no catalog.

        Examples:
            - The CHC catalog exposes its curated datasets:
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> catalog = EarthLens.catalog("chc")
                >>> len(catalog) > 0
                True

                ```
        """
        cls._check_source(data_source)
        module_name, extras = next(
            (mod, extra)
            for key, mod, extra in cls.DataSources.entries()
            if key == data_source
        )
        module = _import_backend_module(
            module_name, data_source, extras, subject="Backend catalog for"
        )
        catalog_cls = getattr(module, "Catalog", None)
        if catalog_cls is None:
            raise NotImplementedError(
                f"the {data_source!r} backend ships no catalog to query"
            )
        return cast("AbstractCatalog", catalog_cls())

    @classmethod
    def list_datasets(cls, data_source: str) -> list[str]:
        """List a backend's curated dataset keys.

        Args:
            data_source: A registered backend key.

        Returns:
            The sorted curated dataset keys — the values accepted by
            `dataset=` (and `describe_dataset`).

        Raises:
            ValueError: If `data_source` is not a registered key.

        Examples:
            - CHC ships a curated dataset for African monthly rainfall:
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> "africa-monthly" in EarthLens.list_datasets("chc")
                True

                ```
        """
        return sorted(cls.catalog(data_source).datasets)

    @classmethod
    def describe_dataset(cls, data_source: str, key: str) -> Any:
        """Return the catalog record for one dataset.

        Args:
            data_source: A registered backend key.
            key: A curated dataset key (see :meth:`list_datasets`).

        Returns:
            The backend-specific dataset record (carries the dataset's
            variables / bands, cadence, and extent).

        Raises:
            ValueError: If `data_source` is unknown, or `key` is not a
                curated dataset (the message suggests the closest key).

        Examples:
            - Inspect a CHC dataset's declared variables:
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> dataset = EarthLens.describe_dataset("chc", "africa-monthly")
                >>> bool(dataset.variables)
                True

                ```
            - An unknown key is rejected with a did-you-mean hint:
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> EarthLens.describe_dataset(  # doctest: +ELLIPSIS
                ...     "chc", "africa-month"
                ... )
                Traceback (most recent call last):
                    ...
                ValueError: 'africa-month' is not in the ...

                ```
        """
        return cls.catalog(data_source).get_dataset(key)

    @classmethod
    def guess_dataset(cls, data_source: str, text: str) -> list[str]:
        """Search a backend's datasets by free text.

        A discovery aid in the spirit of eodag's `guess_product_type`:
        case-insensitive substring matches first (capped), falling back
        to `difflib` fuzzy matches when nothing contains `text`. Searches
        the full `available_datasets` universe plus the curated keys.

        Args:
            data_source: A registered backend key.
            text: The free-text fragment to search for.

        Returns:
            Matching dataset keys, best matches first (may be empty).

        Raises:
            ValueError: If `data_source` is not a registered key.

        Examples:
            - Find the CHC monthly datasets:
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> "africa-monthly" in EarthLens.guess_dataset("chc", "monthly")
                True

                ```
        """
        cat = cls.catalog(data_source)
        pool = sorted(set(cat.available_datasets) | set(cat.datasets))
        lowered = text.lower()
        hits = [name for name in pool if lowered in name.lower()]
        if hits:
            return hits[:20]
        return difflib.get_close_matches(text, pool, n=10, cutoff=0.3)

    def __getattr__(self, name: str) -> Any:
        """Delegate an unknown attribute to the bound backend.

        Python calls this only when normal lookup misses, so the facade's
        own attributes and methods always take precedence. It lets the
        facade transparently expose a backend's *own* surface without
        forcing it onto every backend — e.g.
        `EarthLens(data_source="nwm", ...)._feature_ids()` forwards to the
        NWM backend, while the same call on a CHIRPS-backed facade raises
        `AttributeError` (CHIRPS has no such method).

        Args:
            name: The attribute being looked up.

        Returns:
            The corresponding attribute of `self.datasource`.

        Raises:
            AttributeError: If `name` is a dunder, the backend is not yet
                bound (mid-construction), or the backend lacks `name`.

        Examples:
            - A backend-specific helper is reachable through the facade
              (live; skipped here):
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> el = EarthLens(  # doctest: +SKIP
                ...     data_source="nwm",
                ...     dataset="chrtout",
                ...     variables=["streamflow"],
                ...     start="2024-01-01", end="2024-01-01",
                ... )
                >>> el._feature_ids()  # doctest: +SKIP

                ```
        """
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        datasource = self.__dict__.get("datasource")
        if datasource is None:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )
        return getattr(datasource, name)

    def __dir__(self) -> list[str]:
        """Include the bound backend's attributes for tab-completion."""
        own = set(super().__dir__())
        datasource = self.__dict__.get("datasource")
        if datasource is not None:
            own |= set(dir(datasource))
        return sorted(own)

    def authenticate(self, **credentials: Any) -> EarthLens:
        """Eagerly authenticate the bound backend; raise on failure.

        The explicit, fail-fast counterpart to the lazy authentication
        that otherwise happens on the first :meth:`download` / `search`.
        Delegates to
        :meth:`earthlens.base.AbstractDataSource.authenticate` — opening
        the network client or running the credential step — and is a
        no-op for credential-free backends. Lets callers separate "do I
        have valid credentials?" from the download itself, e.g. wrap only
        `authenticate()` in a `try/except AuthenticationError`.

        Any keyword arguments are forwarded verbatim to the backend's
        `authenticate`, so a backend that takes its credential there
        receives it — e.g. FIRMS accepts `api_key=` (falling back to the
        `FIRMS_MAP_KEY` environment variable when omitted). Passing a
        credential a backend does not accept raises `TypeError`.

        Args:
            **credentials: Backend-specific credential keywords forwarded
                to the bound backend's `authenticate` (e.g. FIRMS's
                `api_key`). Empty for credential-free or env-resolved
                backends.

        Returns:
            The facade, so it chains:
            `EarthLens(...).authenticate().download()`.

        Raises:
            AuthenticationError: If the backend cannot authenticate.

        Examples:
            - Verify credentials up front, then download (live; skipped
              here):
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> EarthLens(  # doctest: +SKIP
                ...     data_source="ecmwf",
                ...     dataset="reanalysis-era5-single-levels",
                ...     variables=["2m-temperature"],
                ...     start="2022-01-01", end="2022-01-01",
                ... ).authenticate().download()

                ```
            - Pass a FIRMS key explicitly at the auth step (live;
              skipped here):
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> EarthLens(  # doctest: +SKIP
                ...     data_source="firms",
                ...     variables=["VIIRS_SNPP_NRT"],
                ...     start="2024-08-01", end="2024-08-01",
                ...     lat_lim=[33.0, 35.0], lon_lim=[-119.0, -117.0],
                ... ).authenticate(api_key="…").download()

                ```
        """
        self.datasource.authenticate(**credentials)
        return self

    def search(self) -> list[RemoteProduct]:
        """List the products this request matches, without downloading them.

        The cheap, dry-run half of the search→fetch split: it queries the
        backend's catalog and returns one
        :class:`~earthlens.base.RemoteProduct` per item that
        :meth:`download` would fetch, so you can inspect or filter the
        result first.

        Returns:
            One :class:`~earthlens.base.RemoteProduct` per matching item;
            the empty list when nothing matches.

        Raises:
            NotImplementedError: If the bound backend keeps the legacy
                `_api`-only flow (CHIRPS, S3, ECMWF, GEE) and exposes no
                searchable product list — call :meth:`download` directly.

        Examples:
            - Preview a STAC search without fetching any bytes (live;
              skipped here):
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> products = EarthLens(  # doctest: +SKIP
                ...     data_source="earth-search",
                ...     dataset="sentinel-2-l2a",
                ...     variables=["red"],
                ...     start="2024-01-01", end="2024-01-31",
                ...     aoi=[-75.0, 4.0, -74.0, 5.0],
                ... ).search()

                ```
        """
        try:
            return self.datasource._search()
        except NotImplementedError as exc:
            raise NotImplementedError(
                f"the {type(self.datasource).__name__} backend does not "
                f"support search()/preview()/count(); call download() instead."
            ) from exc

    def count(self) -> int:
        """Return how many products this request matches, without downloading.

        Uses the backend's :meth:`~earthlens.base.AbstractDataSource._count`
        hook, which a backend with a cheap server-side total overrides;
        otherwise it counts a :meth:`search`.

        Returns:
            The number of matching products.

        Raises:
            NotImplementedError: If the bound backend exposes no
                searchable product list (see :meth:`search`).
        """
        try:
            return self.datasource._count()
        except NotImplementedError as exc:
            raise NotImplementedError(
                f"the {type(self.datasource).__name__} backend does not "
                f"support search()/preview()/count(); call download() instead."
            ) from exc

    def preview(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the first `n` matching products as plain dicts.

        A notebook-friendly view over :meth:`search`: each product is
        flattened to `{"id": ..., "href": ..., **metadata}` for quick
        tabular display before committing to a download.

        Args:
            n: Maximum number of products to return. Defaults to 10.

        Returns:
            Up to `n` dicts, each carrying the product `id`, `href`, and
            its backend-specific metadata.

        Raises:
            NotImplementedError: If the bound backend exposes no
                searchable product list (see :meth:`search`).
        """
        return [
            {"id": product.id, "href": product.href, **product.metadata}
            for product in self.search()[:n]
        ]

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
        *args: object,
        **kwargs: object,
    ) -> Any:
        """Delegate the download to the bound backend.

        Forwards every argument verbatim to `self.datasource.download`.
        Each backend's `download` accepts its own backend-specific
        keyword arguments (for example, CHIRPS supports `cores` for
        parallel FTP retrieval), so unrecognised kwargs propagate
        through.

        Args:
            progress_bar: Whether the backend should print a per-date
                progress bar during the loop. Defaults to `True`.
            aggregate: Optional :class:`earthlens.aggregate.AggregationConfig`.
                Two conditions must both hold for it to be forwarded:
                the backend's `OUTPUT_KIND` is `"raster"` or
                `"mixed"` — the shapes for which a gridded reduction
                is well-defined — **and** the backend declares
                `SUPPORTS_AGGREGATE`. A `"vector"` / `"tabular"`
                backend is refused because the aggregator has no
                meaningful semantics on `GeoDataFrame` / `DataFrame`
                rows; a raster backend that has not wired the reducer
                yet is refused for that reason instead. Either way the
                refusal is a `NotImplementedError` raised before the
                backend's `download` runs, carrying the backend's own
                `AGGREGATE_REFUSAL_REASON`. A backend without an
                explicit `OUTPUT_KIND` is treated as `"raster"` for
                back-compatibility.
            *args: Forwarded positionally to `backend.download`.
            **kwargs: Forwarded as keywords to `backend.download`.

        Returns:
            Whatever the bound backend's `download` returns. The shape
            tracks the backend's `OUTPUT_KIND`:

            * `"raster"` / `"mixed"` file-writers — the list of written
                paths (`list[Path]`); GEE may also return export
                destination strings / `TaskInfo` for async exports.
            * `"vector"` — an in-memory `FeatureCollection` (e.g. FDSN,
                FIRMS, GDACS); radar returns a `GeoDataFrame`.
            * `"tabular"` — a `pandas.DataFrame` (e.g. OpenAQ,
                USGS Water).

            The legacy CHIRPS / ECMWF backends return their written
            `list[Path]` and also leave the files on disk under `path`.

        Raises:
            AuthenticationError: When the ECMWF backend cannot
                authenticate against CDS (typically a missing
                `~/.cdsapirc`). See
                :class:`earthlens.ecmwf.AuthenticationError`.
            KeyError: When any backend receives an unknown variable
                code that the catalog cannot resolve.
            NotImplementedError: When `aggregate=` is not `None` and the
                bound backend does not support it — either because its
                `OUTPUT_KIND` is `"vector"` / `"tabular"` (those emit
                `GeoDataFrame` / `DataFrame` rows, which have no meaningful
                gridded reduction) or because it is a raster backend that has
                not wired the reducer and so does not declare
                `SUPPORTS_AGGREGATE`.

        Examples:
            - End-to-end CHIRPS download. Marked `# doctest: +SKIP`
              because it makes a live FTP connection:

                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> earthlens = EarthLens(  # doctest: +SKIP
                ...     data_source="chc",
                ...     start="2009-01-01",
                ...     end="2009-01-02",
                ...     variables=["precipitation"],
                ...     lat_lim=[4.19, 4.64],
                ...     lon_lim=[-75.65, -74.73],
                ...     path="examples/data/chirps",
                ... )
                >>> earthlens.download()  # doctest: +SKIP

                ```
            - ECMWF download via the facade. Marked
              `# doctest: +SKIP` because CDS requires
              `~/.cdsapirc` and the request blocks for minutes
              while the queue serves it:

                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> earthlens = EarthLens(  # doctest: +SKIP
                ...     data_source="ecmwf",
                ...     start="2022-01-01",
                ...     end="2022-01-01",
                ...     variables={
                ...         "reanalysis-era5-single-levels": ["2m-temperature"],
                ...     },
                ...     lat_lim=[4.0, 5.0],
                ...     lon_lim=[-75.0, -74.0],
                ...     path="examples/data/era5",
                ... )
                >>> earthlens.download()  # doctest: +SKIP

                ```

        See Also:
            :meth:`earthlens.chc.CHIRPS.download`: CHIRPS
                backend implementation, including the `cores=`
                keyword for parallel retrieval.
            :meth:`earthlens.s3.S3.download`: S3/ERA5 backend
                implementation.
            :meth:`earthlens.ecmwf.ECMWF.download`: ECMWF/CDS
                backend implementation.
            :meth:`earthlens.gee.GEE.download`: Google Earth Engine
                backend implementation (`export_via`, the 32768-px
                synchronous cap).
        """
        return self._dispatch_download(
            *args, progress_bar=progress_bar, aggregate=aggregate, **kwargs
        )

    def iter_download(self, *, limit: int | None = None) -> Iterator[Any]:
        """Stream the backend's artifacts one item at a time.

        The streaming counterpart to :meth:`download`, for a vector / tabular
        result too large to want resident all at once: each product's fragment
        is yielded as it arrives, so a caller can write or reduce it and let it
        go before the next one is fetched.

        Whether a backend can stream depends on it having a per-product fetch
        hook — most search/fetch backends do; a backend that answers the whole
        request in one server-side call cannot, and says so rather than
        pretending. `aggregate=` is not accepted: a reduction needs the whole
        cube, so ask for it through :meth:`download`.

        Args:
            limit: Total rows / features to yield across every product, or
                `None` for no cap. Products past the cap are never fetched.

        Yields:
            Any: One fragment per product, of the same type the backend's
                `download` collects (a `FeatureCollection` /
                `GeoDataFrame` / `DataFrame` fragment, or written paths).

        Raises:
            NotImplementedError: When the bound backend has no per-product
                fetch to stream from.
            TypeError: If `limit` is neither `None` nor an `int`.
            ValueError: If `limit` is less than 1.

        Examples:
            - Consume a large query without holding every page. Marked
              `# doctest: +SKIP` because it performs a live request:
                ```python
                >>> from earthlens.core import EarthLens
                >>> earthlens = EarthLens(  # doctest: +SKIP
                ...     data_source="obis",
                ...     start="2024-01-01",
                ...     end="2024-01-31",
                ...     lat_lim=[50.0, 54.0],
                ...     lon_lim=[2.0, 7.0],
                ... )
                >>> total = 0  # doctest: +SKIP
                >>> for fragment in earthlens.iter_download():  # doctest: +SKIP
                ...     total += len(fragment)

                ```
        """
        self.datasource._ensure_root_dir()
        return self.datasource.iter_download(limit=limit)

    def _redirect_output_to_tempdir(self) -> None:
        """Point the backend output at a throwaway temp dir for an in-memory load.

        Called by :meth:`load` when `path` was omitted: `load` only needs the
        in-memory object, so the incidental files go to a fresh temp directory
        instead of the persistent `<output_dir()>/<source>/` default, and a
        load-and-plot run never leaves files in the working tree. Creating the
        temp dir here (not at construction) means a construct-only or
        download-only run allocates no temp directory.

        The `rmdir` below is now belt-and-braces: construction no longer
        creates `root_dir` at all, so in a fresh process there is nothing to
        remove. It still fires for a directory an earlier run left empty.
        """
        default_dir = getattr(self.datasource, "root_dir", None)
        tmp = Path(tempfile.mkdtemp(prefix="earthlens-load-"))
        self.datasource.root_dir = tmp
        self.datasource.path = tmp
        if default_dir is not None and default_dir != tmp:
            try:
                if default_dir.is_dir() and not any(default_dir.iterdir()):
                    default_dir.rmdir()
            except OSError:
                pass

    def _dispatch_download(
        self,
        *args: object,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
        **kwargs: object,
    ) -> Any:
        """Run the `aggregate=` guard, then forward to the backend's `download`.

        The shared fetch path behind :meth:`download` (which first redirects an
        omitted `path` to the persistent directory) and :meth:`load` (which
        keeps the throwaway temp directory). Rejects a non-`None` `aggregate`
        before the backend is called — for a non-raster backend, and equally
        for a raster one that does not declare `SUPPORTS_AGGREGATE`.

        Args:
            *args: Forwarded positionally to `backend.download`.
            progress_bar: Whether the backend prints its progress bar.
            aggregate: Optional aggregation config; valid only for a
                `"raster"` / `"mixed"` backend that also declares
                `SUPPORTS_AGGREGATE`.
            **kwargs: Forwarded as keywords to `backend.download`.

        Returns:
            Whatever the bound backend's `download` returns.

        Raises:
            NotImplementedError: If `aggregate` is not `None` and the backend
                either is not `raster`/`mixed` or does not declare
                `SUPPORTS_AGGREGATE`.
        """
        if aggregate is not None:
            # Forward it and let the backend's `download` wrapper decide. The
            # wrapper checks `SUPPORTS_AGGREGATE`, which is the honest question:
            # `OUTPUT_KIND` used to stand in for it here and could not answer it,
            # because plenty of `"raster"` backends emit grids with no time axis
            # to reduce. Keeping the check in one place also means a direct
            # `backend.download(aggregate=...)` is refused identically.
            kwargs["aggregate"] = aggregate

        # The abstract `download` now declares `(self, progress_bar)` and each
        # backend adds its own optional arguments (ARC-2), which is exactly what
        # cannot be typed at this call site: the facade is handed a backend
        # chosen at runtime by string key, so the extra keywords it forwards are
        # only checkable against the concrete class. The narrow ignore below is
        # the price of that dispatch; a wrong keyword still fails loudly at the
        # backend, and `options_for` validates the request up front.
        # Cast rather than a stack of ignores: at this point the signature
        # genuinely is not statically known, and saying so is more honest than
        # suppressing three separate errors about it.
        download = cast("Callable[..., Any]", self.datasource.download)
        return download(*args, progress_bar=progress_bar, **kwargs)

    def load(self, *args: object, **kwargs: Any) -> Any:
        """Download and return the data in memory instead of only on disk.

        The lazy-stack convenience: runs :meth:`download` and hands back the
        fetched data as the project's **native pyramids objects** rather than
        leaving the caller to re-open files. Raster outputs are read into a
        `pyramids.Dataset` / `NetCDF` (a `.nc` path becomes a `NetCDF`, every
        other raster a `Dataset`); a non-raster output a backend already
        returns in memory (a `FeatureCollection` / `GeoDataFrame` /
        `DataFrame`) is passed through unchanged, as are non-raster file paths
        (e.g. a `.csv` table from a mixed backend). The files are still written
        to `path` — `load` adds the in-memory handle on top.

        When `path` was omitted, `load` writes to a throwaway temp directory
        (not the persistent `<output_dir()>/<source>/` that `download` uses),
        so a load-and-plot run never leaves files in the working tree — there is
        no need to pass `path=tempfile.mkdtemp()` yourself. Each returned raster
        is detached into an in-memory copy and the temp directory is removed
        immediately, so repeated `load()` calls — e.g. a per-year loop — do not
        accumulate gigabytes under the system temp dir. (Because the data is
        copied into memory, a `path=`-less `load` of a very large raster reads it
        fully into RAM; pass `path=` to keep the lazy, file-backed objects.)

        `xarray` is intentionally not the return type: a returned
        `pyramids.Dataset` / `NetCDF` already exposes `.to_xarray()` for callers
        who want the climate-stack interop, so EarthLens stays free of an
        xarray dependency.

        Args:
            *args: Forwarded positionally to :meth:`download`.
            **kwargs: Forwarded as keywords to :meth:`download`.

        Returns:
            For a raster / mixed backend, a list with each written raster read
            into a `pyramids.Dataset` / `NetCDF` (non-raster entries left as
            their `Path`); for a vector / tabular backend, the in-memory
            `FeatureCollection` / `GeoDataFrame` / `DataFrame` `download`
            returned.

        Examples:
            - Load CHIRPS precipitation into pyramids `Dataset` objects
              (live; skipped here):
                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> rasters = EarthLens(  # doctest: +SKIP
                ...     "chc", variables=["precipitation"],
                ...     start="2020-01-01", end="2020-01-02", aoi=[-75, 4, -74, 5],
                ... ).load()
                >>> rasters[0].read_array()  # doctest: +SKIP

                ```
        """
        if self._explicit_path:
            return _load_result(self._dispatch_download(*args, **kwargs))
        self._redirect_output_to_tempdir()
        temp_dir = self.datasource.path
        result = _load_result(self._dispatch_download(*args, **kwargs))
        return _detach_and_cleanup(temp_dir, result)


def download(
    data_source: str = "chc",
    *,
    variables: dict[str, list[str]] | list[str] | None = None,
    dataset: str | None = None,
    start: str | datetime | date | None = None,
    end: str | datetime | date | None = None,
    path: Path | str | None = None,
    lat_lim: list[float] | None = None,
    lon_lim: list[float] | None = None,
    aoi: Any = None,
    buffer: float | None = None,
    temporal_resolution: str = "daily",
    cadence: str | None = None,
    time: Any = None,
    fmt: str = "%Y-%m-%d",
    progress_bar: bool = True,
    aggregate: AggregationConfig | None = None,
    load: bool = False,
    **backend_kwargs: object,
) -> Any:
    """Construct an :class:`EarthLens` and download in one call.

    The one-shot convenience for the common case: it forwards every
    request argument to :class:`EarthLens` and the run-time arguments to
    :meth:`EarthLens.download`, so `earthlens.core.download(...)` replaces the
    two-step construct-then-download.

    Args:
        data_source: Backend key (see `sorted(EarthLens.DataSources)`).
            Defaults to `"chc"`.
        variables: Variable specification, as for :class:`EarthLens`.
        dataset: Explicit dataset / collection key (see
            :class:`EarthLens`).
        start: Inclusive start date (string / `datetime` / `date`).
        end: Inclusive end date.
        time: A single time range (`"a/b"` string / `(a, b)` pair / `slice` /
            single date) — the ergonomic alternative to `start` / `end`;
            mutually exclusive with them.
        path: Output directory; defaults to
            `<output_dir()>/<data_source>/` when omitted — the directory
            configured by `set_output_dir()` / `EARTHLENS_DATA_DIR`.
        lat_lim: Legacy `[lat_min, lat_max]` pair — prefer `aoi=` (mutually
            exclusive with it).
        lon_lim: Legacy `[lon_min, lon_max]` pair — prefer `aoi=` (mutually
            exclusive with it).
        aoi: A single area-of-interest (bbox / point+`buffer` / geometry).
        buffer: Half-width in degrees for a point `aoi`.
        temporal_resolution: Backend cadence / label. Defaults to
            `"daily"`.
        cadence: Clearer alias for `temporal_resolution` (overrides it
            when given). Defaults to `None`.
        fmt: `strptime` format override for string dates.
        progress_bar: Whether the backend prints a progress bar.
        aggregate: Optional :class:`~earthlens.aggregate.AggregationConfig`.
        load: When `True`, return the data in memory via
            :meth:`EarthLens.load` (written rasters read into pyramids
            `Dataset` / `NetCDF` objects) instead of the written paths.
            Defaults to `False`.
        **backend_kwargs: Extra backend-specific options (see
            :meth:`EarthLens.options_for`).

    Returns:
        Whatever :meth:`EarthLens.download` returns for the backend, or —
        when `load=True` — the in-memory objects from :meth:`EarthLens.load`.

    Examples:
        - One-shot CHIRPS download. Marked `# doctest: +SKIP` because it
          makes a live FTP connection:
            ```python
            >>> import earthlens.core
            >>> earthlens.core.download(  # doctest: +SKIP
            ...     data_source="chc",
            ...     variables=["precipitation"],
            ...     start="2009-01-01", end="2009-01-02",
            ...     aoi=[-75.65, 4.19, -74.73, 4.64],
            ...     path="examples/data/chirps",
            ... )

            ```
    """
    facade = EarthLens(
        data_source=data_source,
        variables=variables,
        dataset=dataset,
        start=start,
        end=end,
        path=path,
        lat_lim=lat_lim,
        lon_lim=lon_lim,
        aoi=aoi,
        buffer=buffer,
        temporal_resolution=temporal_resolution,
        cadence=cadence,
        time=time,
        fmt=fmt,
        **backend_kwargs,
    )
    if load:
        return facade.load(progress_bar=progress_bar, aggregate=aggregate)
    return facade.download(progress_bar=progress_bar, aggregate=aggregate)


def sources() -> list[str]:
    """Return the sorted list of distinct backends, one canonical key each.

    The top-level discovery entry point — no class needed to see what
    backends `earthlens.core.download(...)` / :class:`EarthLens` accept. Alias and
    endpoint keys (`"chirps"` for `"chc"`, `"google-earth-engine"` for
    `"gee"`, the STAC endpoint keys `"planetary-computer"` / `"earth-search"`
    / `"cdse"`, …) still work as `data_source=` values but are collapsed to
    their canonical backend key here, so the list is one entry per backend.

    Returns:
        The canonical `data_source` key of each registered backend, sorted.

    Examples:
        - The CHIRPS and GEE backends are listed by their canonical keys:
            ```python
            >>> import earthlens.core
            >>> keys = earthlens.core.sources()
            >>> "chc" in keys and "gee" in keys
            True

            ```
        - Aliases are collapsed, so each backend appears once:
            ```python
            >>> import earthlens.core
            >>> keys = earthlens.core.sources()
            >>> "chirps" in keys or "google-earth-engine" in keys
            False

            ```
    """
    seen_modules: set[str] = set()
    canonical: list[str] = []
    for key, module, _extras in EarthLens.DataSources.entries():
        if module not in seen_modules:
            seen_modules.add(module)
            canonical.append(key)
    return sorted(canonical)


def search(
    data_source: str = "chc",
    *,
    variables: dict[str, list[str]] | list[str] | None = None,
    dataset: str | None = None,
    start: str | datetime | date | None = None,
    end: str | datetime | date | None = None,
    path: Path | str | None = None,
    lat_lim: list[float] | None = None,
    lon_lim: list[float] | None = None,
    aoi: Any = None,
    buffer: float | None = None,
    temporal_resolution: str = "daily",
    cadence: str | None = None,
    time: Any = None,
    fmt: str = "%Y-%m-%d",
    **backend_kwargs: object,
) -> list[RemoteProduct]:
    """Construct an :class:`EarthLens` and run a dry-run `search` in one call.

    The one-shot counterpart to :func:`download` for the search→fetch split:
    it forwards every request argument to :class:`EarthLens` and returns
    :meth:`EarthLens.search` — the products a download *would* fetch — without
    downloading anything.

    Args:
        data_source: Backend key (see :func:`sources`). Defaults to `"chc"`.
        variables: Variable specification, as for :class:`EarthLens`.
        dataset: Explicit dataset / collection key.
        start: Inclusive start date (string / `datetime` / `date`).
        end: Inclusive end date.
        time: A single time range (`"a/b"` string / `(a, b)` pair / `slice` /
            single date) — the ergonomic alternative to `start` / `end`;
            mutually exclusive with them.
        path: Output directory (unused by a dry-run search, but accepted for
            signature parity with :func:`download`).
        lat_lim: Legacy `[lat_min, lat_max]` pair — prefer `aoi=` (mutually
            exclusive with it).
        lon_lim: Legacy `[lon_min, lon_max]` pair — prefer `aoi=` (mutually
            exclusive with it).
        aoi: A single area-of-interest (bbox / point+`buffer` / geometry).
        buffer: Half-width in degrees for a point `aoi`.
        temporal_resolution: Backend cadence / label. Defaults to `"daily"`.
        cadence: Clearer alias for `temporal_resolution`.
        fmt: `strptime` format override for string dates.
        **backend_kwargs: Extra backend-specific options.

    Returns:
        One :class:`~earthlens.base.RemoteProduct` per matching item.

    Raises:
        NotImplementedError: If the backend exposes no searchable product
            list (see :meth:`EarthLens.search`).

    Examples:
        - Dry-run a STAC search and inspect the first product id (live;
          skipped here because it queries a remote catalog):
            ```python
            >>> import earthlens.core
            >>> products = earthlens.core.search(  # doctest: +SKIP
            ...     "stac",
            ...     dataset="sentinel-2-l2a",
            ...     variables=["red"],
            ...     start="2020-06-01", end="2020-06-30",
            ...     aoi=[-75, 4, -74, 5],
            ... )
            >>> products[0].id  # doctest: +SKIP

            ```
    """
    return EarthLens(
        data_source=data_source,
        variables=variables,
        dataset=dataset,
        start=start,
        end=end,
        path=path,
        lat_lim=lat_lim,
        lon_lim=lon_lim,
        aoi=aoi,
        buffer=buffer,
        temporal_resolution=temporal_resolution,
        cadence=cadence,
        time=time,
        fmt=fmt,
        **backend_kwargs,
    ).search()


def find(text: str) -> dict[str, list[str]]:
    """Find which sources expose a dataset matching `text`.

    A best-effort, cross-source discovery aid: it runs
    :meth:`EarthLens.guess_dataset` (case-insensitive substring, then fuzzy)
    against every registered source and collects the hits. A source whose SDK
    is not installed, or that has no free-text catalog, is skipped rather than
    failing the whole call — so the result covers the installed backends.

    Args:
        text: Free-text dataset query, e.g. `"precipitation"` or `"era5"`.

    Returns:
        A mapping `{data_source: [matching dataset key, ...]}` for every
        source with at least one match, in sorted-source order.

    Examples:
        - Find sources whose catalog mentions precipitation (live; skipped):
            ```python
            >>> import earthlens.core
            >>> earthlens.core.find("precipitation")  # doctest: +SKIP
            {'chc': ['global-daily', ...], ...}

            ```
    """
    matches: dict[str, list[str]] = {}
    for source in sources():
        try:
            hits = EarthLens.guess_dataset(source, text)
        except Exception as exc:  # noqa: BLE001 - skip uninstalled / catalog-less backends
            # Log (rather than silently drop) so a real `guess_dataset`
            # bug is traceable instead of just under-reporting.
            logger.debug(f"find(): skipping {source!r}: {type(exc).__name__}: {exc}")
            continue
        if hits:
            matches[source] = hits
    return matches
