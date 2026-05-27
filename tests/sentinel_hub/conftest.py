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
        bbox: Any,
        size: tuple,
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
        """Write one placeholder GeoTIFF under `data_folder` and return a payload."""
        folder = Path(self.data_folder) / "abc123hash"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "response.tiff"
        target.write_bytes(b"II*\x00fake-geotiff")
        self._written = [str(target.relative_to(self.data_folder))]
        return [b"fake-array"]

    def get_filename_list(self) -> list[str]:
        """Return the relative path(s) written by the last `get_data`."""
        return self._written


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
    bbox_to_dimensions = staticmethod(fake_bbox_to_dimensions)


@pytest.fixture
def fake_sh(monkeypatch: pytest.MonkeyPatch) -> FakeSentinelHub:
    """Install the fake `sentinelhub` module and reset recorded requests."""
    module = FakeSentinelHub()
    FakeSentinelHubRequest.instances = []
    monkeypatch.setitem(sys.modules, "sentinelhub", module)
    return module


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """A throwaway output directory for a backend instance."""
    target = tmp_path / "out"
    target.mkdir()
    return target


@pytest.fixture(autouse=True)
def _clear_sh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure SH_* credentials never leak in from the real environment."""
    for key in ("SH_CLIENT_ID", "SH_CLIENT_SECRET", "SH_PROFILE"):
        monkeypatch.delenv(key, raising=False)
