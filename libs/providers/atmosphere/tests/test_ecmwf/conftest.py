"""Shared pytest fixtures for the ECMWF test suite.

Holds the shared pieces every test in this directory needs:

* :func:`_block_real_cdsapi` — autouse safeguard that prevents any
  test (other than `TestApiE2E`) from constructing a real
  :class:`cdsapi.Client`.
* :func:`single_level_var_info` and :func:`pressure_level_var_info`
  — :class:`Variable` fixtures used across the api tests.
* :func:`ecmwf_stub` — a hand-constructed :class:`ECMWF` instance with
  the four attributes `_api()` consumes (`self.client`,
  `self.root_dir`, `self.time`, `self.space`) set by hand.
  Bypasses :meth:`AbstractDataSource.__init__` so unit tests can run
  without going through cdsapi or the file system.
* :func:`download_within_budget` — wraps a **live** retrieve in a
  wall-clock budget so one wedged job cannot consume the whole e2e
  lane, skips (via :func:`earthlens.testing.skip_live_unavailable`)
  when the store is throttling, and re-raises anything else. Every
  live retrieve in this directory goes through it.
"""

from __future__ import annotations

import pathlib
import threading
from pathlib import Path
from unittest.mock import MagicMock

import cdsapi
import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import ECMWF, CadsUnavailableError, Variable
from earthlens.testing import skip_live_unavailable

_LIVE_CDS_TEST_CLASSES = frozenset(
    {
        "TestApiE2E",
        "TestFacadeE2E",
        "TestGlofasE2E",
        "TestPassthroughE2E",
        "TestEfasE2E",
        "TestFireE2E",
        "TestCamsE2E",
        "TestSatelliteCdrE2E",
        "TestEcdsE2E",
        "TestXdsE2E",
    }
)


def pytest_collection_modifyitems(items):
    """Tag every test in this subtree with `@pytest.mark.ecmwf`.

    Lets the suite be filtered with `-m ecmwf` to run only the ECMWF
    backend's tests (e.g. within the atmosphere CI lane).

    Pytest delivers the FULL item list to every conftest hook,
    not just items from this subtree, so we filter by path.
    """
    here = Path(__file__).parent.resolve()
    for item in items:
        try:
            if Path(item.fspath).resolve().is_relative_to(here):
                item.add_marker(pytest.mark.ecmwf)
        except (OSError, ValueError):
            continue


@pytest.fixture(autouse=True)
def _block_real_cdsapi(request, monkeypatch):
    """Fail fast if a test reaches a live :class:`cdsapi.Client`.

    Any test outside the explicit live-CDS allow-list gets a
    :class:`cdsapi.Client` replacement that raises immediately —
    even before the constructor reads `~/.cdsapirc`. Tests that
    need a fake client still call `monkeypatch.setattr(cdsapi,
    "Client", ...)` themselves; that later setattr wins because
    monkeypatch applies fixture-scoped overrides in order.

    The :func:`ecmwf_stub` fixture sets `skip_constraints=True` on
    the synthetic instance so tests that build synthetic requests
    via `_api()` bypass the pre-flight validator (which would
    otherwise hit the live CDS catalogue endpoint). Tests targeting
    the validator itself (in `test_constraints.py`) construct
    :class:`RequestValidator` directly so this default doesn't
    interfere.

    Also clears `EARTHLENS_ECMWF_MODERN` so an ambient flag in a
    contributor shell cannot silently divert `open_client` onto the
    modern client and break the classic endpoint tests.
    """
    monkeypatch.delenv("EARTHLENS_ECMWF_MODERN", raising=False)
    if request.cls is not None and request.cls.__name__ in _LIVE_CDS_TEST_CLASSES:
        return

    def _no_live_client(*args, **kwargs):
        raise AssertionError(
            "A unit test attempted to construct a real cdsapi.Client. "
            'Add `monkeypatch.setattr(cdsapi, "Client", lambda: ...)` '
            "to the test (replacing the lambda with the fake your test "
            "needs), or move the test into a live class (TestApiE2E / "
            "TestFacadeE2E) selected via `pytest -m e2e`."
        )

    monkeypatch.setattr(cdsapi, "Client", _no_live_client)


#: Grace period for an overrunning retrieve to finish its in-flight write
#: before the test fails and pytest removes `tmp_path` underneath it.
_ABANDON_GRACE_SECONDS = 5.0


@pytest.fixture
def download_within_budget():
    """Run a live retrieve under a wall-clock budget, failing fast on a hang.

    Live CDS retrieves sit in a server-side queue whose wait is unbounded; a
    single stuck job (the recurring queue-hang class) would otherwise burn the
    whole 45-minute e2e lane and cancel every following test. This runs the
    download on a daemon thread and fails the test if it overruns the budget,
    so one wedged retrieve costs only its own budget rather than the lane.

    Accepts either an object exposing `download()` (an `EarthLens` / backend
    instance) or a zero-argument callable, so a test driving `_api()` directly
    is guarded the same way as one going through the facade.

    A `CadsUnavailableError` **skips** rather than fails: the store refused to
    queue the job on its per-dataset limit, which says nothing about the code
    under test and clears on its own. The skip goes through
    :func:`earthlens.testing.skip_live_unavailable` so it carries the shared
    availability stamp — a bare `pytest.skip` is invisible to the masked-lane
    guard, and a store-wide throttle would then report a lane green having
    exercised nothing.

    Returns:
        Callable[..., Any]: A `run(work, budget_s=900.0)` helper that returns
        the retrieve result, re-raises any error it raised, or fails the test
        if the budget elapses first.
    """

    def _run(work, budget_s: float = 900.0):
        box: dict = {}
        # Select on the attribute, not on callability: a Mock is both
        # callable and has `.download`, and calling it would pass the
        # test vacuously without ever running a download.
        job = work.download if hasattr(work, "download") else work

        def _work():
            try:
                box["out"] = job()
            except BaseException as exc:  # noqa: BLE001 - relayed to main thread
                box["exc"] = exc

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()
        worker.join(budget_s)
        if worker.is_alive():
            # The thread is still inside `retrieve`, writing into `tmp_path`,
            # and pytest is about to tear that directory down underneath it.
            # A daemon thread cannot be killed, so give it a short grace period
            # to finish the write it is in; if it outlives that, say so rather
            # than leaving a confusing teardown error as the only trace.
            worker.join(_ABANDON_GRACE_SECONDS)
            # A retrieve that lands during the grace produced a real result, so
            # use it rather than failing the test on a deadline it then beat.
            if not box:
                stray = (
                    " (a retrieve is still running and holds a queue slot)"
                    if worker.is_alive()
                    else ""
                )
                pytest.fail(
                    f"live retrieve exceeded the {budget_s:.0f}s budget "
                    f"(CDS queue hang); failing fast so the e2e lane "
                    f"survives{stray}"
                )
        if "exc" in box:
            exc = box["exc"]
            if isinstance(exc, CadsUnavailableError):
                skip_live_unavailable(f"CADS store is throttling this account: {exc}")
            raise exc
        return box["out"]

    return _run


@pytest.fixture
def single_level_var_info():
    """CDS catalog entry for a single-level ERA5 variable.

    Returns:
        Variable: Catalog metadata for `2m_temperature` on
        `reanalysis-era5-single-levels`.
    """
    return Variable(
        cds_dataset="reanalysis-era5-single-levels",
        cds_variable="2m_temperature",
        nc_variable="t2m",
        units="K",
        product_type=["reanalysis"],
    )


@pytest.fixture
def pressure_level_var_info():
    """CDS catalog entry for a pressure-level ERA5 variable.

    Returns:
        Variable: Catalog metadata for `temperature` on
        `reanalysis-era5-pressure-levels` at 1000 hPa.
    """
    return Variable(
        cds_dataset="reanalysis-era5-pressure-levels",
        cds_variable="temperature",
        cds_pressure_level=["1000"],
        nc_variable="t",
        units="K",
        product_type=["reanalysis"],
    )


def _writing_client() -> MagicMock:
    """A `cdsapi` stub that writes the file it is handed, as the real one does.

    The backend retrieves into a `.part` sidecar and moves it onto the target,
    treating a retrieve that wrote nothing as a failed download. A bare
    `MagicMock` writes nothing, so it would exercise that failure path instead
    of the success path every caller of this fixture means to test.
    """
    client = MagicMock()
    client.retrieve.side_effect = lambda dataset, request, target: pathlib.Path(
        target
    ).write_bytes(b"")
    return client


@pytest.fixture
def ecmwf_stub(tmp_path):
    """Minimal `ECMWF` instance with the attributes `_api()` consumes.

    Skips the full parent `__init__` chain (which would still call
    :meth:`cdsapi.Client` for real) and instead constructs the
    instance via `ECMWF.__new__` and wires up the four attributes
    :meth:`ECMWF._api` reads — `self.client`, `self.root_dir`,
    `self.time` and `self.space` — by hand.

    Args:
        tmp_path: Per-test temp directory provided by pytest, used as
            `self.root_dir` so target paths land on the test fs.

    Returns:
        ECMWF: An `ECMWF` instance ready for `_api()` invocation.
    """
    ecmwf = ECMWF.__new__(ECMWF)
    ecmwf.client = _writing_client()
    ecmwf.root_dir = tmp_path
    ecmwf.time = TemporalExtent(
        start_date=pd.Timestamp("2022-01-01"),
        end_date=pd.Timestamp("2022-01-03"),
        resolution="D",
        dates=pd.date_range("2022-01-01", "2022-01-03", freq="D"),
    )
    ecmwf.space = SpatialExtent(
        latitude_min=4.19,
        latitude_max=4.64,
        longitude_min=-75.65,
        longitude_max=-74.73,
        resolution=0.125,
    )
    ecmwf.temporal_resolution = "daily"
    ecmwf.skip_constraints = True
    return ecmwf
