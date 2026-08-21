"""Backend that fetches Argo float profiles via the `argopy` SDK.

`ARGO(AbstractDataSource)` wraps the official **`argopy`** SDK to pull
autonomous-float ocean profiles — temperature, salinity, pressure, and
(for BGC floats) biogeochemical parameters — from the global Argo array
of ~4000 active floats. A request is either a bbox + time window over a
set of parameter names (a **region** selection) or a `float:` / `profile:`
selector; the backend returns a long-format :class:`pandas.DataFrame`, so
`OUTPUT_KIND = "tabular"` and the :class:`earthlens.earthlens.EarthLens`
facade rejects an `aggregate=` argument (gridded ocean fields are the
CMEMS path).

The selection mode is routed from `variables` (see
:func:`earthlens.argo._helpers.parse_selection`):

* a parameter list (`["TEMP", "PSAL"]`, or an empty list) → `.region(...)`
  over the request bbox + time + depth range;
* `"float:6902746"` (or `"float:6902746,6902747"`) → `.float([...])`;
* `"profile:6902746/12"` → `.profile(6902746, 12)`.

The dataset family is chosen with `dataset=` (`"phy"` default core
physical, or `"bgc"` biogeochemical) → `argopy` `ds=`; the data backend
with `source=` (`"erddap"` default / `"gdac"` / `"argovis"`) → `src=`; the
QC mode with `mode=` (`"standard"` default / `"expert"` / `"research"`);
and the depth envelope with `depth=(min, max)` dbar (default `0–2000`).

Argo is open data — there is no authentication. On a successful fetch the
backend logs the standard Argo data-acknowledgement statement
(:data:`earthlens.argo._helpers.ARGO_ACKNOWLEDGEMENT`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd
from loguru import logger

from earthlens.argo import _helpers
from earthlens.argo._helpers import (
    ARGO_ACKNOWLEDGEMENT,
    ARGO_COLUMNS,
    Selection,
    empty_canonical,
    region_box,
)
from earthlens.argo.catalog import Catalog
from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)

OutputFormat = Literal["csv", "parquet"]

#: Accepted `argopy` data backends (`src=`).
SOURCES: tuple[str, ...] = ("erddap", "gdac", "argovis")

#: Accepted `argopy` dataset families (`ds=`).
DATASETS: tuple[str, ...] = ("phy", "bgc")

#: Accepted `argopy` QC modes (`mode=`).
MODES: tuple[str, ...] = ("standard", "expert", "research")

#: Accepted on-disk output formats.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Default depth envelope (dbar) — the standard core-Argo profiling range.
_DEFAULT_DEPTH: tuple[float, float] = (0.0, 2000.0)


def _import_argopy():
    """Import the `argopy` SDK lazily with a friendly error.

    Keeps `import earthlens.argo` working without the optional `[argo]`
    extra: the SDK is only needed at `download()` time.

    Returns:
        The imported `argopy` top-level module.

    Raises:
        ImportError: When `argopy` is not installed; the message names
            the `earthlens[argo]` extra to install.
    """
    try:
        import argopy  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised via fakes
        raise ImportError(
            "The Argo backend requires the 'argopy' SDK. "
            "Install it with: pip install earthlens[argo]"
        ) from exc
    return argopy


def _no_data_errors() -> tuple[type[BaseException], ...]:
    """Return the exception types that mean "the region matched no floats".

    `argopy` raises rather than returning an empty frame: an ERDDAP 404
    surfaces as a `FileNotFoundError`, and the SDK has its own
    `DataNotFound` / `NoData` / `ErddapHTTPNotFound` for adjacent no-data
    cases (verified in `A1`). Resolved lazily so the tuple is built only
    when `argopy` is importable.

    Returns:
        tuple[type[BaseException], ...]: The catchable no-data errors.
    """
    errors: list[type[BaseException]] = [FileNotFoundError]
    try:
        import argopy.errors as ae
    except ImportError:  # pragma: no cover - argopy always present at fetch
        return tuple(errors)
    for name in ("DataNotFound", "NoData", "ErddapHTTPNotFound"):
        candidate = getattr(ae, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            errors.append(candidate)
    return tuple(errors)


class ARGO(AbstractDataSource):
    """Argo float-profile backend (long-format tabular output).

    Fetches autonomous-float ocean profiles for a bbox / float / profile
    selection through the same `download()` shape every other earthlens
    backend uses, and returns a long-format :class:`pandas.DataFrame`
    (one row per measured level). The query is a search/fetch split:
    :meth:`_search` resolves the single `argopy` request and :meth:`_fetch`
    realises it to a frame.

    Attributes:
        OUTPUT_KIND: `"tabular"` — Argo profiles are irregular point
            data, so the facade rejects `aggregate=` with
            `NotImplementedError` (use CMEMS for gridded ocean fields).
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "argo float profiles are irregular tabular point data, not a gridded field, so there is no meaningful gridded reduction. Use the CMEMS backend for gridded ocean fields instead"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "profile",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        source: str = "erddap",
        dataset: str = "phy",
        mode: str = "standard",
        depth: tuple[float, float] = _DEFAULT_DEPTH,
        output_format: OutputFormat = "csv",
    ):
        """Initialise an Argo backend instance.

        Args:
            start: Inclusive start of the window, parsed with `fmt`.
            end: Inclusive end of the window.
            variables: Either Argo parameter names (`["TEMP", "PSAL"]`)
                for a region selection, or a single `"float:<WMO>"` /
                `"profile:<WMO>/<cycle>"` selector token. An empty list
                is a region selection. For a region selection the names
                are **validated** against the chosen `dataset` family but
                do **not** subset the result: `argopy` returns the whole
                family (for `"phy"`: `PRES`/`TEMP`/`PSAL` plus their
                `*_ERROR`/`*_QC`), so naming a parameter asserts intent
                rather than filtering columns.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes (used by
                a region selection; ignored for float / profile).
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes.
            temporal_resolution: Resolution label recorded on the
                temporal extent (Argo cycles are ~10-daily; the default
                `"profile"` is descriptive only).
            path: Output directory for the written table.
            fmt: `strptime` format for `start` / `end`.
            source: `argopy` data backend (`src=`) — `"erddap"`
                (default), `"gdac"`, or `"argovis"`.
            dataset: `argopy` dataset family (`ds=`) — `"phy"` (default,
                core T/S/P) or `"bgc"` (biogeochemical).
            mode: `argopy` QC mode (`mode=`) — `"standard"` (default),
                `"expert"`, or `"research"`.
            depth: `(min, max)` depth envelope in dbar for a region
                selection. Defaults to `(0, 2000)`.
            output_format: On-disk format — `"csv"` (default) or
                `"parquet"`.

        Raises:
            ValueError: When `source`, `dataset`, `mode`, or
                `output_format` is not a recognised value, or when a
                region selection's parameter names are not in the chosen
                family (with a did-you-mean hint).
            TypeError: When `variables` is a mapping (this backend takes
                a flat list of parameter names or one selector token).
        """
        if isinstance(variables, dict):
            raise TypeError(
                "ARGO `variables` must be a list of Argo parameter names "
                "(e.g. ['TEMP', 'PSAL']) or a single selector token "
                "('float:6902746' / 'profile:6902746/12'), not a mapping. "
                "Query knobs are explicit ARGO(...) keyword arguments "
                "(source=, dataset=, mode=, depth=)."
            )
        if source not in SOURCES:
            raise ValueError(f"source must be one of {list(SOURCES)}, got {source!r}.")
        if dataset not in DATASETS:
            raise ValueError(
                f"dataset must be one of {list(DATASETS)}, got {dataset!r}."
            )
        if mode not in MODES:
            raise ValueError(f"mode must be one of {list(MODES)}, got {mode!r}.")
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )

        self._catalog = Catalog()
        self._selection: Selection = _helpers.parse_selection(list(variables))
        # A region selection names parameters; validate them against the
        # chosen family. A float / profile selector carries no parameter
        # names, so there is nothing to validate.
        if self._selection.kind == "region" and variables:
            self._catalog.validate_parameters(list(variables), dataset)

        self._source = source
        self._dataset = dataset
        self._mode = mode
        self._depth = (float(depth[0]), float(depth[1]))
        self._output_format: OutputFormat = output_format
        self._acknowledged = False
        super().__init__(
            start=start,
            end=end,
            variables=list(variables),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        Argo `.region()` takes the whole window as ISO date bounds in one
        call, so there is no per-date loop; `dates` collapses to the two
        endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        return self._whole_window_extent(
            start, end, fmt=fmt, resolution=temporal_resolution
        )

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Fetch the profiles, write the table, and return it.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends. Argo issues one bulk `argopy` call, so there is
                no per-item progress bar — this is a no-op.
            limit: Cap on the total rows returned. **Trims, it does not
                reduce the fetch**: argopy answers a whole selection in one
                call, so `_search` plans a single product and there is no
                later item for the cap to skip. Use it to bound what you get
                back, and narrow `start` / `end` / the bbox to bound what is
                transferred. `None` (the default) returns everything.

        Returns:
            pd.DataFrame: The long-format profile table.
        """
        self._limit = self.check_limit(limit)
        frames = self._api()
        df = (
            pd.concat(frames, ignore_index=True)
            if frames
            else empty_canonical(ARGO_COLUMNS)
        )
        out_path = self._write_table(df)
        if len(df):
            logger.info(f"ARGO: {len(df)} row(s) written to {out_path}")
            # Argo asks users to cite the program; log the acknowledgement
            # once per backend instance rather than on every download() call.
            if not self._acknowledged:
                logger.info(ARGO_ACKNOWLEDGEMENT)
                self._acknowledged = True
        else:
            logger.warning(
                f"ARGO: no profiles matched the request; wrote an empty "
                f"(schema-only) table to {out_path}"
            )
        return df

    def _search(self) -> list[RemoteProduct]:
        """Resolve the single `argopy` request as one product.

        Returns:
            list[RemoteProduct]: One product whose `metadata` carries the
                parsed selection and the resolved `source` / `dataset` /
                `mode` knobs.
        """
        return [
            RemoteProduct(
                id="argo",
                metadata={
                    "selection": self._selection,
                    "source": self._source,
                    "dataset": self._dataset,
                    "mode": self._mode,
                },
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[pd.DataFrame]:
        """Realise each product to a long-format frame.

        Widens the inherited `-> list[Path]` contract: a tabular backend
        returns in-memory frames, not file paths (the write happens in
        :meth:`download` via :meth:`_write_table`).

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[pd.DataFrame]: One frame per product, same order.
        """
        return self._fetch_limited(products, self._limit)

    def _fetch_one(self, product: RemoteProduct) -> pd.DataFrame:
        """Fetch one product's profiles as a long-format frame.

        Builds an `argopy.DataFetcher`, dispatches on the selection kind
        (`region` / `float` / `profile`), and realises it through the
        **pandas** accessor (`.to_dataframe()` — never the xarray one).
        An empty result folds to the canonical zero-row frame — `argopy`
        signals "no floats" two ways (verified in `A1`): an erddap fetch
        **raises**, while a gdac fetch can return an already-empty frame;
        both are normalised here.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            pd.DataFrame: The realised profile frame, or the canonical
                empty frame when the request matched no floats.
        """
        argopy = _import_argopy()
        selection: Selection = product.metadata["selection"]
        fetcher = argopy.DataFetcher(
            src=product.metadata["source"],
            ds=product.metadata["dataset"],
            mode=product.metadata["mode"],
        )
        try:
            access = self._apply_selection(fetcher, selection)
            df = access.to_dataframe()
        except _no_data_errors() as exc:
            # `FileNotFoundError` is in the no-data tuple because an empty
            # erddap region surfaces as one, but argopy/fsspec can also raise
            # it for a genuinely missing artefact — log the caught exception so
            # a mis-classified failure is diagnosable rather than invisible.
            logger.warning(
                f"ARGO: argopy returned no data for the request "
                f"({type(exc).__name__}: {exc}); returning an empty frame."
            )
            return empty_canonical(ARGO_COLUMNS)
        if df is None or df.empty:
            return empty_canonical(ARGO_COLUMNS)
        return df.reset_index(drop=True)

    def _apply_selection(self, fetcher, selection: Selection):
        """Apply the parsed selection to an `argopy` fetcher.

        Args:
            fetcher: An `argopy.DataFetcher`.
            selection: The parsed :class:`Selection`.

        Returns:
            The `argopy` access point (a fetcher) ready to realise.
        """
        if selection.kind == "float":
            return fetcher.float(list(selection.wmos))
        if selection.kind == "profile":
            return fetcher.profile(selection.wmos[0], selection.cycle)
        return fetcher.region(region_box(self.space, self.time, self._depth))

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write the long-format table to `root_dir` and return the path.

        Args:
            df: The canonical long-format frame.

        Returns:
            Path: The written CSV / Parquet file path.
        """
        ext = "parquet" if self._output_format == "parquet" else "csv"
        out_path = self.root_dir / f"argo_{self._dataset}_{self._selection.kind}.{ext}"
        if self._output_format == "parquet":
            try:
                df.to_parquet(out_path, index=False)
            except ImportError as exc:  # pragma: no cover - depends on env
                raise ImportError(
                    "Writing Parquet requires 'pyarrow'. Install it (pip "
                    "install pyarrow) or use output_format='csv'."
                ) from exc
        else:
            df.to_csv(out_path, index=False)
        return out_path
