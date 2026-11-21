"""Unit + integration tests for the EUMETSAT backend (mocked `eumdac`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.base import AuthenticationError
from earthlens.eumetsat import EUMETSAT, TailorConfig
from earthlens.eumetsat.catalog import DataStoreGroup

from .conftest import _FakeCustomisation, _FakeProduct

pytestmark = pytest.mark.eumetsat

_CREDS = {"consumer_key": "k", "consumer_secret": "s"}
_OLCI = "EO:EUM:DAT:0409"  # s3-olci-l1-efr, tailor_product_type OLL1EFR


def _make_backend(fake_eumdac, tmp_path, variables, **kwargs):
    """Build an EUMETSAT backend wired to the fake `eumdac` store."""
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


def test_construction_sets_output_kind_and_defers_auth(fake_eumdac, tmp_path):
    """Construction copies the row's output_kind but defers token minting."""
    backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    assert backend.OUTPUT_KIND == "raster"
    assert backend._auth.is_authenticated() is False, (
        "construction must not authenticate"
    )
    backend._auth.configure()
    assert backend._auth.is_authenticated() is True, "configure() mints the token"


def test_empty_variables_rejected(fake_eumdac, tmp_path):
    """An empty variables mapping is rejected at construction."""
    with pytest.raises(ValueError, match="non-empty"):
        _make_backend(fake_eumdac, tmp_path, {})


def test_unknown_collection_key_rejected(fake_eumdac, tmp_path):
    """An unknown collection key surfaces the catalog did-you-mean."""
    with pytest.raises(ValueError, match="Did you mean"):
        _make_backend(fake_eumdac, tmp_path, {"msg-hrsevir": ["x"]})


def test_group_kwarg_disambiguates(fake_eumdac, tmp_path):
    """A matching group= kwarg is accepted; a mismatch is rejected."""
    backend = _make_backend(
        fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]}, group="MSG"
    )
    assert backend._datasets[0].group is DataStoreGroup.MSG
    with pytest.raises(ValueError, match="not the requested group"):
        _make_backend(
            fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]}, group="MTG"
        )


def test_search_calls_collection_with_bbox_and_dates(fake_eumdac, tmp_path):
    """_search passes the W,S,E,N bbox string and dtstart/dtend datetimes."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("p1")]
    backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    products = backend._search()
    call = fake_eumdac.store.search_calls[0]
    assert call["collection"] == "EO:EUM:DAT:MSG:HRSEVIRI"
    assert call["bbox"] == "-1.0,50.0,1.0,52.0"  # W,S,E,N axis order (A1)
    assert call["dtstart"] == backend.time.start_date
    assert [p.id for p in products] == ["p1"]


def test_search_dtend_extends_to_end_of_day(fake_eumdac, tmp_path):
    """A same-day window is widened to 23:59:59.999999 so it is not zero-width."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("p1")]
    backend = EUMETSAT(
        start="2024-06-01",
        end="2024-06-01",
        variables={"msg-hrseviri": ["HRSEVIRI"]},
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        **_CREDS,
    )
    backend._search()
    call = fake_eumdac.store.search_calls[0]
    assert call["dtstart"] == backend.time.start_date
    assert call["dtend"] == backend.time.end_date.replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    assert call["dtstart"] < call["dtend"]  # window is non-empty


def test_search_dtend_with_time_of_day_is_not_widened(fake_eumdac, tmp_path):
    """An end carrying a time of day means that instant, not the end of its day."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("p1")]
    backend = EUMETSAT(
        start="2024-06-01 09:00",
        end="2024-06-01 09:09",
        fmt="%Y-%m-%d %H:%M",
        variables={"msg-hrseviri": ["HRSEVIRI"]},
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        **_CREDS,
    )
    backend._search()
    call = fake_eumdac.store.search_calls[0]
    assert call["dtend"] == backend.time.end_date
    assert call["dtend"].hour == 9, f"hour changed: {call['dtend']}"
    assert call["dtend"].minute == 9, f"minute changed: {call['dtend']}"


def test_search_dtend_explicit_midnight_is_not_widened(fake_eumdac, tmp_path):
    """An end typed as an explicit midnight instant means that instant."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("p1")]
    backend = EUMETSAT(
        start="2024-06-01 00:00",
        end="2024-06-02 00:00",
        fmt="%Y-%m-%d %H:%M",
        variables={"msg-hrseviri": ["HRSEVIRI"]},
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        **_CREDS,
    )
    backend._search()
    call = fake_eumdac.store.search_calls[0]
    assert call["dtend"] == backend.time.end_date, (
        f"an explicit '00:00' end must not be widened to end of day, got {call['dtend']}"
    )


def test_search_dtend_just_past_midnight_is_not_widened(fake_eumdac, tmp_path):
    """One second past midnight is a time of day, so it is left alone."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("p1")]
    backend = EUMETSAT(
        start="2024-06-01 00:00:00",
        end="2024-06-01 00:00:01",
        fmt="%Y-%m-%d %H:%M:%S",
        variables={"msg-hrseviri": ["HRSEVIRI"]},
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        **_CREDS,
    )
    backend._search()
    call = fake_eumdac.store.search_calls[0]
    assert call["dtend"] == backend.time.end_date, (
        f"a 00:00:01 end must not be widened, got {call['dtend']}"
    )


def test_search_dtend_late_in_the_day_is_passed_through(fake_eumdac, tmp_path):
    """An end already near end of day is used as given, not re-rounded."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("p1")]
    backend = EUMETSAT(
        start="2024-06-01 00:00:00",
        end="2024-06-01 23:59:59",
        fmt="%Y-%m-%d %H:%M:%S",
        variables={"msg-hrseviri": ["HRSEVIRI"]},
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        **_CREDS,
    )
    backend._search()
    call = fake_eumdac.store.search_calls[0]
    assert call["dtend"].microsecond == 0, (
        f"an explicit 23:59:59 end must keep its microseconds, got {call['dtend']}"
    )


def test_fetch_streams_each_product_to_disk(fake_eumdac, tmp_path):
    """_fetch writes one file per product, named by the product id."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [
        _FakeProduct("alpha", b"AAAA"),
        _FakeProduct("beta", b"BBBB"),
    ]
    backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    paths = backend.download(progress_bar=False)
    assert sorted(p.name for p in paths) == ["alpha", "beta"]
    assert (tmp_path / "alpha").read_bytes() == b"AAAA"


def test_fetch_sanitizes_product_id_with_path_separator(fake_eumdac, tmp_path):
    """A product id containing a path separator is written under root_dir, not outside it."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [
        _FakeProduct("../../escape.nat", b"X")
    ]
    backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    paths = backend.download(progress_bar=False)
    assert paths == [tmp_path / "escape.nat"]
    assert (tmp_path / "escape.nat").read_bytes() == b"X"
    assert paths[0].parent == tmp_path


def test_download_empty_search_returns_empty(fake_eumdac, tmp_path):
    """A search that matches nothing returns an empty list, no fetch."""
    backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    assert backend.download(progress_bar=False) == []


def test_multiple_datasets_searched(fake_eumdac, tmp_path):
    """A request naming two same-kind datasets searches both."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("a")]
    fake_eumdac.store.products_for["EO:EUM:DAT:0407"] = [_FakeProduct("b")]
    backend = _make_backend(
        fake_eumdac,
        tmp_path,
        {"msg-hrseviri": ["HRSEVIRI"], "s3-olci-l2-wfr": ["OL_2_WFR"]},
    )
    paths = backend.download(progress_bar=False)
    assert {p.name for p in paths} == {"a", "b"}
    assert len(fake_eumdac.store.search_calls) == 2


def test_aggregate_raises_not_implemented(fake_eumdac, tmp_path):
    """download(aggregate=...) still raises NotImplementedError (temporal reducer)."""
    backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    with pytest.raises(NotImplementedError, match="aggregate="):
        backend.download(aggregate=object())


def test_unify_output_kind_rejects_mixed(fake_eumdac, tmp_path):
    """A request mixing output kinds is rejected at construction."""
    from earthlens.eumetsat.catalog import EumetsatDataset

    raster = EumetsatDataset(collection_id="A", group="MSG", output_kind="raster")
    vector = EumetsatDataset(collection_id="B", group="MSG", output_kind="vector")
    with pytest.raises(ValueError, match="must share one"):
        EUMETSAT._unify_output_kind([raster, vector])


def test_api_composes_search_and_fetch(fake_eumdac, tmp_path):
    """The legacy `_api` hook composes search + fetch like download()."""
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("z")]
    backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    paths = backend._api()
    assert [p.name for p in paths] == ["z"]


def test_search_without_eumdac_raises_friendly_import_error(tmp_path, monkeypatch):
    """A missing `eumdac` surfaces a friendly ImportError naming the extra."""
    import sys

    # Construct with a fake so auth/catalog resolve, then hide eumdac for _search.
    from .conftest import _FakeEumdac

    fake = _FakeEumdac()
    monkeypatch.setitem(sys.modules, "eumdac", fake)
    backend = _make_backend(fake, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    monkeypatch.setitem(sys.modules, "eumdac", None)  # force ImportError on re-import
    with pytest.raises(ImportError, match=r"earthlens\[eumetsat\]"):
        backend._search()


class _RaiseFromCall:
    """A `_fetch_one` / `_tailor_one` stand-in raising `exc` from `from_call`.

    Holds its own call counter; `from_call=2` fails the second product (a
    partial failure), `from_call=1` fails every product (a whole-service
    failure).
    """

    def __init__(self, real, exc, from_call=2):
        self._real = real
        self._exc = exc
        self._from_call = from_call
        self._calls = 0

    def __call__(self, *args, **kwargs):
        """Delegate until `from_call`, then raise `exc` on that call onward."""
        self._calls += 1
        if self._calls >= self._from_call:
            raise self._exc
        return self._real(*args, **kwargs)


class TestErrorsPolicy:
    """`errors=` partial-failure policy across the independent products."""

    def _two_product_backend(
        self, fake_eumdac, tmp_path, monkeypatch, exc, from_call=2
    ):
        """A native backend over two products, raising `exc` from `from_call`."""
        fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [
            _FakeProduct("alpha", b"AAAA"),
            _FakeProduct("beta", b"BBBB"),
        ]
        backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
        monkeypatch.setattr(
            backend, "_fetch_one", _RaiseFromCall(backend._fetch_one, exc, from_call)
        )
        return backend

    def test_raise_propagates_first_failure(self, fake_eumdac, tmp_path, monkeypatch):
        """errors="raise" surfaces a per-product exception, not the batch summary."""
        backend = self._two_product_backend(
            fake_eumdac, tmp_path, monkeypatch, ValueError("product boom")
        )
        with pytest.raises(ValueError, match="product boom"):
            backend.download(progress_bar=False, errors="raise")

    def test_warn_returns_the_surviving_product(
        self, fake_eumdac, tmp_path, monkeypatch
    ):
        """errors="warn" drops a non-fatal per-product failure and returns survivors."""
        backend = self._two_product_backend(
            fake_eumdac, tmp_path, monkeypatch, ValueError("product boom")
        )
        paths = backend.download(progress_bar=False, errors="warn")
        assert [p.name for p in paths] == ["alpha"], (
            f"expected the one surviving product, got {[p.name for p in paths]}"
        )

    def test_ignore_drops_failures_and_returns_survivor(
        self, fake_eumdac, tmp_path, monkeypatch
    ):
        """errors="ignore" drops a non-fatal per-product failure silently."""
        backend = self._two_product_backend(
            fake_eumdac, tmp_path, monkeypatch, ValueError("product boom")
        )
        paths = backend.download(progress_bar=False, errors="ignore")
        assert [p.name for p in paths] == ["alpha"]

    def test_default_policy_is_warn(self, fake_eumdac, tmp_path, monkeypatch):
        """Omitting errors= defaults to warn — a non-fatal product failure survives."""
        backend = self._two_product_backend(
            fake_eumdac, tmp_path, monkeypatch, ValueError("product boom")
        )
        paths = backend.download(progress_bar=False)
        assert [p.name for p in paths] == ["alpha"]

    @pytest.mark.parametrize("policy", ["warn", "ignore"])
    def test_every_product_failing_raises(
        self, fake_eumdac, tmp_path, monkeypatch, policy
    ):
        """When every native product fails, the first error is raised, never empty."""
        backend = self._two_product_backend(
            fake_eumdac, tmp_path, monkeypatch, ValueError("all down"), from_call=1
        )
        with pytest.raises(ValueError, match="all down"):
            backend.download(progress_bar=False, errors=policy)

    def test_invalid_policy_rejected(self, fake_eumdac, tmp_path):
        """An unrecognised errors= value is refused before any request."""
        backend = _make_backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
        with pytest.raises(ValueError, match="errors"):
            backend.download(progress_bar=False, errors="explode")

    @pytest.mark.parametrize("policy", ["warn", "ignore"])
    def test_tailor_auth_error_propagates_under_any_policy(
        self, fake_eumdac, tmp_path, monkeypatch, policy
    ):
        """A whole-service AuthenticationError on the tailor path is fatal under any policy."""
        fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1"), _FakeProduct("p2")]
        fake_eumdac.tailor.customisation = _FakeCustomisation(statuses=["DONE"])
        backend = _make_backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
        monkeypatch.setattr(
            backend,
            "_tailor_one",
            _RaiseFromCall(backend._tailor_one, AuthenticationError("key revoked"), 1),
        )
        with pytest.raises(AuthenticationError, match="key revoked"):
            backend.download(progress_bar=False, tailor=TailorConfig(), errors=policy)

    @pytest.mark.parametrize("policy", ["warn", "ignore"])
    def test_tailor_every_product_failing_raises_non_fatal(
        self, fake_eumdac, tmp_path, monkeypatch, policy
    ):
        """When every tailor product fails, the first error is raised, never empty."""
        fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1"), _FakeProduct("p2")]
        fake_eumdac.tailor.customisation = _FakeCustomisation(statuses=["DONE"])
        backend = _make_backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
        monkeypatch.setattr(
            backend,
            "_tailor_one",
            _RaiseFromCall(backend._tailor_one, ValueError("all tailors down"), 1),
        )
        with pytest.raises(ValueError, match="all tailors down"):
            backend.download(progress_bar=False, tailor=TailorConfig(), errors=policy)
