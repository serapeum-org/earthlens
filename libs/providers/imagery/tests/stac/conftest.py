"""Fake-SDK fixtures for the STAC backend tests.

The STAC backend imports pyramids and the STAC SDKs lazily inside its methods,
so these fixtures inject fakes into `sys.modules` (the same pattern
`tests/cmems/` uses for `copernicusmarine`). That keeps the unit tests free of
network, GDAL, and the optional `[stac]` SDK: `pyramids.stac.open_client`,
`pyramids.dataset.merge`, `pyramids.dataset.cog`, `pyramids.base.remote`,
`pyramids.dataset`, and `pyramids.feature.bbox` are all replaced with recording
stubs.
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

    def __init__(
        self,
        item_id: str,
        date: str,
        asset_hrefs: dict[str, str],
        proj_epsg: int | None = None,
    ) -> None:
        import datetime as dt

        self.id = item_id
        self.datetime = dt.datetime.strptime(date, "%Y-%m-%d")
        self.assets = {k: _FakeAsset(v) for k, v in asset_hrefs.items()}
        self.properties = {"proj:epsg": proj_epsg} if proj_epsg is not None else {}


def make_item(
    item_id: str, date: str, asset_hrefs: dict[str, str], proj_epsg: int | None = None
) -> _FakeItem:
    """Build a fake STAC item for a search result (optional `proj:epsg`)."""
    return _FakeItem(item_id, date, asset_hrefs, proj_epsg)


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

    def __init__(
        self, href: str = "", epsg: int | None = 32630, shape: tuple = (1, 2, 2)
    ) -> None:
        self.href = href
        self.epsg = epsg
        self.shape = shape
        self.geotransform = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
        self.cropped_bbox: list[float] | None = None
        self.cropped_epsg: int | None = None
        self.reprojected_to: int | None = None
        self.aligned_to: tuple | None = None

    def to_crs(self, epsg: int) -> _FakeDataset:
        """Return a copy reprojected to `epsg` (records the target)."""
        out = _FakeDataset(self.href, epsg)
        out.reprojected_to = epsg
        return out

    def read_array(self, band: int = 0):
        """Return a zero array matching this dataset's (rows, cols)."""
        import numpy as np

        return np.zeros(self.shape[-2:], dtype="uint16")

    def align(self, reference: _FakeDataset) -> _FakeDataset:
        """Return a copy resampled onto `reference`'s grid (records the target)."""
        out = _FakeDataset(self.href, self.epsg, reference.shape)
        out.aligned_to = reference.shape
        return out

    def crop(
        self, mask=None, touch: bool = True, *, bbox=None, epsg=None
    ) -> _FakeDataset:
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
        self.dataset_shapes: dict[str, tuple] = {}
        self.create_calls: list[dict[str, Any]] = []


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


class _PlanetaryComputerSigner(_AnonymousSigner):
    """Stand-in for pyramids' native PlanetaryComputerSigner (SAS URL signing)."""

    name = "planetary-computer"

    def sign_href(self, href: str) -> str:
        return href + "?sas=token"


def _resolved_href(
    item_or_asset: Any, asset_key: Any = None, *, signer: Any = None
) -> str:
    """Stand-in for pyramids.stac.resolved_href: resolve href + apply sign_href."""
    if asset_key is None:
        asset = item_or_asset
    else:
        assets = getattr(item_or_asset, "assets", None)
        if assets is None and isinstance(item_or_asset, dict):
            assets = item_or_asset.get("assets")
        if not assets or asset_key not in assets:
            raise KeyError(
                f"asset {asset_key!r} not found; available {sorted(assets or [])}"
            )
        asset = assets[asset_key]
    href = getattr(asset, "href", None)
    if href is None and isinstance(asset, dict):
        href = asset.get("href")
    if href is None:
        raise KeyError(f"asset {asset_key!r} has no 'href'")
    href = str(href)
    return signer.sign_href(href) if signer is not None else href


def _read_extension_metadata(item: Any, asset_key: Any = None) -> dict[str, Any]:
    """Stand-in for pyramids.stac.read_extension_metadata (proj:epsg only here)."""
    props = getattr(item, "properties", None)
    if props is None and isinstance(item, dict):
        props = item.get("properties")
    epsg = props.get("proj:epsg") if isinstance(props, dict) else None
    return {"epsg": epsg if isinstance(epsg, int) else None}


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> FakePyramids:
    """Inject fake pyramids submodules into `sys.modules` and return the recorder."""
    fp = FakePyramids()

    stac_mod = types.ModuleType("pyramids.stac")
    # earthlens no longer builds pyramids' `AnonymousSigner` — `build_signer`
    # returns its own `_AnonymousS3Signer`, which adds AWS_NO_SIGN_REQUEST — so
    # this binding is only here for any test that reaches for it directly.
    stac_mod.AnonymousSigner = _AnonymousSigner
    stac_mod.AWSRequesterPaysSigner = _AWSRequesterPaysSigner
    stac_mod.PlanetaryComputerSigner = _PlanetaryComputerSigner
    stac_mod.resolved_href = _resolved_href
    stac_mod.read_extension_metadata = _read_extension_metadata

    def _open_client(url: str, *, signer: Any = None, **kwargs: Any):
        fp.open_client_calls.append({"url": url, "signer": signer})
        return fp.client

    stac_mod.open_client = _open_client
    monkeypatch.setitem(sys.modules, "pyramids.stac", stac_mod)

    merge_mod = types.ModuleType("pyramids.dataset.merge")

    def _merge_rasters(src, dst, **kwargs):
        fp.merge_calls.append((list(src), str(dst), dict(kwargs)))
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
        epsg = next((v for k, v in fp.dataset_epsgs.items() if k in str(href)), 32630)
        shape = next(
            (v for k, v in fp.dataset_shapes.items() if k in str(href)), (1, 2, 2)
        )
        return _FakeDataset(href, epsg, shape)

    def _from_array(arr=None, *, geo_ref, no_data_value=None, path=None):
        epsg = getattr(geo_ref, "epsg", None)
        fp.create_calls.append({"no_data_value": no_data_value, "epsg": epsg})
        return _FakeDataset(epsg=epsg)

    dataset_mod.Dataset = type(
        "Dataset",
        (),
        {
            "read_file": staticmethod(_read_file),
            "from_array": staticmethod(_from_array),
        },
    )
    dataset_mod.DatasetCollection = _FakeDatasetCollection
    # The real value object: pyramids.base.georeference is not faked, and the
    # backend imports GeoReference from pyramids.dataset alongside Dataset.
    from pyramids.base.georeference import GeoReference

    dataset_mod.GeoReference = GeoReference
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
