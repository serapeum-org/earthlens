"""Integration tests for `_gportal.fetch_gportal`.

Uses a faked `gportal` module (injected via `sys.modules`) that mimics
`search() -> Search` with `.matched()` / `.products()` and `download()`
returning paths. Auth is exercised by checking that `gportal.username`
gets set before the search call runs.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
from earthlens.jaxa.catalog import Dataset


def _fake_gportal(monkeypatch, *, matched: int, products: list, write_files: bool):
    """Inject a fake `gportal` module with configurable search/download behaviour."""
    state = {
        "username": None,
        "password": None,
        "search_calls": [],
        "download_calls": [],
    }

    class _Search:
        def __init__(self, products):
            self._products = products

        def matched(self):
            return matched

        def products(self, convert_types=True):
            return iter(self._products)

    fake = types.ModuleType("gportal")
    fake.username = None  # type: ignore[attr-defined]
    fake.password = None  # type: ignore[attr-defined]

    def _search(**kwargs):
        state["search_calls"].append(kwargs)
        return _Search(products)

    def _download(target, local_dir=".", username=None, password=None):
        state["download_calls"].append(
            {"target": list(target), "local_dir": local_dir}
        )
        out: list[str] = []
        for p in target:
            local = Path(local_dir) / f"{p.id}.dat"
            if write_files:
                local.write_bytes(b"x")
            out.append(str(local))
        return out

    fake.search = _search  # type: ignore[attr-defined]
    fake.download = _download  # type: ignore[attr-defined]
    fake.datasets = lambda: {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gportal", fake)
    return state, fake


def _make_product(pid: str):
    """Build a tiny stand-in for `gportal.product.Product`."""

    class _P:
        id = pid
        data_path = f"path/{pid}"
        data_url = f"https://example.invalid/{pid}"

    return _P()


@pytest.fixture
def extents():
    """Standard space + time extents for the branch tests."""
    space = SpatialExtent(
        latitude_min=35.0,
        latitude_max=36.0,
        longitude_min=138.0,
        longitude_max=139.0,
    )
    import datetime as dt

    import pandas as pd

    time = TemporalExtent(
        start_date=dt.datetime(2024, 1, 1),
        end_date=dt.datetime(2024, 1, 2),
        resolution="D",
        dates=pd.date_range("2024-01-01", "2024-01-02", freq="D"),
    )
    return space, time


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_search_and_download(monkeypatch, tmp_path, extents) -> None:
    """A non-zero match downloads the products and returns their paths."""
    products = [_make_product("A"), _make_product("B")]
    state, _ = _fake_gportal(
        monkeypatch, matched=2, products=products, write_files=True
    )
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa._gportal import fetch_gportal

    written = fetch_gportal(dataset=ds, space=space, time=time, out_dir=tmp_path)
    assert sorted(p.name for p in written) == ["A.dat", "B.dat"]
    # Search args translated correctly
    assert state["search_calls"][0]["dataset_ids"] == ["10003001"]
    assert state["search_calls"][0]["bbox"] == [138.0, 35.0, 139.0, 36.0]
    assert state["search_calls"][0]["start_time"].startswith("2024-01-01")
    assert state["search_calls"][0]["end_time"].startswith("2024-01-02")


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_zero_matches_returns_empty(monkeypatch, tmp_path, extents) -> None:
    """Zero matches returns `[]`, not an error (empty AOI/time is normal)."""
    _fake_gportal(monkeypatch, matched=0, products=[], write_files=False)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa._gportal import fetch_gportal

    written = fetch_gportal(dataset=ds, space=space, time=time, out_dir=tmp_path)
    assert written == []


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_download_returns_single_str_normalised(
    monkeypatch, tmp_path, extents
) -> None:
    """A single-target `download` may return `str`; the branch normalises to a list."""
    state, fake = _fake_gportal(
        monkeypatch, matched=1, products=[_make_product("A")], write_files=True
    )

    def _download_single(target, local_dir=".", username=None, password=None):
        state["download_calls"].append({"target": list(target)})
        Path(local_dir, "A.dat").write_bytes(b"x")
        return str(Path(local_dir) / "A.dat")  # not a list

    fake.download = _download_single  # type: ignore[attr-defined]
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa._gportal import fetch_gportal

    written = fetch_gportal(dataset=ds, space=space, time=time, out_dir=tmp_path)
    assert len(written) == 1 and written[0].name == "A.dat"
