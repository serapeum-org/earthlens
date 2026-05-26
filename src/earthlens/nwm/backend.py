"""Backend that fetches NOAA National Water Model output from S3.

`NWM(AbstractDataSource)` pulls National Water Model NetCDF output from
the **unsigned** `noaa-nwm-pds` AWS bucket. NWM is NOAA's operational
hydrologic model: it routes the land-surface water budget onto the
NHDPlus river network, producing per-reach streamflow (`channel_rt`,
indexed by `feature_id` — **not** a lat/lon grid) plus gridded land
(`land`), reservoir, and routing (`terrain_rt`) products.

The request mirrors the NWP backend's forecast axis: a configuration
(`short_range`, `medium_range_mem1`, …) runs on a set of UTC `cycles`
and publishes forecast `steps` (`fNNN`). `variables = {config: [product,
…]}` selects which products to pull. Because each configuration's S3
file names are irregular (the ensemble member can ride on the product
token — `channel_rt_1` — regional domains use 5-digit sub-hourly steps,
analyses use `tmNN`), every :class:`~earthlens.nwm.catalog.NWMConfig`
carries a full `key_template` and the backend just formats it.

NWM output is native NetCDF; this backend **fetches** the files and
returns a `pandas.DataFrame` inventory (so `OUTPUT_KIND = "tabular"`,
and the facade rejects `aggregate=`). Decoding `channel_rt` streamflow
into a tidy `feature_id × time` table (via `xarray`) is a downstream
follow-on.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.nwm.catalog import NWMCatalog, NWMConfig

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

#: The unsigned AWS bucket holding NWM operational output.
BUCKET = "noaa-nwm-pds"


def _s3_client(region: str) -> Any:
    """Build an unsigned `boto3` S3 client for the public NWM bucket.

    Args:
        region: AWS region of the bucket (`"us-east-1"`).

    Returns:
        An anonymous `boto3` S3 client.

    Raises:
        ImportError: When `boto3` is not installed (names `earthlens[nwm]`).
    """
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config
    except ImportError as exc:
        raise ImportError(
            "the National Water Model backend needs `boto3`; install "
            "`pip install earthlens[nwm]`."
        ) from exc
    return boto3.client(
        "s3", region_name=region, config=Config(signature_version=UNSIGNED)
    )


def enumerate_cycles(
    start: dt.datetime, end: dt.datetime, cycles_utc: list[int]
) -> list[dt.datetime]:
    """Enumerate the model cycles in `[start, end]` for the given run hours.

    Walks every calendar day from `start` to `end` inclusive and emits one
    naive-UTC datetime per run hour on that day, ascending.

    Args:
        start: Inclusive start of the cycle-date range (only its date is used).
        end: Inclusive end of the cycle-date range.
        cycles_utc: Daily run hours, in `[0, 23]`.

    Returns:
        list[datetime.datetime]: One datetime per `(day, run-hour)`, ascending.

    Raises:
        ValueError: If `start` is later than `end`, or a run hour is out of range.

    Examples:
        - Two cycles across one day:
            ```python
            >>> import datetime as dt
            >>> from earthlens.nwm.backend import enumerate_cycles
            >>> day = dt.datetime(2026, 1, 1)
            >>> [c.hour for c in enumerate_cycles(day, day, [0, 12])]
            [0, 12]

            ```
    """
    if start.date() > end.date():
        raise ValueError(f"start {start.date()} is after end {end.date()}.")
    bad = [h for h in cycles_utc if not 0 <= h <= 23]
    if bad:
        raise ValueError(f"run hour(s) {bad} are outside [0, 23].")
    out: list[dt.datetime] = []
    day = start.date()
    while day <= end.date():
        for hour in sorted(cycles_utc):
            out.append(dt.datetime(day.year, day.month, day.day, hour))
        day += dt.timedelta(days=1)
    return out


class NWM(AbstractDataSource):
    """NOAA National Water Model backend (operational NetCDF output).

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is a `pandas.DataFrame`
            inventory of fetched NetCDF files, so the facade rejects
            `aggregate=` (NWM `channel_rt` is feature-id indexed, not a
            griddable raster).
    """

    OUTPUT_KIND: OutputKind = "tabular"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "raw",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        *,
        cycles: list[int] | None = None,
        steps: list[int] | None = None,
        horizon: int | None = None,
        region: str = "us-east-1",
        catalog: NWMCatalog | None = None,
    ):
        """Initialise a National Water Model backend instance.

        Args:
            start: Inclusive start of the cycle-date range (parsed with `fmt`).
            end: Inclusive end of the cycle-date range.
            variables: Mapping from NWM configuration key to the product
                tokens to fetch, e.g. `{"short_range": ["channel_rt"]}`.
                An empty list selects all of the configuration's products.
            lat_lim: `[lat_min, lat_max]` in degrees (informational — NWM
                files are continental and not server-side croppable here).
            lon_lim: `[lon_min, lon_max]` in degrees (informational).
            temporal_resolution: Advisory label.
            path: Output directory for the fetched NetCDF files.
            fmt: `strptime` format for `start` / `end` (default date-only).
            cycles: Restrict the run hours fetched (subset of the config's
                `cycles_utc`); defaults to every cycle the config runs.
            steps: Explicit forecast steps (`fNNN`) to fetch; wins over
                `horizon`.
            horizon: Maximum forecast step; expands from the config's
                `first_step` on its `step_cadence_h`.
            region: AWS region of the bucket.
            catalog: Optional pre-built :class:`NWMCatalog` (tests inject one).

        Raises:
            ValueError: When `variables` is empty.
        """
        if not variables:
            raise ValueError(
                "NWM requires a non-empty `variables` mapping of "
                "{configuration: [product, ...]}."
            )
        self._region = region
        self._cycles_arg = cycles
        self._steps_arg = steps
        self._horizon_arg = horizon
        self._catalog = catalog if catalog is not None else NWMCatalog()
        self._requests: list[tuple[str, NWMConfig, list[str]]] = []
        for cfg_key, products in variables.items():
            config = self._catalog.get_config(cfg_key)
            chosen = list(products) if products else list(config.products)
            unknown = [p for p in chosen if p not in config.products]
            if unknown:
                raise ValueError(
                    f"product(s) {unknown} are not in configuration {cfg_key!r}; "
                    f"available: {config.products}."
                )
            self._requests.append((cfg_key, config, chosen))
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

    def _initialize(self):
        """No-op auth hook — the NWM bucket is anonymous. Returns `None`."""
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a :class:`SpatialExtent`."""
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the cycle-date window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive window start.
            end: Inclusive window end.
            temporal_resolution: Advisory cadence label.
            fmt: `strptime` format applied to `start` / `end`.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="raw",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` (returns the fetched paths)."""
        return self._api_via_search_fetch()

    def _cycles_for(self, config: NWMConfig) -> list[int]:
        """Resolve the run hours to fetch for one configuration.

        Args:
            config: The resolved catalog row.

        Returns:
            list[int]: The requested run hours (validated against the config).

        Raises:
            ValueError: When a requested cycle is not one the config runs.
        """
        if self._cycles_arg is None:
            return list(config.cycles_utc)
        unknown = [c for c in self._cycles_arg if c not in config.cycles_utc]
        if unknown:
            raise ValueError(
                f"cycle(s) {unknown} are not run by this configuration "
                f"{config.cycles_utc}."
            )
        return sorted(set(self._cycles_arg))

    def _steps_for(self, config: NWMConfig) -> list[int]:
        """Resolve the forecast steps to fetch for one configuration.

        Precedence: an explicit `steps=` list wins; otherwise `horizon=`
        expands from the config's `first_step` to the horizon on its
        `step_cadence_h`; otherwise just the `first_step`.

        Args:
            config: The resolved catalog row.

        Returns:
            list[int]: The forecast steps to fetch, ascending.

        Raises:
            ValueError: When a requested step exceeds the config's horizon.
        """
        if self._steps_arg is not None:
            steps = sorted({int(s) for s in self._steps_arg})
        elif self._horizon_arg is not None:
            steps = list(
                range(
                    config.first_step,
                    int(self._horizon_arg) + 1,
                    max(config.step_cadence_h, 1),
                )
            )
        else:
            steps = [config.first_step]
        too_far = [s for s in steps if s > config.horizon_h]
        if too_far:
            raise ValueError(
                f"step(s) {too_far} exceed the {config.horizon_h} h horizon."
            )
        return steps

    def _search(self) -> list[RemoteProduct]:
        """Expand the request into one product per `(config, cycle, step, product)`.

        For each requested configuration, crosses every in-window cycle
        with every requested step and product, formatting the config's
        `key_template` into the exact S3 key. The key rides on the
        product metadata so :meth:`_fetch` needs no re-listing.

        Returns:
            list[RemoteProduct]: One product per
                `(config, cycle, step, product)`, each carrying `href`
                (the S3 key) and `config_key` / `cycle` / `step` /
                `product` / `domain` metadata.
        """
        products: list[RemoteProduct] = []
        for cfg_key, config, prods in self._requests:
            cycles = enumerate_cycles(
                self.time.start_date, self.time.end_date, self._cycles_for(config)
            )
            for cycle in cycles:
                for step in self._steps_for(config):
                    for product in prods:
                        key = config.key_template.format(
                            date=cycle, cycle=cycle, step=step, product=product
                        )
                        products.append(
                            RemoteProduct(
                                id=f"{cfg_key}.{cycle:%Y%m%d%H}.f{step:03d}.{product}",
                                href=key,
                                metadata={
                                    "config_key": cfg_key,
                                    "cycle": cycle,
                                    "step": step,
                                    "product": product,
                                    "domain": config.domain,
                                },
                            )
                        )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download each product's NetCDF file into the output directory.

        Per product: GET the S3 key and write it (atomically, via a
        `.part` rename) under its bucket-relative basename. A
        `(cycle, step)` that is not yet published — or a product a
        configuration does not carry on that cycle — is logged and
        skipped, so one miss does not lose the rest (mirrors the
        radar / NWP policy).

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: One fetched NetCDF path per successful product,
                in order. Shorter than `products` when some were skipped.
        """
        client = _s3_client(self._region)
        out: list[Path] = []
        for product in products:
            try:
                out.append(self._fetch_one(client, product))
            except Exception as exc:  # noqa: BLE001 - skip the miss, keep going
                logger.warning(
                    f"nwm: skipping {product.id} — fetch failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        return out

    def _fetch_one(self, client: Any, product: RemoteProduct) -> Path:
        """Download one product's NetCDF file (atomic `.part` rename)."""
        key = product.href
        target = self.root_dir / Path(key).name
        tmp = target.with_name(target.name + ".part")
        try:
            body = client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            with open(tmp, "wb") as handle:
                handle.write(body)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ):
        """Fetch the requested NWM files and return a `DataFrame` inventory.

        Args:
            progress_bar: Unused (kept for interface parity).
            aggregate: Must be `None` — NWM `channel_rt` is feature-id
                indexed, not a griddable raster. The facade already
                rejects a non-`None` `aggregate=` for a `"tabular"`
                backend before reaching here.

        Returns:
            pandas.DataFrame: One row per fetched file — `config`,
                `cycle`, `step`, `valid_time`, `product`, `domain`, and
                local `path`. Empty (with the right columns) when nothing
                in the window was available.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "NWM.download(aggregate=...) is not supported — NWM channel_rt "
                "is feature-id indexed, not a griddable raster."
            )
        products = self._search()
        paths = self._fetch(products)
        return self._inventory(products, paths)

    @staticmethod
    def _inventory(products: list[RemoteProduct], paths: list[Path]) -> pd.DataFrame:
        """Build the DataFrame inventory from products + fetched paths."""
        columns = ["config", "cycle", "step", "valid_time", "product", "domain", "path"]
        by_name = {Path(p.href).name: p for p in products}
        rows = []
        for path in paths:
            product = by_name.get(path.name)
            meta = product.metadata if product else {}
            cycle = meta.get("cycle")
            step = meta.get("step")
            valid = (
                cycle + dt.timedelta(hours=step)
                if cycle is not None and step is not None
                else None
            )
            rows.append(
                {
                    "config": meta.get("config_key"),
                    "cycle": cycle,
                    "step": step,
                    "valid_time": valid,
                    "product": meta.get("product"),
                    "domain": meta.get("domain"),
                    "path": str(path),
                }
            )
        return pd.DataFrame(rows, columns=columns)
