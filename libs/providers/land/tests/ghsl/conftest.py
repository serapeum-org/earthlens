"""Shared fixtures + fakes for the GHSL backend tests (no network)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pytest
import requests
from pyramids.dataset import Dataset, GeoReference

#: Mollweide (ESRI:54009) geotransform inside the verified R6_C18 tile extent
#: ([-1041000, 3000000, -41000, 4000000]); 5x5 cells of 100 km.
_MOLLWEIDE_GEO = (-900000.0, 100000.0, 0.0, 3900000.0, 0.0, -100000.0)
#: WGS84 geotransform covering [-9, 30, -8, 31] (4x4 cells of 0.25 deg).
_WGS84_GEO = (-9.0, 0.25, 0.0, 31.0, 0.0, -0.25)


def make_tiny_tif(
    path: Path,
    *,
    epsg: int = 54009,
    geo: tuple[float, float, float, float, float, float] | None = None,
    values: np.ndarray | None = None,
    no_data: float = -200.0,
) -> Path:
    """Write a tiny real GeoTIFF (default 5x5 Mollweide) and return its path."""
    if geo is None:
        geo = _MOLLWEIDE_GEO if epsg == 54009 else _WGS84_GEO
    rows = 5 if epsg == 54009 else 4
    if values is None:
        values = np.arange(rows * rows, dtype="float32").reshape(rows, rows)
    dataset = Dataset.from_array(
        values,
        no_data_value=no_data,
        geo_ref=GeoReference(geo=geo, epsg=epsg),
    )
    dataset.to_file(str(path))
    return path


def zip_with_tif(tif_path: Path, zip_path: Path, *, sidecars: bool = True) -> Path:
    """Build a JRC-style `.zip` holding the `.tif` plus the PDF/xlsx sidecars."""
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(tif_path, arcname=Path(tif_path).name)
        if sidecars:
            archive.writestr("GHSL_Data_Package_2023_light.pdf", b"%PDF-1.4 fake")
            archive.writestr("input_metadata.xlsx", b"fake-xlsx-bytes")
    return zip_path


class _FakeResponse:
    """Minimal `requests.Response` stand-in for the fake session."""

    def __init__(self, content: bytes = b"", text: str = "", status: int = 200):
        self._content = content
        self.text = text
        self._status = status
        self.status_code = status
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        """Raise `HTTPError` when the route is marked as a 4xx/5xx."""
        if self._status >= 400:
            raise requests.HTTPError(f"{self._status} for fake url")

    def iter_content(self, chunk_size: int = 1) -> Any:
        """Yield the payload in `chunk_size` slices."""
        for start in range(0, len(self._content), max(chunk_size, 1)):
            yield self._content[start : start + chunk_size]

    def close(self) -> None:
        """No-op close (parity with `requests.Response`)."""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeSession:
    """`requests.Session` stand-in routing URLs to canned responses."""

    def __init__(self, routes: dict[str, _FakeResponse] | None = None):
        self.routes: dict[str, _FakeResponse] = routes or {}
        self.requested: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any):
        """Return the canned response for `url` (404 when unrouted)."""
        self.requested.append(url)
        self.calls.append(kwargs)
        return self.routes.get(url, _FakeResponse(status=404))

    def close(self) -> None:
        """No-op close (parity with `requests.Session`)."""


@pytest.fixture
def tiny_mollweide_tif(tmp_path: Path) -> Path:
    """A tiny real ESRI:54009 GeoTIFF inside the R6_C18 extent."""
    return make_tiny_tif(tmp_path / "tile_54009.tif", epsg=54009)


@pytest.fixture
def tiny_wgs84_tif(tmp_path: Path) -> Path:
    """A tiny real EPSG:4326 GeoTIFF over [-9, 30, -8, 31]."""
    return make_tiny_tif(tmp_path / "tile_4326.tif", epsg=4326)


@pytest.fixture
def make_response() -> Callable[..., _FakeResponse]:
    """Factory for `_FakeResponse` objects (content / text / status)."""
    return _FakeResponse


@pytest.fixture
def fake_session() -> Callable[[dict[str, _FakeResponse]], FakeSession]:
    """Factory building a `FakeSession` from a url→response route map."""
    return FakeSession
