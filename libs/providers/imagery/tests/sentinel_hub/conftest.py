"""Shared fakes + fixtures for the Sentinel Hub backend tests (no network, no SDK).

The real `sentinelhub` SDK is replaced in `sys.modules` by :class:`FakeSentinelHub`
so the backend's lazy `import sentinelhub` returns the fake. The fake records
every request it builds and writes placeholder GeoTIFFs / returns canned stats so
the per-plane fetchers can be exercised without touching CDSE.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


class FakeCRS:
    """Stand-in for `sentinelhub.CRS` with the one member the backend uses."""

    WGS84 = "EPSG:4326"


class FakeMimeType:
    """Stand-in for `sentinelhub.MimeType`."""

    TIFF = "image/tiff"


class FakeMosaickingOrder:
    """Stand-in for `sentinelhub.MosaickingOrder`."""

    MOST_RECENT = "mostRecent"
    LEAST_RECENT = "leastRecent"
    LEAST_CC = "leastCC"


class FakeBBox:
    """Stand-in for `sentinelhub.BBox` recording its coordinates + CRS."""

    def __init__(self, bbox: tuple, crs: Any = None) -> None:
        self.bbox = tuple(bbox)
        self.crs = crs


class FakeDataCollectionMember:
    """Stand-in for a `DataCollection` enum member supporting `define_from`."""

    def __init__(self, name: str, service_url: str | None = None) -> None:
        self.name = name
        self.service_url = service_url

    def define_from(self, new_name: str, service_url: str) -> FakeDataCollectionMember:
        """Rebind to a new service URL (the CDSE binding)."""
        return FakeDataCollectionMember(new_name, service_url=service_url)


class FakeDataCollection:
    """Stand-in for `sentinelhub.DataCollection` resolving members on access."""

    _KNOWN = {
        "SENTINEL2_L1C",
        "SENTINEL2_L2A",
        "SENTINEL1_IW",
        "SENTINEL3_OLCI",
        "SENTINEL5P",
    }

    def __getattr__(self, name: str) -> FakeDataCollectionMember:
        if name in self._KNOWN:
            return FakeDataCollectionMember(name)
        raise AttributeError(name)


class FakeSHConfig:
    """Stand-in for `sentinelhub.SHConfig` carrying the writable url/cred fields."""

    def __init__(self, profile: str | None = None) -> None:
        self.profile = profile
        self.sh_base_url = "https://services.sentinel-hub.com"
        self.sh_token_url = "https://services.sentinel-hub.com/token"
        self.sh_auth_base_url = "https://services.sentinel-hub.com"
        self.sh_client_id = ""
        self.sh_client_secret = ""


class FakeSentinelHubRequest:
    """Stand-in for `sentinelhub.SentinelHubRequest` recording its construction."""

    #: Every constructed request, for assertions.
    instances: list[FakeSentinelHubRequest] = []

    def __init__(
        self,
        evalscript: str,
        input_data: list,
        responses: list,
        bbox: Any = None,
        size: tuple | None = None,
        data_folder: str | None = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        self.evalscript = evalscript
        self.input_data = input_data
        self.responses = responses
        self.bbox = bbox
        self.size = size
        self.data_folder = data_folder
        self.config = config
        self.kwargs = kwargs
        self._written: list[str] = []
        FakeSentinelHubRequest.instances.append(self)

    @staticmethod
    def input_data(
        data_collection: Any,
        time_interval: Any = None,
        mosaicking_order: Any = None,
        maxcc: float | None = None,
        **kwargs: Any,
    ) -> dict:
        """Record an `input_data` block."""
        return {
            "data_collection": data_collection,
            "time_interval": time_interval,
            "mosaicking_order": mosaicking_order,
            "maxcc": maxcc,
            **kwargs,
        }

    @staticmethod
    def output_response(identifier: str, response_format: Any, **kwargs: Any) -> dict:
        """Record an `output_response` block."""
        return {"identifier": identifier, "format": response_format}

    def get_data(self, save_data: bool = False) -> list:
        """Write one small real GeoTIFF under `data_folder` and return a payload."""
        import numpy as np
        from pyramids.dataset import Dataset, GeoReference

        folder = Path(self.data_folder) / "abc123hash"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "response.tiff"
        # A readable raster rather than bare TIFF magic: the tiling path reads
        # each rendered tile back to inherit its no-data before mosaicking.
        Dataset.from_array(
            np.arange(9, dtype="float32").reshape(3, 3),
            geo_ref=GeoReference(
                top_left_corner=(14.0, 40.5), cell_size=0.1, epsg=4326
            ),
        ).to_file(str(target))
        self._written = [str(target.relative_to(self.data_folder))]
        return [b"fake-array"]

    def get_filename_list(self) -> list[str]:
        """Return the relative path(s) written by the last `get_data`."""
        return self._written


class FakeAsyncProcessRequest:
    """Stand-in for `sentinelhub.AsyncProcessRequest` (S3-delivered)."""

    instances: list[FakeAsyncProcessRequest] = []

    def __init__(
        self,
        evalscript: str,
        input_data: list,
        responses: list,
        delivery: Any,
        bbox: Any = None,
        size: tuple | None = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        self.evalscript = evalscript
        self.input_data = input_data
        self.responses = responses
        self.delivery = delivery
        self.bbox = bbox
        self.size = size
        self.config = config
        self.submitted = False
        FakeAsyncProcessRequest.instances.append(self)

    @staticmethod
    def input_data(**kwargs: Any) -> dict:
        """Record an `input_data` block (same shape as the sync request)."""
        return FakeSentinelHubRequest.input_data(**kwargs)

    @staticmethod
    def output_response(identifier: str, response_format: Any, **kwargs: Any) -> dict:
        """Record an `output_response` block."""
        return {"identifier": identifier, "format": response_format}

    @staticmethod
    def s3_specification(url: str, **kwargs: Any) -> dict:
        """Record an S3 delivery spec."""
        return {"url": url, **kwargs}

    def get_data(self, save_data: bool = False) -> list:
        """Pretend to submit the async job; return the submission JSON (with id)."""
        self.submitted = True
        return [{"id": "async-job-1", "status": "CREATED"}]

    def get_url_list(self) -> list[str]:
        """Return the (fake) S3 delivery URI for the job."""
        return [f"{self.delivery['url']}/async-result.tiff"]


def fake_get_async_running_status(ids: Any, config: Any = None) -> dict:
    """Stand-in for `get_async_running_status`: nothing is still running.

    The real endpoint resolves `…/async/process/{id}`, so the ids must be async
    **request ids**, never delivery URLs — reject a URL-shaped id so a regression
    that polls the wrong identifier is caught by the tests.
    """
    items = list(ids)
    for item in items:
        if "://" in str(item):
            raise AssertionError(
                f"get_async_running_status expects an async request id, got a "
                f"URL-like value {item!r}"
            )
    return {item: False for item in items}


class FakeGeometry:
    """Stand-in for `sentinelhub.Geometry`."""

    def __init__(self, geometry: Any, crs: Any) -> None:
        self.geometry = geometry
        self.crs = crs


class FakeSentinelHubStatistical:
    """Stand-in for `sentinelhub.SentinelHubStatistical` returning canned stats."""

    instances: list[FakeSentinelHubStatistical] = []

    def __init__(
        self,
        aggregation: Any,
        input_data: list,
        bbox: Any = None,
        geometry: Any = None,
        calculations: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        self.aggregation = aggregation
        self.input_data = input_data
        self.geometry = geometry
        self.calculations = calculations
        self.config = config
        FakeSentinelHubStatistical.instances.append(self)

    @staticmethod
    def aggregation(
        evalscript: str,
        time_interval: Any,
        aggregation_interval: str,
        size: Any = None,
        resolution: Any = None,
        **kwargs: Any,
    ) -> dict:
        """Record an aggregation block."""
        return {
            "evalscript": evalscript,
            "time_interval": time_interval,
            "aggregation_interval": aggregation_interval,
            "resolution": resolution,
        }

    @staticmethod
    def input_data(
        data_collection: Any, maxcc: float | None = None, **kwargs: Any
    ) -> dict:
        """Record a statistical input_data block."""
        return {"data_collection": data_collection, "maxcc": maxcc}

    def get_data(self) -> list:
        """Return a canned nested interval→output→band→stats payload."""
        return [
            {
                "data": [
                    {
                        "interval": {
                            "from": "2020-06-01T00:00:00Z",
                            "to": "2020-06-02T00:00:00Z",
                        },
                        "outputs": {
                            "ndvi": {
                                "bands": {
                                    "B0": {
                                        "stats": {
                                            "min": 0.1,
                                            "max": 0.8,
                                            "mean": 0.45,
                                            "stDev": 0.2,
                                            "sampleCount": 100,
                                            "noDataCount": 5,
                                        },
                                        "percentiles": {
                                            "5": 0.12,
                                            "50": 0.46,
                                            "95": 0.78,
                                        },
                                    }
                                }
                            },
                            "dataMask": {"bands": {"B0": {"stats": {"mean": 1.0}}}},
                        },
                    }
                ],
                "status": "OK",
            }
        ]


class FakeBatchProcessRequest:
    """Stand-in for `sentinelhub.BatchProcessRequest`.

    `cost_PU` starts `None` and is populated only once analysis is monitored
    (mirrors the live ordering: `start_analysis` merely starts the phase).
    """

    def __init__(self) -> None:
        self.completion_percentage = 100.0
        self.cost_PU = None


class FakeBatchProcessClient:
    """Stand-in for `sentinelhub.BatchProcessClient` recording the job lifecycle."""

    instances: list[FakeBatchProcessClient] = []

    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.calls: list[str] = []
        FakeBatchProcessClient.instances.append(self)

    @staticmethod
    def s3_specification(url: str, **kwargs: Any) -> dict:
        """Record an S3 delivery spec."""
        return {"url": url, **kwargs}

    @staticmethod
    def tiling_grid_input(grid_id: int, resolution: float, **kwargs: Any) -> dict:
        """Record a tiling-grid input."""
        return {"grid_id": grid_id, "resolution": resolution, **kwargs}

    @staticmethod
    def raster_output(delivery: Any, **kwargs: Any) -> dict:
        """Record a raster-output spec."""
        return {"delivery": delivery, **kwargs}

    #: cost_PU the analysed request reports back (overridable per test).
    cost_pu = 5.0

    def create(self, process_request: Any, input: Any, output: Any, **kwargs: Any):
        """Record batch-request creation and return a fake request (cost_PU=None)."""
        self.calls.append("create")
        self.created = {"input": input, "output": output}
        return FakeBatchProcessRequest()

    def get_request(self, batch_request: Any):
        """Return the (analysed) request; a read, not a lifecycle step."""
        return batch_request

    def start_analysis(self, batch_request: Any):
        """Record the analysis step."""
        self.calls.append("start_analysis")

    def start_job(self, batch_request: Any):
        """Record the job start."""
        self.calls.append("start_job")


def fake_monitor_batch_process_analysis(request: Any, client: Any, **kwargs: Any):
    """Stand-in for `monitor_batch_process_analysis`: completes the analysis phase.

    Populates `cost_PU` (as the live monitor does) so the cost guard can read it;
    a read, not a job-lifecycle step, so it is not recorded in `client.calls`.
    """
    request.cost_PU = type(client).cost_pu
    return request


def fake_monitor_batch_process_job(request: Any, client: Any, **kwargs: Any):
    """Stand-in for `monitor_batch_process_job`: completes immediately."""
    client.calls.append("monitor")
    return request


class FakeBatchStatisticalRequest:
    """Stand-in for `sentinelhub.BatchStatisticalRequest`."""

    request_id = "batch-stat-1"


class FakeSentinelHubBatchStatistical:
    """Stand-in for `sentinelhub.SentinelHubBatchStatistical`."""

    instances: list[FakeSentinelHubBatchStatistical] = []

    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.calls: list[str] = []
        self.created: dict = {}
        FakeSentinelHubBatchStatistical.instances.append(self)

    @staticmethod
    def s3_specification(url: str, **kwargs: Any) -> dict:
        """Record an S3 spec."""
        return {"url": url, **kwargs}

    def create(
        self,
        *,
        input_features: Any,
        input_data: Any,
        aggregation: Any,
        calculations: Any,
        output: Any,
        **kwargs: Any,
    ):
        """Record batch-statistical creation."""
        self.calls.append("create")
        self.created = {
            "input_features": input_features,
            "output": output,
            "aggregation": aggregation,
        }
        return FakeBatchStatisticalRequest()

    def start_analysis(self, batch_request: Any):
        """Record the analysis step."""
        self.calls.append("start_analysis")

    def start_job(self, batch_request: Any):
        """Record the job start."""
        self.calls.append("start_job")


def fake_monitor_batch_statistical_job(
    batch_request: Any, config: Any = None, **kwargs
):
    """Stand-in for `monitor_batch_statistical_job`."""
    return {"status": "DONE"}


class FakeAwsBatchStatisticalResults:
    """Stand-in for `sentinelhub.aws.AwsBatchStatisticalResults` (per-feature JSON)."""

    def __init__(
        self,
        batch_request: Any,
        *,
        feature_ids: Any = None,
        data_folder: str | None = None,
        config: Any = None,
    ) -> None:
        self.batch_request = batch_request
        self.feature_ids = feature_ids
        self.data_folder = data_folder

    def get_data(self, save_data: bool = False) -> list:
        """Return one canned stats payload per requested feature."""
        ids = self.feature_ids or [0, 1]
        payload = {
            "data": [
                {
                    "interval": {
                        "from": "2020-06-01T00:00:00Z",
                        "to": "2020-06-02T00:00:00Z",
                    },
                    "outputs": {
                        "ndvi": {
                            "bands": {
                                "B0": {"stats": {"mean": 0.5, "min": 0.2, "max": 0.7}}
                            }
                        },
                    },
                }
            ]
        }
        return [payload for _ in ids]


class FakeSentinelHubCatalog:
    """Stand-in for `sentinelhub.SentinelHubCatalog` returning canned items."""

    #: Items the next `search` returns; tests may override.
    items: list[dict] = [
        {
            "id": "S2_TILE_A",
            "properties": {"datetime": "2020-06-01T10:00:00Z"},
            "geometry": {"type": "Polygon", "coordinates": []},
        },
        {
            "id": "S2_TILE_B",
            "properties": {"datetime": "2020-06-02T10:00:00Z"},
            "geometry": {"type": "Polygon", "coordinates": []},
        },
    ]

    #: Class-level log of every search issued (backend builds its own instance).
    searches: list[dict] = []

    def __init__(self, config: Any = None) -> None:
        self.config = config

    def search(self, collection: Any, **kwargs: Any) -> list[dict]:
        """Record the search args and return the canned items."""
        FakeSentinelHubCatalog.searches.append({"collection": collection, **kwargs})
        return list(FakeSentinelHubCatalog.items)

    def get_collections(self) -> list[dict]:
        """Return canned collection metadata dicts (for the refresh tool)."""
        return [
            {"id": "sentinel-2-l2a"},
            {"id": "sentinel-1-grd"},
            {"id": "sentinel-3-olci"},
        ]


def fake_bbox_to_dimensions(bbox: Any, resolution: float) -> tuple[int, int]:
    """Deterministic `bbox_to_dimensions`: (degrees * 1000 / resolution) per side."""
    west, south, east, north = bbox.bbox
    width = max(1, int(round((east - west) * 100000 / resolution)))
    height = max(1, int(round((north - south) * 100000 / resolution)))
    return width, height


class FakeSentinelHub:
    """Stand-in for the top-level `sentinelhub` module."""

    SHConfig = FakeSHConfig
    BBox = FakeBBox
    CRS = FakeCRS
    MimeType = FakeMimeType
    MosaickingOrder = FakeMosaickingOrder
    DataCollection = FakeDataCollection()
    SentinelHubRequest = FakeSentinelHubRequest
    AsyncProcessRequest = FakeAsyncProcessRequest
    SentinelHubStatistical = FakeSentinelHubStatistical
    Geometry = FakeGeometry
    BatchProcessClient = FakeBatchProcessClient
    SentinelHubBatchStatistical = FakeSentinelHubBatchStatistical
    SentinelHubCatalog = FakeSentinelHubCatalog
    bbox_to_dimensions = staticmethod(fake_bbox_to_dimensions)
    get_async_running_status = staticmethod(fake_get_async_running_status)
    monitor_batch_process_analysis = staticmethod(fake_monitor_batch_process_analysis)
    monitor_batch_process_job = staticmethod(fake_monitor_batch_process_job)
    monitor_batch_statistical_job = staticmethod(fake_monitor_batch_statistical_job)


@pytest.fixture
def fake_sh(monkeypatch: pytest.MonkeyPatch) -> FakeSentinelHub:
    """Install the fake `sentinelhub` module and reset recorded requests."""
    import types

    module = FakeSentinelHub()
    FakeSentinelHubRequest.instances = []
    FakeAsyncProcessRequest.instances = []
    FakeSentinelHubStatistical.instances = []
    FakeBatchProcessClient.instances = []
    FakeSentinelHubBatchStatistical.instances = []
    monkeypatch.setitem(sys.modules, "sentinelhub", module)
    aws_mod = types.ModuleType("sentinelhub.aws")
    aws_mod.AwsBatchStatisticalResults = FakeAwsBatchStatisticalResults
    monkeypatch.setitem(sys.modules, "sentinelhub.aws", aws_mod)
    return module


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """A throwaway output directory for a backend instance."""
    target = tmp_path / "out"
    target.mkdir()
    return target


@pytest.fixture(autouse=True)
def _clear_sh_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure SH_* credentials never leak into the faked-SDK unit/integration tests.

    Skipped for `e2e`-marked tests, which authenticate against the live service
    with the real `SH_CLIENT_ID` / `SH_CLIENT_SECRET` from the environment.
    """
    if request.node.get_closest_marker("e2e"):
        return
    for key in (
        "SENTINELHUB_CLIENT_ID",
        "SENTINELHUB_CLIENT_SECRET",
        "SENTINELHUB_PROFILE",
        "SH_CLIENT_ID",
        "SH_CLIENT_SECRET",
        "SH_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)
