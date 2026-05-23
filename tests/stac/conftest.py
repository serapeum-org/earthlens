"""Fake-SDK fixtures for the STAC backend tests.

The STAC backend imports pyramids and the STAC SDKs lazily inside its methods,
so these fixtures inject fakes into `sys.modules` (the same pattern
`tests/cmems/` uses for `copernicusmarine`). That keeps the unit tests free of
network, GDAL, and the optional `[stac]` SDKs: `pyramids.stac.open_client`,
`pyramids.dataset.merge`, `pyramids.dataset.cog`, `pyramids.base.remote`,
`pyramids.dataset`, `pyramids.feature.bbox`, and `planetary_computer` are all
replaced with recording stubs.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest


class _FakeAsset:
    """A STAC asset stand-in carrying just an href."""

    def __init__(self, href: str) -> None:
        self.href = href


class _FakeItem:
    """A STAC item stand-in with id, datetime, and an assets map."""

    def __init__(self, item_id: str, date: str, asset_hrefs: dict[str, str]) -> None:
        import datetime as dt

        self.id = item_id
        self.datetime = dt.datetime.strptime(date, "%Y-%m-%d")
        self.assets = {k: _FakeAsset(v) for k, v in asset_hrefs.items()}


def make_item(item_id: str, date: str, asset_hrefs: dict[str, str]) -> _FakeItem:
    """Build a fake STAC item for a search result."""
    return _FakeItem(item_id, date, asset_hrefs)


class _FakeSearch:
    """A pystac-client search stand-in yielding a fixed item list."""

    def __init__(self, items: list[_FakeItem]) -> None:
        self._items = items

    def items(self):
        """Yield the configured items."""
        return iter(self._items)


class _FakeClient:
    """A pystac-client Client stand-in recording every search call."""

    def __init__(self, items_by_collection: dict[str, list[_FakeItem]]) -> None:
        self._items_by_collection = items_by_collection
        self.search_calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> _FakeSearch:
        """Record the search kwargs and return the items for the collection."""
        self.search_calls.append(dict(kwargs))
        collection = kwargs.get("collections", [None])[0]
        return _FakeSearch(self._items_by_collection.get(collection, []))


class _FakeDataset:
    """A pyramids Dataset stand-in for the load/mosaic/write path."""

    def __init__(self, href: str = "", epsg: int | None = 32630) -> None:
        self.href = href
        self.epsg = epsg
        self.geotransform = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
        self.cropped_bbox: list[float] | None = None
        self.cropped_epsg: int | None = None
        self.reprojected_to: int | None = None

    def to_crs(self, epsg: int) -> _FakeDataset:
        """Return a copy reprojected to `epsg` (records the target)."""
        out = _FakeDataset(self.href, epsg)
        out.reprojected_to = epsg
        return out

    def crop(self, mask=None, touch: bool = True, *, bbox=None, epsg=None) -> _FakeDataset:
        """Mirror pyramids' keyword-only crop, recording the bbox + its CRS."""
        out = _FakeDataset(self.href, self.epsg)
        out.cropped_bbox = list(bbox) if bbox is not None else None
        out.cropped_epsg = epsg
        return out

    def to_file(self, path: str) -> None:
        """Write a placeholder file at `path`."""
        Path(path).write_bytes(b"")


class _FakeCloudConfig:
    """A pyramids CloudConfig stand-in recording the GDAL env it was given."""

    active_extras: list[dict[str, str]] = []

    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self.extra = extra or {}

    def __enter__(self) -> _FakeCloudConfig:
        _FakeCloudConfig.active_extras.append(self.extra)
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeGrouped:
    """A pyramids _GroupedCollection stand-in returning one array per label."""

    def __init__(self, labels: list[str]) -> None:
        self._labels = list(dict.fromkeys(labels))

    def _reduce(self, **kwargs):
        import numpy as np

        return {label: np.zeros((1, 1)) for label in self._labels}

    mean = sum = min = max = std = var = _reduce


class _FakeDatasetCollection:
    """A pyramids DatasetCollection stand-in (from_files + groupby)."""

    def __init__(self) -> None:
        self.files: list[str] = []

    @classmethod
    def from_files(cls, files, **kwargs) -> _FakeDatasetCollection:
        """Build a collection from file paths."""
        inst = cls()
        inst.files = list(files)
        return inst

    def groupby(self, labels) -> _FakeGrouped:
        """Return a grouped view keyed by the per-timestep labels."""
        return _FakeGrouped(labels)


class FakePyramids:
    """Container exposing every recording stub + the configurable client."""

    def __init__(self) -> None:
        self.items_by_collection: dict[str, list[_FakeItem]] = {}
        self.client = _FakeClient(self.items_by_collection)
        self.open_client_calls: list[dict[str, Any]] = []
        self.merge_calls: list[tuple[list[str], str]] = []
        self.stack_calls: list[dict[str, Any]] = []
        self.write_calls: list[str] = []
        self.write_data: list = []
        self.split_antimeridian_calls: list[tuple] = []
        self.dataset_epsgs: dict[str, int] = {}


@pytest.fixture
def fake_pc(monkeypatch: pytest.MonkeyPatch):
    """Install a fake `planetary_computer` module (sign / sign_inplace)."""
    pc = types.ModuleType("planetary_computer")
    pc.sign_calls = []
    pc.sign_inplace_calls = []

    def _sign(href: str) -> str:
        pc.sign_calls.append(href)
        return href + "?sas=token"

    def _sign_inplace(item: Any) -> None:
        pc.sign_inplace_calls.append(item)
        return None

    pc.sign = _sign
    pc.sign_inplace = _sign_inplace
    monkeypatch.setitem(sys.modules, "planetary_computer", pc)
    return pc


class _AnonymousSigner:
    name = "anonymous"

    def sign_request(self, request: Any) -> None:
        return None

    def sign_item(self, item: Any) -> None:
        return None

    def sign_href(self, href: str) -> str:
        return href

    def gdal_env(self) -> dict[str, str]:
        return {}


class _AWSRequesterPaysSigner(_AnonymousSigner):
    name = "aws-requester-pays"

    def __init__(self, region: str | None = None) -> None:
        self.region = region

    def gdal_env(self) -> dict[str, str]:
        return {"AWS_REQUEST_PAYER": "requester"}


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> FakePyramids:
    """Inject fake pyramids submodules into `sys.modules` and return the recorder."""
    fp = FakePyramids()

    stac_mod = types.ModuleType("pyramids.stac")
    stac_mod.AnonymousSigner = _AnonymousSigner
    stac_mod.AWSRequesterPaysSigner = _AWSRequesterPaysSigner

    def _open_client(url: str, *, signer: Any = None, **kwargs: Any):
        fp.open_client_calls.append({"url": url, "signer": signer})
        return fp.client

    stac_mod.open_client = _open_client
    monkeypatch.setitem(sys.modules, "pyramids.stac", stac_mod)

    merge_mod = types.ModuleType("pyramids.dataset.merge")

    def _merge_rasters(src, dst, **kwargs):
        fp.merge_calls.append((list(src), str(dst)))
        Path(dst).write_bytes(b"")

    def _stack_bands(files, **kwargs):
        fp.stack_calls.append({"files": list(files), **kwargs})
        return _FakeDataset()

    merge_mod.merge_rasters = _merge_rasters
    merge_mod.stack_bands = _stack_bands
    monkeypatch.setitem(sys.modules, "pyramids.dataset.merge", merge_mod)

    cog_mod = types.ModuleType("pyramids.dataset.cog")

    def _write_cog(data, output, **kwargs):
        fp.write_calls.append(str(output))
        fp.write_data.append(data)
        Path(output).write_bytes(b"")
        return Path(output), None

    cog_mod.write_cog = _write_cog
    monkeypatch.setitem(sys.modules, "pyramids.dataset.cog", cog_mod)

    remote_mod = types.ModuleType("pyramids.base.remote")
    remote_mod.CloudConfig = _FakeCloudConfig
    monkeypatch.setitem(sys.modules, "pyramids.base.remote", remote_mod)

    dataset_mod = types.ModuleType("pyramids.dataset")

    def _read_file(href, **kwargs):
        return _FakeDataset(href, fp.dataset_epsgs.get(href, 32630))

    def _create_from_array(arr=None, geo=None, epsg=None, **kwargs):
        return _FakeDataset()

    dataset_mod.Dataset = type(
        "Dataset",
        (),
        {
            "read_file": staticmethod(_read_file),
            "create_from_array": staticmethod(_create_from_array),
        },
    )
    dataset_mod.DatasetCollection = _FakeDatasetCollection
    monkeypatch.setitem(sys.modules, "pyramids.dataset", dataset_mod)

    bbox_mod = types.ModuleType("pyramids.feature.bbox")

    def _split_antimeridian(bbox):
        fp.split_antimeridian_calls.append(tuple(bbox))
        w, s, e, n = bbox
        if w <= e:
            return [(w, s, e, n)]
        return [(w, s, 180.0, n), (-180.0, s, e, n)]

    bbox_mod.split_antimeridian = _split_antimeridian
    monkeypatch.setitem(sys.modules, "pyramids.feature.bbox", bbox_mod)

    _FakeCloudConfig.active_extras = []
    return fp
