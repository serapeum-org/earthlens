"""JAXA backend — dispatches a request to one of two protocols.

`JAXA` reaches JAXA's Earth-observation archive via two complementary
SDKs, selected per-dataset by the catalog's `protocol` discriminator:

* `protocol: jaxa-earth` — authless STAC/COG access through the official
  `jaxa.earth` API. The API returns in-memory numpy arrays which the
  backend writes to north-up GeoTIFFs via `pyramids.dataset.Dataset`.
* `protocol: gportal` — credentialed SFTP access through the community
  `gportal` SDK. The backend authenticates, searches, and downloads
  matching products into the output directory.

Each `download()` call routes every requested key to its protocol branch
(`_jaxa_earth.fetch_jaxa_earth` or `_gportal.fetch_gportal`). The two
branches share the request shape (bbox + dates + a list of dataset keys);
the catalog row carries the protocol-specific identifier, default band,
and aliases. Mixing keys from the two protocols in one call is rejected —
the two paths emit different file types (GeoTIFFs vs raw SFTP products)
and have different concurrency profiles, so the API forces one protocol
per call.

`OUTPUT_KIND = "raster"`; `download()` returns the list of written paths.
The `aggregate=` argument raises `NotImplementedError` — multi-date
windowed reductions are a follow-on enhancement once the array-stack
shape stabilises across the catalog (see `G6` in the planning doc).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

from earthlens.base import OutputKind
from earthlens.base.abstractdatasource import (
    AbstractDataSource,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.jaxa.auth import JaxaAuth, JaxaCredentials
from earthlens.jaxa.catalog import Catalog, Dataset, JaxaProtocol


class JAXA(AbstractDataSource):
    """Unified JAXA backend over two protocols (`jaxa-earth` and `gportal`).

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`. The `jaxa-earth` branch always emits
            GeoTIFFs; the `gportal` branch writes raw products (HDF5,
            GeoTIFF, NetCDF — mission-dependent) which downstream readers
            still treat as gridded artefacts.
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        *,
        resolution: float | None = None,
        bands: list[str] | None = None,
        gportal_username: str | None = None,
        gportal_password: str | None = None,
        catalog: Catalog | None = None,
    ):
        """Initialise a JAXA backend instance.

        Resolves every key against the catalog up front so that an unknown
        key (or a request that mixes the two protocols) fails at
        construction rather than mid-download.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`).
            end: Inclusive end of the date window.
            variables: Catalog dataset keys, canonical or alias. Every key
                must resolve to the **same** protocol — mixing
                `jaxa-earth` keys with `gportal` keys raises.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label only — JAXA Earth uses the
                full date range, G-Portal queries on `(start, end)`.
            path: Output directory (created if missing).
            fmt: `strptime` format for `start` / `end`.
            resolution: `ppu` (pixels per degree) for the `jaxa-earth`
                branch. `None` lets the API pick its native resolution.
                Ignored for `gportal`.
            bands: Override the catalog's `default_band` for the
                `jaxa-earth` branch. `None` uses each dataset's default
                band. Ignored for `gportal`.
            gportal_username: Explicit G-Portal username. When omitted,
                `JaxaAuth.configure("gportal")` reads `$GPORTAL_USERNAME`.
                Only used by the `gportal` branch.
            gportal_password: Explicit G-Portal password. When omitted,
                falls back to `$GPORTAL_PASSWORD`. Only used by the
                `gportal` branch.
            catalog: Optional pre-built `Catalog` (tests inject a faked
                one); defaults to the bundled catalog.

        Raises:
            ValueError: If `variables` is empty, an alias is unknown, or
                the resolved keys span both protocols.
        """
        if not variables:
            raise ValueError(
                "JAXA requires a non-empty `variables` list of dataset keys, "
                'e.g. ["aw3d30"] or ["elevation"].'
            )

        self._catalog = catalog if catalog is not None else Catalog()
        self._resolution = resolution
        self._bands_override = bands

        resolved: list[Dataset] = [self._catalog.get(v) for v in variables]
        protocols = {ds.protocol for ds in resolved}
        if len(protocols) > 1:
            jaxa_earth_keys = [ds.key for ds in resolved if ds.protocol == "jaxa-earth"]
            gportal_keys = [ds.key for ds in resolved if ds.protocol == "gportal"]
            raise ValueError(
                "one JAXA request must target a single protocol, but the "
                f"requested variables span both: jaxa-earth={jaxa_earth_keys}, "
                f"gportal={gportal_keys}. Issue two separate JAXA(...) calls."
            )
        self._resolved: list[Dataset] = resolved
        self._protocol: JaxaProtocol = next(iter(protocols))

        from pydantic import SecretStr

        creds = JaxaCredentials(
            gportal_username=gportal_username,
            gportal_password=SecretStr(gportal_password)
            if gportal_password is not None
            else None,
        )
        self._auth = JaxaAuth(creds)

        super().__init__(
            start=start,
            end=end,
            variables=variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    @property
    def auth(self) -> JaxaAuth:
        """The :class:`JaxaAuth` bound to this request.

        Returns:
            JaxaAuth: The optional-credentials auth object. `configure()`
            is a no-op for the `jaxa-earth` protocol and resolves
            G-Portal credentials for the `gportal` protocol.
        """
        return self._auth

    @property
    def protocol(self) -> JaxaProtocol:
        """The single protocol this request resolved to.

        Returns:
            JaxaProtocol: Either `"jaxa-earth"` or `"gportal"`.
        """
        return self._protocol

    def _initialize(self) -> None:
        """No eager connection setup.

        Both SDKs (`jaxa.earth`, `gportal`) are imported lazily in their
        branch modules, and `jaxa-earth` is authless. The optional
        `gportal` credentials are resolved lazily by `download()` (the
        first call to `JaxaAuth.configure("gportal")`).

        Returns:
            None: No backend client to attach.
        """
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a `SpatialExtent`.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox (WGS84).
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the date window into a `TemporalExtent`.

        The frequency is advisory: `jaxa-earth` consumes a `dlim` range
        directly, and `gportal.search` consumes `start_time` / `end_time`
        ISO strings.

        Args:
            start: Inclusive start of the window.
            end: Inclusive end of the window.
            temporal_resolution: Advisory label; defaults to `"daily"`.
            fmt: `strptime` format applied to `start` / `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        freq_alias = "D" if temporal_resolution == "daily" else "MS"
        dates = pd.date_range(start_dt, end_dt, freq=freq_alias)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=freq_alias,
            dates=dates,
        )

    def _api(self) -> list[Path]:
        """Dispatch to the matching protocol branch.

        Returns:
            list[Path]: One or more written paths per resolved dataset
                (a COG for `jaxa-earth`, an HDF5/GeoTIFF/NetCDF for
                `gportal`, depending on the mission).
        """
        out_dir = Path(self.path)
        written: list[Path] = []
        if self._protocol == "jaxa-earth":
            from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

            self._auth.configure("jaxa-earth")
            for ds in self._resolved:
                written.extend(
                    fetch_jaxa_earth(
                        dataset=ds,
                        space=self.space,
                        time=self.time,
                        resolution=self._resolution,
                        bands=self._bands_override,
                        out_dir=out_dir,
                    )
                )
        else:
            from earthlens.jaxa._gportal import fetch_gportal

            self._auth.configure("gportal")
            for ds in self._resolved:
                written.extend(
                    fetch_gportal(
                        dataset=ds,
                        space=self.space,
                        time=self.time,
                        out_dir=out_dir,
                    )
                )
        return written

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Fetch the requested datasets and return the written paths.

        Args:
            progress_bar: Reserved for future per-download progress; the
                two SDKs print their own progress lines today.
            aggregate: Not supported — JAXA requests return per-date
                rasters; reducing them across dates is a follow-on.

        Returns:
            list[Path]: One or more files per resolved dataset, in
                request order.

        Raises:
            NotImplementedError: When `aggregate` is non-`None`. See `G6`
                in the planning doc.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "JAXA does not yet support the aggregate= argument. "
                "The jaxa-earth branch returns per-date COGs; reducing "
                "them across dates is a planned follow-on (planning G6)."
            )
        del progress_bar
        return self._api()
