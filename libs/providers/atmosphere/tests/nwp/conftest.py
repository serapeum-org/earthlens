"""Shared fakes and fixtures for the NWP backend tests.

The whole suite runs without `herbie-data` / `ecmwf-opendata` / a real
GRIB file or any network: the per-centre SDKs and the `pyramids.grib`
reader are replaced by recording fakes injected into `sys.modules`
(SDKs) or monkeypatched (pyramids), so the lazy imports inside the
centres and the `_fetch` pipeline resolve to the fakes.
"""

from __future__ import annotations

import datetime as dt
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from earthlens.nwp.catalog import Catalog, NWPModel


class _FakeHerbie:
    """Recording stand-in for `herbie.Herbie`."""

    instances: list[_FakeHerbie] = []

    def __init__(self, date: Any, **kwargs: Any) -> None:
        self.date = date
        self.kwargs = kwargs
        self.download_calls: list[str] = []
        _FakeHerbie.instances.append(self)

    def download(self, search: str) -> str:
        """Record the search selector and return a fabricated subset path."""
        self.download_calls.append(search)
        save_dir = self.kwargs.get("save_dir", ".")
        return f"{save_dir}/subset_{self.kwargs.get('model')}_f{self.kwargs.get('fxx')}.grib2"


class _FakeClient:
    """Recording stand-in for `ecmwf.opendata.Client`."""

    instances: list[_FakeClient] = []

    def __init__(self, source: str | None = None, **kwargs: Any) -> None:
        self.source = source
        self.kwargs = kwargs
        self.retrieve_calls: list[dict[str, Any]] = []
        _FakeClient.instances.append(self)

    def retrieve(self, **kwargs: Any) -> None:
        """Record the retrieve kwargs and write a stub file at `target`."""
        self.retrieve_calls.append(kwargs)
        Path(kwargs["target"]).write_bytes(b"GRIB-stub")


class _FakeResponse:
    """Stand-in for a `requests.Response` carrying bz2-compressed bytes."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.status_code = 200
        self.raised = False

    def raise_for_status(self) -> None:
        """No-op success (the fake never returns an error status)."""
        self.raised = True

    def iter_content(self, chunk_size=None):
        """Yield the canned body in one chunk, as a streamed response would."""
        yield self.content

    def close(self):
        """Release the response, as the streaming call sites do."""
        self.closed = True


class _FakeDataset:
    """Stand-in for a `pyramids` `Dataset` recording crop / longitude calls."""

    def __init__(self, path: str, global_360: bool = True) -> None:
        self.path = path
        self.global_360 = global_360
        self.cropped: tuple[Any, Any] | None = None
        self.converted = False

    def wrap_longitude(self) -> _FakeDataset:
        """Mimic pyramids: only a whole-globe 0–360 grid can be converted."""
        if not self.global_360:
            raise ValueError("The raster should cover the whole globe")
        self.converted = True
        return self

    def crop(
        self, bbox: Any = None, epsg: Any = None, touch: bool = True
    ) -> _FakeDataset:
        """Record the crop bbox / epsg / touch and return self."""
        self.cropped = (tuple(bbox), epsg)
        self.touch = touch
        return self


@pytest.fixture
def fake_herbie(monkeypatch: pytest.MonkeyPatch) -> type[_FakeHerbie]:
    """Inject a fake `herbie` module exposing the recording `Herbie`."""
    _FakeHerbie.instances = []
    module = types.ModuleType("herbie")
    module.Herbie = _FakeHerbie
    monkeypatch.setitem(sys.modules, "herbie", module)
    return _FakeHerbie


@pytest.fixture
def fake_ecmwf_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    """Inject a fake `ecmwf.opendata` module exposing the recording `Client`."""
    _FakeClient.instances = []
    pkg = types.ModuleType("ecmwf")
    sub = types.ModuleType("ecmwf.opendata")
    sub.Client = _FakeClient
    pkg.opendata = sub
    monkeypatch.setitem(sys.modules, "ecmwf", pkg)
    monkeypatch.setitem(sys.modules, "ecmwf.opendata", sub)
    return _FakeClient


@pytest.fixture
def fake_requests(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject a fake `requests` whose `get` returns bz2-compressed bytes."""
    import bz2

    state: dict[str, Any] = {"urls": []}

    def fake_get(url: str, timeout: int | None = None, **kwargs: Any) -> _FakeResponse:
        state["urls"].append(url)
        # The URL carries the variable token twice: once as the lowercase
        # `{var_lc}` path segment and once in the filename. Recover it from
        # the path segment (robust to var tokens that contain underscores).
        var = Path(url).parent.name.upper()
        return _FakeResponse(bz2.compress(b"GRIB<" + var.encode() + b">"))

    module = types.ModuleType("requests")
    module.get = fake_get
    monkeypatch.setitem(sys.modules, "requests", module)
    return state


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `pyramids.grib.open_grib` and `write_cog` with recorders."""
    import pyramids.dataset.cog as cog_mod
    import pyramids.grib as grib_mod

    state: dict[str, Any] = {"opened": [], "written": [], "global_360": True}

    def fake_open_grib(path: str, **kwargs: Any) -> _FakeDataset:
        ds = _FakeDataset(str(path), global_360=state["global_360"])
        state["opened"].append(ds)
        return ds

    def fake_write_cog(data: Any, output: str, **kwargs: Any):
        state["written"].append(str(output))
        return (Path(output), None)

    monkeypatch.setattr(grib_mod, "open_grib", fake_open_grib)
    monkeypatch.setattr(cog_mod, "write_cog", fake_write_cog)
    return state


class _FakeGrouped:
    """Stand-in for the result of `DatasetCollection.groupby(labels)`."""

    def __init__(self, labels: list[str]) -> None:
        self.labels = labels

    def mean(self, skipna: bool = True) -> dict[str, str]:
        """Return one fabricated array per unique window label, in order."""
        return {label: f"array-{label}" for label in dict.fromkeys(self.labels)}


class _FakeCollection:
    """Stand-in for `pyramids.dataset.DatasetCollection`."""

    last_files: list[str] = []

    @classmethod
    def from_files(cls, files: list[str]) -> _FakeCollection:
        """Record the input files and return a collection."""
        cls.last_files = list(files)
        return cls()

    def groupby(self, labels: list[str]) -> _FakeGrouped:
        """Return a grouped stand-in carrying the per-file labels."""
        return _FakeGrouped(labels)


class _FakeRef:
    """Stand-in reference Dataset exposing a geotransform + EPSG."""

    geotransform = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
    epsg = 4326


class _FakeAggDataset:
    """Stand-in for `pyramids.dataset.Dataset` used by _aggregate."""

    @classmethod
    def read_file(cls, path: str) -> _FakeRef:
        """Return a reference grid for the first stacked COG."""
        return _FakeRef()

    @classmethod
    def from_array(
        cls,
        arr: Any = None,
        *,
        geo_ref: Any = None,
        no_data_value: Any = None,
        path: Any = None,
    ) -> tuple:
        """Return a sentinel 'dataset' carrying the reduced array."""
        return (
            "dataset",
            arr,
            getattr(geo_ref, "geo", None),
            getattr(geo_ref, "epsg", None),
        )


@pytest.fixture
def fake_aggregate(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the pyramids aggregate surface (DatasetCollection / Dataset / write_cog)."""
    import pyramids.dataset as ds_mod
    import pyramids.dataset.cog as cog_mod

    state: dict[str, Any] = {"written": []}

    def fake_write_cog(data: Any, output: str, **kwargs: Any):
        state["written"].append(str(output))
        return (Path(output), None)

    monkeypatch.setattr(ds_mod, "DatasetCollection", _FakeCollection)
    monkeypatch.setattr(ds_mod, "Dataset", _FakeAggDataset)
    monkeypatch.setattr(cog_mod, "write_cog", fake_write_cog)
    return state


@pytest.fixture
def mini_catalog() -> Catalog:
    """A two-model catalog (Herbie gfs + direct-HTTPS icon) built in-memory."""
    return Catalog(
        datasets={
            "gfs": NWPModel(
                provider="noaa-nodd",
                model_family="gfs",
                cycles_utc=[0, 12],
                cadence_h=12,
                horizon_h=48,
                backend="herbie",
                mirrors=["aws", "google"],
                bands={"temperature_2m": ":TMP:2 m above ground:"},
            ),
            "icon-global": NWPModel(
                provider="dwd-opendata",
                model_family="icon",
                cycles_utc=[0, 12],
                horizon_h=48,
                idx=False,
                backend="direct-https",
                mirrors=["origin"],
                url_template=(
                    "https://example.test/{cycle:%H}/{var_lc}/"
                    "icon_{date:%Y%m%d%H}_{step:03d}_{var}.grib2.bz2"
                ),
                bands={"temperature_2m": "T_2M", "precipitation_acc": "TOT_PREC"},
            ),
        }
    )


@pytest.fixture
def jan_first() -> dt.datetime:
    """A fixed reference cycle datetime (2024-01-01 00Z)."""
    return dt.datetime(2024, 1, 1, 0)
