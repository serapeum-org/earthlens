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


def _fake_gportal_raising(monkeypatch, exc: BaseException) -> None:
    """Inject a fake `gportal` whose catalogue search raises `exc` from matched()."""

    class _Search:
        def matched(self):
            raise exc

        def products(self, convert_types=True):
            raise exc

    fake = types.ModuleType("gportal")
    fake.username = None  # type: ignore[attr-defined]
    fake.password = None  # type: ignore[attr-defined]
    fake.search = lambda **kwargs: _Search()  # type: ignore[attr-defined]
    fake.download = lambda *a, **k: []  # type: ignore[attr-defined]
    fake.datasets = lambda: {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gportal", fake)


class _RaisingDownload:
    """Callable stand-in for `gportal.download` that always raises `exc`."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __call__(self, *args, **kwargs):
        raise self._exc


def _probe_must_not_run() -> int | None:
    """Stand-in probe that fails the test if the classification branch consults it."""
    raise AssertionError("the endpoint probe must not run on this path")


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_http_503_raises_typed_unavailable(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A 503 HTTPError from the SDK becomes GportalUnavailableError carrying the status."""
    import requests

    resp = type("_Resp", (), {"status_code": 503})()
    _fake_gportal_raising(
        monkeypatch,
        requests.HTTPError("503 Server Error: Service Unavailable", response=resp),
    )
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalUnavailableError
    from earthlens.jaxa._gportal import fetch_gportal

    with pytest.raises(GportalUnavailableError, match="down or throttling") as excinfo:
        fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert excinfo.value.status_code == 503
    assert "G-Portal" in str(excinfo.value)


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_html_body_jsondecode_transient_when_probe_cannot_confirm(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A JSONDecodeError stays a transient outage when the probe cannot confirm retirement."""
    import requests

    from earthlens.jaxa import _gportal

    _fake_gportal_raising(
        monkeypatch,
        requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0),
    )
    # Probe cannot reach the host (returns None) -> the ambiguous HTML body is
    # classified as a transient outage, not a confirmed retirement.
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", lambda: None)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalEndpointRetiredError, GportalUnavailableError

    with pytest.raises(GportalUnavailableError, match="down or throttling") as excinfo:
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert not isinstance(excinfo.value, GportalEndpointRetiredError)
    assert excinfo.value.status_code is None
    assert isinstance(excinfo.value.__cause__, requests.exceptions.JSONDecodeError)


@pytest.mark.jaxa
@pytest.mark.integration
@pytest.mark.parametrize("probe_status", [301, 308, 404, 410])
def test_gportal_search_retired_endpoint_raises_retired_error(
    monkeypatch, tmp_path, extents, configured_auth, probe_status
) -> None:
    """A statusless search failure plus any gone-status probe becomes GportalEndpointRetiredError."""
    import requests

    from earthlens.jaxa import _gportal

    _fake_gportal_raising(
        monkeypatch,
        requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0),
    )
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", lambda: probe_status)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalEndpointRetiredError, GportalUnavailableError

    with pytest.raises(
        GportalEndpointRetiredError, match="no longer exists"
    ) as excinfo:
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert isinstance(excinfo.value, GportalUnavailableError)
    assert excinfo.value.status_code == probe_status
    assert "#1415" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, requests.exceptions.JSONDecodeError)


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_search_probe_non_gone_status_stays_transient(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A statusless failure whose probe answers a present-but-not-gone status (500) stays transient."""
    import requests

    from earthlens.jaxa import _gportal

    _fake_gportal_raising(
        monkeypatch,
        requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0),
    )
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", lambda: 500)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalEndpointRetiredError, GportalUnavailableError

    with pytest.raises(GportalUnavailableError, match="down or throttling") as excinfo:
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert not isinstance(excinfo.value, GportalEndpointRetiredError)


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_retired_not_found_message_omits_redirect_claim(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A 404 probe is reported as a gone path, not as a redirect to the landing page."""
    import requests

    from earthlens.jaxa import _gportal

    _fake_gportal_raising(
        monkeypatch,
        requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0),
    )
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", lambda: 404)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalEndpointRetiredError

    with pytest.raises(GportalEndpointRetiredError) as excinfo:
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    message = str(excinfo.value)
    assert "HTTP 404" in message
    assert "redirects to the portal landing page" not in message
    assert "the path no longer exists" in message


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_search_live_endpoint_stays_transient(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A JSONDecodeError stays transient when the probe finds the service URL alive (200)."""
    import requests

    from earthlens.jaxa import _gportal

    _fake_gportal_raising(
        monkeypatch,
        requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0),
    )
    # The service URL answers 200 (no redirect) -> the endpoint exists; the
    # parse failure was a transient blip, not a retirement.
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", lambda: 200)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalEndpointRetiredError, GportalUnavailableError

    with pytest.raises(GportalUnavailableError, match="down or throttling") as excinfo:
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert not isinstance(excinfo.value, GportalEndpointRetiredError)


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_search_5xx_does_not_probe(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A 5xx carries a status, so it is classified transient without probing the endpoint."""
    import requests

    from earthlens.jaxa import _gportal

    resp = type("_Resp", (), {"status_code": 503})()
    _fake_gportal_raising(
        monkeypatch,
        requests.HTTPError("503 Server Error: Service Unavailable", response=resp),
    )
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", _probe_must_not_run)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalEndpointRetiredError, GportalUnavailableError

    with pytest.raises(GportalUnavailableError, match="down or throttling") as excinfo:
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert not isinstance(excinfo.value, GportalEndpointRetiredError)
    assert excinfo.value.status_code == 503


@pytest.mark.jaxa
@pytest.mark.integration
@pytest.mark.parametrize("probe_status", [302, 303, 307])
def test_gportal_search_temporary_redirect_probe_stays_transient(
    monkeypatch, tmp_path, extents, configured_auth, probe_status
) -> None:
    """A temporary-redirect probe status is not in the gone-set, so it stays transient."""
    import requests

    from earthlens.jaxa import _gportal

    _fake_gportal_raising(
        monkeypatch,
        requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0),
    )
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", lambda: probe_status)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalEndpointRetiredError, GportalUnavailableError

    with pytest.raises(GportalUnavailableError, match="down or throttling") as excinfo:
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert not isinstance(excinfo.value, GportalEndpointRetiredError)


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_download_drop_raises_typed_unavailable(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """An SSH drop during the SFTP download becomes GportalUnavailableError."""
    import paramiko

    _, fake = _fake_gportal(
        monkeypatch, matched=1, products=[_make_product("A")], write_files=False
    )
    fake.download = _RaisingDownload(paramiko.SSHException("Server connection dropped"))
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalUnavailableError
    from earthlens.jaxa._gportal import fetch_gportal

    with pytest.raises(GportalUnavailableError, match="SFTP") as excinfo:
        fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert excinfo.value.status_code is None
    assert isinstance(excinfo.value.__cause__, paramiko.SSHException)


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_download_auth_error_is_not_relabelled(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A credential rejection during download propagates, not wrapped as unavailable."""
    import paramiko

    _, fake = _fake_gportal(
        monkeypatch, matched=1, products=[_make_product("A")], write_files=False
    )
    fake.download = _RaisingDownload(paramiko.AuthenticationException("Auth failed"))
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa._gportal import fetch_gportal

    with pytest.raises(paramiko.AuthenticationException):
        fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_download_connection_reset_raises_typed_unavailable(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A reset socket during the SFTP download becomes GportalUnavailableError."""
    _, fake = _fake_gportal(
        monkeypatch, matched=1, products=[_make_product("A")], write_files=False
    )
    fake.download = _RaisingDownload(ConnectionResetError("Connection reset by peer"))
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalUnavailableError
    from earthlens.jaxa._gportal import fetch_gportal

    with pytest.raises(GportalUnavailableError, match="SFTP") as excinfo:
        fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert isinstance(excinfo.value.__cause__, ConnectionResetError)


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_download_local_oserror_is_not_relabelled(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A local filesystem OSError during the write propagates, not wrapped as unavailable."""
    _, fake = _fake_gportal(
        monkeypatch, matched=1, products=[_make_product("A")], write_files=False
    )
    fake.download = _RaisingDownload(OSError(28, "No space left on device"))
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa._gportal import fetch_gportal

    with pytest.raises(OSError, match="No space left") as excinfo:
        fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert excinfo.type is OSError


@pytest.mark.jaxa
@pytest.mark.unit
def test_probe_search_endpoint_status_reads_status_without_following_redirects(
    monkeypatch,
) -> None:
    """The probe HEADs the service URL with redirects off and returns the raw status."""
    import requests

    from earthlens.jaxa import _gportal

    fake = types.ModuleType("gportal")
    fake.http_client = types.SimpleNamespace(  # type: ignore[attr-defined]
        BASE_URL="https://gportal.jaxa.jp/"
    )
    monkeypatch.setitem(sys.modules, "gportal", fake)
    monkeypatch.setitem(sys.modules, "gportal.http_client", fake.http_client)

    captured: dict = {}

    def _head(url, params=None, allow_redirects=True, timeout=None):
        captured.update(
            url=url, params=params, allow_redirects=allow_redirects, timeout=timeout
        )
        return type("_Resp", (), {"status_code": 301})()

    monkeypatch.setattr(requests, "head", _head)
    assert _gportal._probe_search_endpoint_status() == 301
    assert captured["url"] == "https://gportal.jaxa.jp/csw/csw"
    assert captured["allow_redirects"] is False
    assert captured["params"]["request"] == "GetCapabilities"


@pytest.mark.jaxa
@pytest.mark.unit
def test_probe_search_endpoint_status_matches_sdk_path_under_base_with_segment(
    monkeypatch,
) -> None:
    """The probe resolves the same root-absolute /csw/csw the SDK hits, even if BASE_URL has a path."""
    import requests

    from earthlens.jaxa import _gportal

    fake = types.ModuleType("gportal")
    fake.http_client = types.SimpleNamespace(  # type: ignore[attr-defined]
        BASE_URL="https://gportal.jaxa.jp/api/"
    )
    monkeypatch.setitem(sys.modules, "gportal", fake)
    monkeypatch.setitem(sys.modules, "gportal.http_client", fake.http_client)

    captured: dict = {}

    def _head(url, params=None, allow_redirects=True, timeout=None):
        captured["url"] = url
        return type("_Resp", (), {"status_code": 301})()

    monkeypatch.setattr(requests, "head", _head)
    _gportal._probe_search_endpoint_status()
    assert captured["url"] == "https://gportal.jaxa.jp/csw/csw"


@pytest.mark.jaxa
@pytest.mark.unit
def test_probe_search_endpoint_status_returns_none_on_transport_failure(
    monkeypatch,
) -> None:
    """The probe returns None (treat as transient) when it cannot reach the host."""
    import requests

    from earthlens.jaxa import _gportal

    fake = types.ModuleType("gportal")
    fake.http_client = types.SimpleNamespace(  # type: ignore[attr-defined]
        BASE_URL="https://gportal.jaxa.jp/"
    )
    monkeypatch.setitem(sys.modules, "gportal", fake)
    monkeypatch.setitem(sys.modules, "gportal.http_client", fake.http_client)

    def _head(*a, **k):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "head", _head)
    assert _gportal._probe_search_endpoint_status() is None


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_search_plain_4xx_propagates_without_probing(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A plain 4xx (not 404/410) propagates raw and never consults the endpoint probe."""
    import requests

    from earthlens.jaxa import _gportal

    resp = type("_Resp", (), {"status_code": 400})()
    _fake_gportal_raising(
        monkeypatch, requests.HTTPError("400 Client Error: Bad Request", response=resp)
    )
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", _probe_must_not_run)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")

    with pytest.raises(requests.HTTPError, match="400"):
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )


@pytest.mark.jaxa
@pytest.mark.integration
def test_gportal_search_direct_404_live_service_propagates(
    monkeypatch, tmp_path, extents, configured_auth
) -> None:
    """A direct 404 whose service URL is still alive is a resource error and propagates raw."""
    import requests

    from earthlens.jaxa import _gportal

    resp = type("_Resp", (), {"status_code": 404})()
    _fake_gportal_raising(
        monkeypatch, requests.HTTPError("404 Client Error: Not Found", response=resp)
    )
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", lambda: 200)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")

    with pytest.raises(requests.HTTPError, match="404"):
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )


@pytest.mark.jaxa
@pytest.mark.integration
@pytest.mark.parametrize("search_status", [404, 410])
def test_gportal_search_direct_404_410_retires_when_probe_confirms(
    monkeypatch, tmp_path, extents, configured_auth, search_status
) -> None:
    """A direct 404/410 on the search becomes a retirement when the probe confirms the endpoint is gone."""
    import requests

    from earthlens.jaxa import _gportal

    resp = type("_Resp", (), {"status_code": search_status})()
    _fake_gportal_raising(
        monkeypatch, requests.HTTPError(f"{search_status} Client Error", response=resp)
    )
    monkeypatch.setattr(_gportal, "_probe_search_endpoint_status", lambda: 410)
    space, time = extents
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    from earthlens.jaxa import GportalEndpointRetiredError

    with pytest.raises(
        GportalEndpointRetiredError, match="no longer exists"
    ) as excinfo:
        _gportal.fetch_gportal(
            dataset=ds, space=space, time=time, auth=configured_auth, out_dir=tmp_path
        )
    assert excinfo.value.status_code == 410
