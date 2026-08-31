"""JAXA backend — dispatches a request to one of three protocols.

`JAXA` reaches JAXA's Earth-observation archive via three complementary
SDKs, selected per-dataset by the catalog's `protocol` discriminator:

* `protocol: jaxa-earth` — authless STAC/COG access through the official
  `jaxa.earth` API. The API returns in-memory numpy arrays which the
  backend writes to north-up GeoTIFFs via `pyramids.dataset.Dataset`.
* `protocol: gportal` — credentialed SFTP access through the community
  `gportal` SDK. The backend authenticates, searches, and downloads
  matching products into the output directory.
* `protocol: ptree` — credentialed FTP access to `ftp.ptree.jaxa.jp`'s
  Himawari-8/9 HSD granules (30-day rolling archive, 10 segments per
  band per 10-minute slot). Ships raw `.DAT.bz2` files; decode is
  `pyramids PY-2` (`satpy` reader bridge), not this backend.

Each `download()` call routes every requested key to its protocol branch
(`_jaxa_earth.fetch_jaxa_earth`, `_gportal.fetch_gportal`, or
`_ptree.fetch_ptree`). The three branches share the request shape
(bbox + dates + a list of dataset keys); the catalog row carries the
protocol-specific identifier, default band, and aliases. Mixing keys
from more than one protocol in one call is rejected — the three paths
emit different file types (GeoTIFFs vs raw SFTP products vs raw HSD
granules) and have different concurrency profiles, so the API forces
one protocol per call.

`OUTPUT_KIND = "raster"`; `download()` returns the list of written paths.
The `aggregate=` argument raises `NotImplementedError` — multi-date
windowed reductions are a follow-on enhancement once the array-stack
shape stabilises across the catalog (see `G6` in the planning doc).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from earthlens.base import OutputKind, date_windows, resolve_cadence, to_datetime
from earthlens.base.abstractdatasource import (
    AbstractDataSource,
    TemporalExtent,
)
from earthlens.jaxa.auth import JaxaAuth, JaxaCredentials
from earthlens.jaxa.catalog import Catalog, Dataset, JaxaProtocol

#: Maps an `EarthLens(temporal_resolution=...)` value to the pandas
#: frequency alias used for `self.time.dates`. JAXA's three branches
#: only read `start_date` / `end_date` directly (jaxa-earth via `dlim`,
#: gportal via `start_time` / `end_time`, ptree via a slot iterator),
#: so the `dates` index is informational — but it should still report
#: a frequency that matches the cadence the user asked for instead of
#: silently snapping every non-`daily` value to month-start.
#: JAXA keeps its own cadence map rather than `earthlens.base.CADENCE_ALIASES`
#: because it is the one adopter that actually iterates `self.time.dates`, and
#: because its `raw` means "the native granule cadence, walked daily" — not the
#: shared vocabulary's "no temporal aggregation, query the window whole". Adding
#: a cadence here is deliberate, not an omission from the shared map.
_FREQ_ALIAS: dict[str, str] = {
    "raw": "D",
    "hourly": "h",
    "daily": "D",
    "monthly": "MS",
    "yearly": "YS",
}


class JAXA(AbstractDataSource):
    """Unified JAXA backend over three protocols (`jaxa-earth`, `gportal`, `ptree`).

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`. The `jaxa-earth` branch always emits
            GeoTIFFs; the `gportal` branch writes raw products (HDF5,
            GeoTIFF, NetCDF — mission-dependent); the `ptree` branch
            writes raw Himawari HSD `.DAT.bz2` segments (10 per band per
            10-minute slot) — all three downstream readers still treat
            the output as gridded artefacts.
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = "JAXA does not yet support the aggregate= argument. All three branches emit per-date artefacts today (jaxa-earth: per-date COGs; gportal: per-product SFTP downloads; ptree: per-slot HSD segments) — reducing them across dates is a planned follow-on (planning G6)"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        resolution: float | None = None,
        bands: list[str] | None = None,
        gportal_username: str | None = None,
        gportal_password: str | None = None,
        ptree_username: str | None = None,
        ptree_password: str | None = None,
        catalog: Catalog | None = None,
    ):
        """Initialise a JAXA backend instance.

        Resolves every key against the catalog up front so that an unknown
        key (or a request that mixes more than one of the three protocols)
        fails at construction rather than mid-download.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`).
            end: Inclusive end of the date window.
            variables: Catalog dataset keys, canonical or alias. Every key
                must resolve to the **same** protocol — mixing keys from
                more than one of `jaxa-earth`, `gportal`, and `ptree`
                raises with a diagnostic message that names each violated
                protocol and its offending keys.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label only — JAXA Earth uses
                the full date range, G-Portal queries on
                `(start, end)`, and `_ptree.fetch_ptree` iterates every
                10-minute slot in the window.
            path: Output directory (created if missing).
            fmt: `strptime` format for `start` / `end`.
            resolution: `ppu` (pixels per degree) for the `jaxa-earth`
                branch. `None` lets the API pick its native resolution.
                Ignored for `gportal` and `ptree`.
            bands: Override the catalog's `default_band`. For
                `jaxa-earth`, picks the band(s) whose GeoTIFF the
                backend writes. For `ptree`, selects which Himawari
                AHI band(s) to download from the full 10-segment set
                — `"B01"`..`"B16"` are valid, with `"B03"` (0.5 km
                visible) and `"B13"` (2 km IR) as the two common
                defaults. `None` uses the row's `default_band`.
                Ignored for `gportal` (products are downloaded whole).
            gportal_username: Explicit G-Portal username. When omitted,
                `JaxaAuth.configure()` reads `$GPORTAL_USERNAME` at
                authentication time. Only used by the `gportal` branch.
            gportal_password: Explicit G-Portal password. When omitted,
                falls back to `$GPORTAL_PASSWORD`. Only used by the
                `gportal` branch.
            ptree_username: Explicit P-Tree username (the email
                registered at eorc.jaxa.jp/ptree). When omitted, falls
                back to `$JAXA_PTREE_USERNAME`. Only used by the
                `ptree` branch. Never reuse the G-Portal credentials —
                the two accounts are distinct.
            ptree_password: Explicit P-Tree password. When omitted,
                falls back to `$JAXA_PTREE_PASSWORD`. Only used by the
                `ptree` branch.
            catalog: Optional pre-built `Catalog` (tests inject a faked
                one); defaults to the bundled catalog.

        Raises:
            ValueError: If `variables` is empty, an alias is unknown, or
                the resolved keys span more than one protocol.
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
            groups = {
                p: [ds.key for ds in resolved if ds.protocol == p]
                for p in sorted(protocols)
            }
            summary = ", ".join(f"{p}={ks}" for p, ks in groups.items())
            raise ValueError(
                "one JAXA request must target a single protocol, but the "
                f"requested variables span multiple: {summary}. "
                "Issue one JAXA(...) call per protocol."
            )
        self._resolved: list[Dataset] = resolved
        self._protocol: JaxaProtocol = next(iter(protocols))

        creds = JaxaCredentials(
            gportal_username=gportal_username,
            gportal_password=SecretStr(gportal_password)
            if gportal_password is not None
            else None,
            ptree_username=ptree_username,
            ptree_password=SecretStr(ptree_password)
            if ptree_password is not None
            else None,
        )
        # Bind the protocol so `AbstractDataSource.authenticate()` (which
        # calls `self._auth.configure()` with no args) fails fast on
        # missing credentials for whichever credentialed branch this
        # request pins (`gportal` or `ptree`); jaxa-earth is authless.
        self._auth = JaxaAuth(creds, protocol=self._protocol)

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
            is a no-op for the `jaxa-earth` protocol, resolves
            G-Portal credentials for `gportal`, and resolves P-Tree
            credentials for `ptree`.

        Examples:
            - The auth object's protocol always matches the backend's:
                ```python
                >>> import tempfile
                >>> from earthlens.jaxa import JAXA
                >>> backend = JAXA(
                ...     start="2020-01-01", end="2020-12-31",
                ...     variables=["aw3d30"],
                ...     lat_lim=[35.0, 36.0], lon_lim=[138.0, 139.0],
                ...     path=tempfile.gettempdir(),
                ... )
                >>> backend.auth.protocol
                'jaxa-earth'

                ```
        """
        return self._auth

    @property
    def protocol(self) -> JaxaProtocol:
        """The single protocol this request resolved to.

        Returns:
            JaxaProtocol: One of `"jaxa-earth"`, `"gportal"`, or
                `"ptree"`.

        Examples:
            - Resolving an alias pins the protocol on construction:
                ```python
                >>> import tempfile
                >>> from earthlens.jaxa import JAXA
                >>> JAXA(
                ...     start="2020-01-01", end="2020-12-31",
                ...     variables=["elevation"],
                ...     lat_lim=[35.0, 36.0], lon_lim=[138.0, 139.0],
                ...     path=tempfile.gettempdir(),
                ... ).protocol
                'jaxa-earth'

                ```
        """
        return self._protocol

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the date window into a `TemporalExtent`.

        The frequency is advisory: `jaxa-earth` consumes a `dlim` range
        directly, `gportal.search` consumes `start_time` / `end_time`
        ISO strings, and `_ptree.fetch_ptree` iterates every 10-minute
        slot in the window.

        Args:
            start: Inclusive start of the window.
            end: Inclusive end of the window.
            temporal_resolution: Advisory label; defaults to `"daily"`.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        freq_alias = resolve_cadence(
            temporal_resolution, _FREQ_ALIAS, backend=type(self).__name__
        )
        dates = date_windows(start_dt, end_dt, freq_alias)
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
                — a COG for `jaxa-earth`, an HDF5 / GeoTIFF / NetCDF
                product for `gportal` (depending on the mission), or
                the 10 raw `.DAT.bz2` HSD segments per band per
                10-minute slot for `ptree`.
        """
        out_dir = Path(self.path)
        self._auth.configure()
        written: list[Path] = []
        if self._protocol == "jaxa-earth":
            from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

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
        elif self._protocol == "gportal":
            from earthlens.jaxa._gportal import fetch_gportal

            for ds in self._resolved:
                written.extend(
                    fetch_gportal(
                        dataset=ds,
                        space=self.space,
                        time=self.time,
                        auth=self._auth,
                        out_dir=out_dir,
                    )
                )
        elif self._protocol == "ptree":
            from earthlens.jaxa._ptree import fetch_ptree

            for ds in self._resolved:
                written.extend(
                    fetch_ptree(
                        dataset=ds,
                        space=self.space,
                        time=self.time,
                        auth=self._auth,
                        out_dir=out_dir,
                        bands=self._bands_override,
                    )
                )
        else:
            # Defensive: `JaxaProtocol` is a `Literal[...]` and the
            # constructor rejects anything else, so this only fires if a
            # future fourth protocol is added to the literal but not to
            # `_api`. Failing loud beats silently routing to the last
            # `else` branch (Round 2 L3).
            raise ValueError(
                f"unhandled JAXA protocol {self._protocol!r}; add a "
                "dispatch branch alongside jaxa-earth/gportal/ptree."
            )
        return written

    def download(
        self,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Fetch the requested datasets and return the written paths.

        Args:
            progress_bar: Reserved for future per-download progress;
                each branch prints its own progress lines today
                (jaxa-earth via `jaxa.earth`, gportal via the `gportal`
                SDK, ptree via one segment-path write per FTP transfer).

        Returns:
            list[Path]: One or more files per resolved dataset, in
                request order.

        Examples:
            - Passing `aggregate=` raises because per-date stacks are not
              reduced yet:
                ```python
                >>> import tempfile
                >>> from earthlens.jaxa import JAXA
                >>> backend = JAXA(
                ...     start="2020-01-01", end="2020-12-31",
                ...     variables=["aw3d30"],
                ...     lat_lim=[35.0, 36.0], lon_lim=[138.0, 139.0],
                ...     path=tempfile.gettempdir(),
                ... )
                >>> backend.download(aggregate=object())  # doctest: +ELLIPSIS
                Traceback (most recent call last):
                    ...
                NotImplementedError: aggregate= is not supported by JAXA...

                ```
        """
        del progress_bar
        return self._api()
