"""Shared fixtures and offline fakes for the WorldPop backend tests.

No test here touches the network: `patch_http` swaps `requests.get` for a
dispatcher that returns canned REST JSON for `/rest/data/…` queries and the
bytes of a tiny real WGS84 GeoTIFF for `.tif` URLs, and `fake_worldpoppy`
injects a stub `worldpoppy` module backed by the same tiny GeoTIFF.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
import requests
from pyramids.dataset import Dataset, GeoReference

#: The WGS84 extent the tiny test GeoTIFF covers (a Kenya-sized box).
TIF_WEST, TIF_SOUTH, TIF_EAST, TIF_NORTH = 33.9, -4.7, 41.9, 5.0

#: The 18 WorldPop age-band lower bounds (0 = <1, 1 = 1–4, then 5-year bands).
AGE_BANDS: tuple[int, ...] = (
    0,
    1,
    5,
    10,
    15,
    20,
    25,
    30,
    35,
    40,
    45,
    50,
    55,
    60,
    65,
    70,
    75,
    80,
)


@pytest.fixture(scope="session")
def tiny_tif_bytes(tmp_path_factory) -> bytes:
    """Bytes of a tiny WGS84 GeoTIFF covering the Kenya-sized test extent."""
    path = tmp_path_factory.mktemp("wp_tif") / "tiny.tif"
    rows = cols = 20
    arr = np.arange(rows * cols, dtype="float64").reshape(rows, cols) + 1.0
    px = (TIF_EAST - TIF_WEST) / cols
    py = (TIF_NORTH - TIF_SOUTH) / rows
    geo = (TIF_WEST, px, 0.0, TIF_NORTH, 0.0, -py)
    Dataset.from_array(arr=arr, geo_ref=GeoReference(geo=geo, epsg=4326)).to_file(
        str(path)
    )
    return path.read_bytes()


def pop_records(iso3: str = "ken", years=range(2000, 2021)) -> list[dict]:
    """Build canned `pop/wpgp` REST records (one file per year)."""
    base = "https://data.worldpop.org/GIS/Population/Global_2000_2020"
    return [
        {
            "popyear": str(year),
            "citation": "WorldPop (www.worldpop.org). CC-BY-4.0.",
            "license": "https://hub.worldpop.org/data/licence.txt",
            "doi": "10.5258/SOTON/WP00645",
            "files": [f"{base}/{year}/{iso3.upper()}/{iso3}_ppp_{year}.tif"],
        }
        for year in years
    ]


def age_records(iso3: str = "ken", years=(2020,)) -> list[dict]:
    """Build canned `age_structures/aswpgp` records (36 cohort files per year)."""
    base = "https://data.worldpop.org/GIS/AgeSex_structures/Global_2000_2020"
    out = []
    for year in years:
        files = [
            f"{base}/{year}/{iso3.upper()}/{iso3}_{sex}_{band}_{year}.tif"
            for sex in ("m", "f")
            for band in AGE_BANDS
        ]
        out.append({"popyear": str(year), "files": files})
    return out


class _FakeResponse:
    """Minimal stand-in for `requests.Response` returning canned data."""

    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, *, json_data: object | None = None, content: bytes = b""):
        self._json = json_data
        self.content = content

    def json(self) -> object:
        """Return the canned decoded JSON body."""
        return self._json

    def raise_for_status(self) -> None:
        """Raise `HTTPError` when neither JSON nor content was supplied (a 404)."""
        if self._json is None and not self.content:
            raise requests.HTTPError("404 Not Found")

    def close(self) -> None:
        """No-op — the fake holds no socket."""

    def iter_content(self, chunk_size=None):
        """Yield the canned body in one chunk, as a streamed response would."""
        yield self.content


@pytest.fixture
def patch_http(monkeypatch, tiny_tif_bytes):
    """Return an installer that routes `requests.get` to canned REST + tiny tifs.

    Call the returned function with the records list the REST query should
    return; `.tif` URLs always yield the tiny GeoTIFF bytes.
    """

    def _install(records: list[dict]) -> None:
        def fake_get(url, params=None, timeout=None, **kwargs):
            if "/rest/data/" in url:
                return _FakeResponse(json_data={"data": records})
            if url.endswith(".tif"):
                return _FakeResponse(content=tiny_tif_bytes)
            raise AssertionError(f"unexpected URL in test: {url}")

        monkeypatch.setattr(requests, "get", fake_get)

    return _install


@pytest.fixture
def fake_worldpoppy(monkeypatch, tiny_tif_bytes, tmp_path):
    """Inject a stub `worldpoppy` module whose cache holds tiny pop GeoTIFFs."""
    cache = tmp_path / "wp_cache"
    cache.mkdir()

    def wp_raster(product_name, aoi, years, download_dry_run=False):
        for iso3 in aoi:
            for year in years:
                (cache / f"{iso3.lower()}_ppp_{year}.tif").write_bytes(tiny_tif_bytes)
        return "FAKE_XARRAY_DATAARRAY"

    def get_cache_dir():
        return str(cache)

    module = types.ModuleType("worldpoppy")
    module.wp_raster = wp_raster
    module.get_cache_dir = get_cache_dir
    monkeypatch.setitem(sys.modules, "worldpoppy", module)
    return cache


@pytest.fixture
def wp_kwargs(tmp_path):
    """Return a builder for valid `WorldPop(...)` kwargs over the test extent."""

    def _make(**overrides):
        kwargs = dict(
            variables=["pop"],
            start="2020",
            end="2020",
            lat_lim=[-4.0, 4.0],
            lon_lim=[34.0, 41.0],
            fmt="%Y",
            path=str(tmp_path / "out"),
            aoi="KEN",
        )
        kwargs.update(overrides)
        return kwargs

    return _make
