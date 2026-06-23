"""Integration tests for `_gportal.fetch_gportal`.

Uses a faked `gportal` module (injected via `sys.modules`) that mimics
`search() -> Search` with `.matched()` / `.products()` and `download()`
returning paths. Credentials are passed explicitly to the branch via a
configured `JaxaAuth` so the fake can assert they flow through to
`gportal.download(username=, password=)` without the SDK's module-level
globals being touched.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from pydantic import SecretStr

from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
from earthlens.jaxa import JaxaAuth, JaxaCredentials
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
            {
                "target": list(target),
                "local_dir": local_dir,
                "username": username,
                "password": password,
            }
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


class _Product:
    """Tiny stand-in for `gportal.product.Product` — set `id` after construction."""

    id: str = ""
    data_path: str = ""
    data_url: str = ""


def _make_product(pid: str) -> _Product:
    """Build a `_Product` instance with the given id and derived paths."""
    p = _Product()
    p.id = pid
    p.data_path = f"path/{pid}"
    p.data_url = f"https://example.invalid/{pid}"
    return p


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


@pytest.fixture
def configured_auth():
    """Return a `JaxaAuth(protocol='gportal')` with creds already resolved."""
    auth = JaxaAuth(
        JaxaCredentials(
            gportal_username="alice",
            gportal_password=SecretStr("pytest-fixture-not-a-real-pw"),
        ),
        protocol="gportal",
    )
    auth.configure()
    return auth


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_search_and_download(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A non-zero match downloads the products and returns their paths."""
    products = [_make_product("A"), _make_product("B")]
    state, _ = _fake_gportal(
        monkeypatch, matched=2, products=products, write_files=True
    )
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa._gportal import fetch_gportal

    written = fetch_gportal(
        dataset=ds,
        space=space,
        time=time,
        auth=configured_auth,
        out_dir=tmp_path,
    )
    assert sorted(p.name for p in written) == ["A.dat", "B.dat"]
    # Search args translated correctly
    assert state["search_calls"][0]["dataset_ids"] == ["10003001"]
    assert state["search_calls"][0]["bbox"] == [138.0, 35.0, 139.0, 36.0]
    assert state["search_calls"][0]["start_time"].startswith("2024-01-01")
    assert state["search_calls"][0]["end_time"].startswith("2024-01-02")
    # Credentials flowed through to download() as kwargs
    call = state["download_calls"][0]
    assert call["username"] == "alice"
    assert call["password"] == "pytest-fixture-not-a-real-pw"


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_zero_matches_returns_empty(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """Zero matches returns `[]`, not an error (empty AOI/time is normal)."""
    _fake_gportal(monkeypatch, matched=0, products=[], write_files=False)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa._gportal import fetch_gportal

    written = fetch_gportal(
        dataset=ds,
        space=space,
        time=time,
        auth=configured_auth,
        out_dir=tmp_path,
    )
    assert written == []


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_download_returns_single_str_normalised(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A single-target `download` may return `str`; the branch normalises to a list."""
    state, fake = _fake_gportal(
        monkeypatch, matched=1, products=[_make_product("A")], write_files=True
    )

    def _download_single(target, local_dir=".", username=None, password=None):
        state["download_calls"].append(
            {
                "target": list(target),
                "username": username,
                "password": password,
            }
        )
        Path(local_dir, "A.dat").write_bytes(b"x")
        return str(Path(local_dir) / "A.dat")  # not a list

    fake.download = _download_single  # type: ignore[attr-defined]
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa._gportal import fetch_gportal

    written = fetch_gportal(
        dataset=ds,
        space=space,
        time=time,
        auth=configured_auth,
        out_dir=tmp_path,
    )
    assert len(written) == 1 and written[0].name == "A.dat"


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_unconfigured_auth_raises(monkeypatch, tmp_path, extents) -> None:
    """Calling fetch_gportal with an unconfigured auth raises clearly."""
    _fake_gportal(monkeypatch, matched=0, products=[], write_files=False)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    auth = JaxaAuth(JaxaCredentials(), protocol="gportal")  # not configured
    from earthlens.jaxa._gportal import fetch_gportal
    from earthlens.jaxa.auth import AuthenticationError

    with pytest.raises(AuthenticationError, match="not resolved"):
        fetch_gportal(
            dataset=ds,
            space=space,
            time=time,
            auth=auth,
            out_dir=tmp_path,
        )
