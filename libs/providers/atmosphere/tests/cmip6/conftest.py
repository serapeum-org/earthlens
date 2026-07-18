"""Fixtures + fakes for the CMIP6 backend tests (offline, no network / GDAL).

The consolidated-stores CSV is replaced by a tiny in-memory `DataFrame`
(:func:`store_frame`) injected into :class:`~earthlens.cmip6.StoreResolver`, and
the pyramids readers are replaced by the fakes below so no `gs://` store is ever
opened.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from earthlens.cmip6 import Catalog, StoreResolver

#: One row per Zarr store, mirroring the real CSV columns. Two versions of the
#: same store exercise `version="latest"`; a second member and a second
#: experiment / model exercise fan-out and misses.
_ROWS = [
    {
        "activity_id": "ScenarioMIP", "institution_id": "CCCma", "source_id": "CanESM5",
        "experiment_id": "ssp585", "member_id": "r1i1p1f1", "table_id": "Amon",
        "variable_id": "tas", "grid_label": "gn", "version": 20190101,
        "zstore": "gs://cmip6/CanESM5/ssp585/tas/gn/v20190101/", "dcpp_init_year": None,
    },
    {
        "activity_id": "ScenarioMIP", "institution_id": "CCCma", "source_id": "CanESM5",
        "experiment_id": "ssp585", "member_id": "r1i1p1f1", "table_id": "Amon",
        "variable_id": "tas", "grid_label": "gn", "version": 20190429,
        "zstore": "gs://cmip6/CanESM5/ssp585/tas/gn/v20190429/", "dcpp_init_year": None,
    },
    {
        "activity_id": "ScenarioMIP", "institution_id": "CCCma", "source_id": "CanESM5",
        "experiment_id": "ssp585", "member_id": "r1i1p1f1", "table_id": "Amon",
        "variable_id": "tas", "grid_label": "gr", "version": 20190429,
        "zstore": "gs://cmip6/CanESM5/ssp585/tas/gr/v20190429/", "dcpp_init_year": None,
    },
    {
        "activity_id": "ScenarioMIP", "institution_id": "CCCma", "source_id": "CanESM5",
        "experiment_id": "ssp585", "member_id": "r2i1p1f1", "table_id": "Amon",
        "variable_id": "tas", "grid_label": "gn", "version": 20190429,
        "zstore": "gs://cmip6/CanESM5/ssp585/tas/gn-r2/v20190429/", "dcpp_init_year": None,
    },
    {
        "activity_id": "CMIP", "institution_id": "CCCma", "source_id": "CanESM5",
        "experiment_id": "historical", "member_id": "r1i1p1f1", "table_id": "Amon",
        "variable_id": "tas", "grid_label": "gn", "version": 20190429,
        "zstore": "gs://cmip6/CanESM5/historical/tas/gn/v20190429/", "dcpp_init_year": None,
    },
    {
        "activity_id": "ScenarioMIP", "institution_id": "NOAA-GFDL", "source_id": "GFDL-ESM4",
        "experiment_id": "ssp585", "member_id": "r1i1p1f1", "table_id": "day",
        "variable_id": "pr", "grid_label": "gr1", "version": 20180701,
        "zstore": "gs://cmip6/GFDL-ESM4/ssp585/pr/gr1/v20180701/", "dcpp_init_year": None,
    },
]


@pytest.fixture
def store_frame() -> pd.DataFrame:
    """A tiny consolidated-stores table standing in for the real CSV."""
    return pd.DataFrame(_ROWS)


@pytest.fixture
def catalog() -> Catalog:
    """The bundled CMIP6 catalog (config + curated vocabulary)."""
    return Catalog()


@pytest.fixture
def resolver(catalog: Catalog, store_frame: pd.DataFrame) -> StoreResolver:
    """A resolver backed by the fixture frame (no network)."""
    return StoreResolver(catalog.csv_url, catalog.facet_columns, frame=store_frame)


class FakeSubset:
    """Stand-in for a pyramids windowed subset that records its write."""

    def __init__(self) -> None:
        self.written: str | None = None

    def to_file(self, path: str) -> None:
        """Record the destination path a real subset would be written to."""
        self.written = path


class FakeContainer:
    """Stand-in for `pyramids.netcdf.NetCDF` opened over a store."""

    def __init__(self, variable_names: list[str] | None = None) -> None:
        self.variable_names = variable_names or ["tas"]
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.last_subset: FakeSubset | None = None

    def subset(self, variable: str, **kwargs: Any) -> FakeSubset:
        """Record the `(variable, time, bbox, crs)` window and return a subset."""
        self.calls.append({"variable": variable, **kwargs})
        self.last_subset = FakeSubset()
        return self.last_subset

    def close(self) -> None:
        """Mark the handle closed."""
        self.closed = True


class FakeLabeled:
    """Stand-in for `pyramids.netcdf.LabeledDataset` with a CF time axis.

    `select_time(start=...)` keeps steps at/after `start`; `select_time(end=...)`
    keeps steps at/before `end`, over a fixed monthly axis, so the backend's
    date-window -> index math can be checked without a real store. Like the real
    reader, an empty single-sided selection **raises** `ValueError`.
    """

    _AXIS = pd.date_range("2015-01-01", periods=24, freq="MS")

    def __init__(self, kept: Any = None) -> None:
        self._kept = self._AXIS if kept is None else kept

    @property
    def sizes(self) -> dict[str, int]:
        """Report the current time-axis length."""
        return {"time": len(self._kept)}

    def select_time(self, start: Any = None, end: Any = None, *, time_dim: str = "time") -> FakeLabeled:
        """Return a view restricted to the in-range steps (raising when empty)."""
        kept = self._kept
        if start is not None:
            kept = kept[kept >= pd.Timestamp(start)]
        if end is not None:
            kept = kept[kept <= pd.Timestamp(end)]
        if len(kept) == 0:
            raise ValueError(f"no timesteps in window [{start}, {end}]")
        return FakeLabeled(kept)

    def close(self) -> None:
        """No-op close."""
