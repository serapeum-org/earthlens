"""Tests for bounded Data Tailor HTTP (#1146) and FAILED-outcome retry (#1145)."""

from __future__ import annotations

import pytest

from earthlens.eumetsat import EUMETSAT, TailorConfig
from earthlens.eumetsat import backend as be

from .conftest import _FakeCustomisation, _FakeProduct

pytestmark = pytest.mark.eumetsat

_CREDS = {"consumer_key": "k", "consumer_secret": "s"}
_OLCI = "EO:EUM:DAT:0409"  # s3-olci-l1-efr, tailor_product_type OLL1EFR
_STALE = "2026 ERROR [Errno 116] Stale file handle: /var/dtws/users/x/OUTPUTS/y"


def _backend(fake_eumdac, tmp_path, variables, **kwargs):
    """Build an EUMETSAT backend wired to the fake `eumdac`."""
    return EUMETSAT(
        start="2024-01-01",
        end="2024-01-02",
        variables=variables,
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        **_CREDS,
        **kwargs,
    )


class _BoundCheckProduct(_FakeProduct):
    """A fake product that records whether HTTP was bounded when it was opened."""

    def __init__(self, product_id: str, payload: bytes = b"X") -> None:
        super().__init__(product_id, payload)
        self.opened_bounded: bool | None = None

    def open(self, entry=None, chunk=None, custom_headers=None):
        import requests

        self.opened_bounded = getattr(
            requests.sessions.Session.request, "_earthlens_bounded", False
        )
        return super().open(entry, chunk, custom_headers)


# --- #1146: bounded HTTP ----------------------------------------------------


def test_bounded_http_injects_and_preserves_timeout(monkeypatch):
    """`_bounded_http` injects the default timeout, but preserves an explicit one."""
    import requests

    calls: list = []

    def _recorder(self, *args, **kwargs):
        calls.append(kwargs.get("timeout", "MISSING"))
        return "resp"

    monkeypatch.setattr(requests.sessions.Session, "request", _recorder)
    with be._bounded_http():
        requests.sessions.Session.request(object(), "GET", "http://x")
        requests.sessions.Session.request(object(), "GET", "http://x", timeout=5)
    assert calls == [be.TAILOR_HTTP_TIMEOUT_S, 5]


def test_bounded_http_reentrant_and_restored():
    """Nested `_bounded_http` reuses the outer wrap and restores on exit."""
    import requests

    original = requests.sessions.Session.request
    with be._bounded_http():
        wrapped = requests.sessions.Session.request
        assert getattr(wrapped, "_earthlens_bounded", False) is True
        with be._bounded_http():
            assert requests.sessions.Session.request is wrapped  # no double-wrap
        assert requests.sessions.Session.request is wrapped  # inner exit is a no-op
    assert requests.sessions.Session.request is original  # fully restored


def test_bounded_http_restores_on_exception():
    """`_bounded_http` restores `Session.request` even if the block raises."""
    import requests

    original = requests.sessions.Session.request
    with pytest.raises(RuntimeError, match="boom"):
        with be._bounded_http():
            raise RuntimeError("boom")
    assert requests.sessions.Session.request is original  # not leaked


def test_network_methods_are_bounded():
    """The three networked backend methods carry the timeout decorator."""
    for name in ("_search", "_fetch", "_tailor_one"):
        assert hasattr(getattr(EUMETSAT, name), "__wrapped__"), name


def test_fetch_bounds_its_http_calls(fake_eumdac, tmp_path):
    """The native `_fetch` opens products under the timeout wrapper (#1146)."""
    product = _BoundCheckProduct("p.nc")
    fake_eumdac.store.products_for[_OLCI] = [product]
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    backend._fetch(backend._search())
    assert product.opened_bounded is True


def test_poll_rides_out_transient_http_error_then_returns(monkeypatch):
    """A transient poll HTTP error is not fatal; polling continues to DONE."""
    import requests

    monkeypatch.setattr(be, "TAILOR_POLL_INITIAL_S", 0.0)
    cust = _FakeCustomisation(
        statuses=[
            requests.exceptions.ReadTimeout("read timed out"),
            requests.exceptions.ReadTimeout("read timed out"),
            "DONE",
        ]
    )
    assert EUMETSAT._poll_customisation(cust) == "DONE"


def test_poll_permanent_error_propagates_fast(monkeypatch):
    """A permanent (non-transient) poll error fails fast, not after the budget."""
    import requests

    monkeypatch.setattr(be, "TAILOR_POLL_TIMEOUT_S", 9999.0)  # a stall would hang
    cust = _FakeCustomisation(statuses=[requests.exceptions.HTTPError("404 Not Found")])
    with pytest.raises(requests.exceptions.HTTPError):
        EUMETSAT._poll_customisation(cust)


def test_poll_wall_clock_timeout_on_persistent_http_error(monkeypatch):
    """A persistently unreadable status times out on the wall-clock budget."""
    import requests

    monkeypatch.setattr(be, "TAILOR_POLL_TIMEOUT_S", 0.0)
    cust = _FakeCustomisation(statuses=[requests.exceptions.ConnectionError("boom")])
    with pytest.raises(TimeoutError, match="did not finish"):
        EUMETSAT._poll_customisation(cust)


# --- #1145: retry a FAILED customisation on an infrastructure fault ---------


def test_failed_infra_marker_resubmits_deleting_before_retry(
    fake_eumdac, tmp_path, monkeypatch
):
    """A stale-NFS-handle FAILED is resubmitted, the abandoned job deleted first."""
    monkeypatch.setattr(be, "TAILOR_SUBMIT_BACKOFF_S", 0.0)
    monkeypatch.setattr(be, "TAILOR_POLL_INITIAL_S", 0.0)
    failed = _FakeCustomisation(statuses=["FAILED"], logfile=_STALE, name="c1")
    done = _FakeCustomisation(statuses=["DONE"], outputs=["o.tif"], name="c2")
    fake_eumdac.tailor.customisations = [failed, done]
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(progress_bar=False, tailor=TailorConfig())
    assert [p.name for p in paths] == ["o.tif"]
    # the abandoned job is deleted BEFORE the resubmit — no quota leak (G7)
    assert fake_eumdac.tailor.events == [
        ("submit", "c1"),
        ("delete", "c1"),
        ("submit", "c2"),
        ("delete", "c2"),
    ]
    assert failed.deleted == 1 and done.deleted == 1


def test_failed_bad_request_fails_fast_without_retry(fake_eumdac, tmp_path):
    """A FAILED with no infrastructure marker raises on the first attempt."""
    fake_eumdac.tailor.customisation = _FakeCustomisation(
        statuses=["FAILED"], logfile="invalid product-ID 'X'", name="c1"
    )
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(RuntimeError, match="FAILED"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert len(fake_eumdac.tailor.submitted) == 1


def test_failed_infra_marker_exhausts_retry_budget(fake_eumdac, tmp_path, monkeypatch):
    """Repeated infrastructure FAILEDs exhaust the budget and then raise."""
    monkeypatch.setattr(be, "TAILOR_SUBMIT_BACKOFF_S", 0.0)
    monkeypatch.setattr(be, "TAILOR_POLL_INITIAL_S", 0.0)
    custs = [
        _FakeCustomisation(statuses=["FAILED"], logfile=_STALE, name=f"c{i}")
        for i in range(be.TAILOR_SUBMIT_RETRIES)
    ]
    fake_eumdac.tailor.customisations = list(custs)
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(RuntimeError, match="FAILED"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert len(fake_eumdac.tailor.submitted) == be.TAILOR_SUBMIT_RETRIES
    assert all(c.deleted == 1 for c in custs)


def test_killed_is_not_retried_even_with_infra_log(fake_eumdac, tmp_path):
    """A KILLED job is never retried, even if its log looks like an infra fault."""
    fake_eumdac.tailor.customisation = _FakeCustomisation(
        statuses=["KILLED"], logfile=_STALE, name="c1"
    )
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(RuntimeError, match="KILLED"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert len(fake_eumdac.tailor.submitted) == 1
