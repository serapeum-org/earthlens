from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# `dates`, `number_downloaded_files`, `daily_temporal_resolution`, `lat_bounds`,
# `lon_bounds` and `chirps_variables` below are intentionally duplicated in
# libs/providers/atmosphere/tests/conftest.py (the only other member that uses
# them) — there is no shared root conftest by design (#785). Keep the two copies
# in sync.

#: Scratch root for the download fixtures below. Anchored under the OS temp dir
#: (not the repo) so an e2e/download test never recreates a stray `tests/` tree
#: at the working directory after the per-distribution move (#785).
_SCRATCH = Path(tempfile.gettempdir()) / "earthlens-test-downloads"


@pytest.fixture(scope="session")
def dates() -> list:
    return ["2009-01-01", "2009-01-02"]


@pytest.fixture(scope="session")
def monthly_dates() -> list:
    return ["2009-01-01", "2009-02-01"]


@pytest.fixture(scope="session")
def number_downloaded_files() -> int:
    return 2


@pytest.fixture(scope="session")
def daily_temporal_resolution() -> str:
    return "daily"


@pytest.fixture(scope="session")
def monthly_temporal_resolution() -> str:
    return "monthly"


@pytest.fixture(scope="module")
def lat_bounds() -> list:
    return [4.19, 4.64]


@pytest.fixture(scope="module")
def lon_bounds() -> list:
    return [-75.65, -74.73]


@pytest.fixture(scope="session")
def chirps_variables() -> list[str]:
    return ["precipitation"]


@pytest.fixture(scope="session")
def s3_era5_variables() -> list[str]:
    # NSF NCAR ERA5 surface-analysis stream ("e5.oper.an.sfc") ships 2m
    # temperature but not total precipitation (that lives in the forecast
    # stream); `t2m` is the catalog default and always downloadable.
    return ["t2m"]


@pytest.fixture(scope="session")
def chirps_data_source() -> str:
    # Primary facade key is now `"chc"`; `"chirps"` remains a
    # back-compat alias pointing at the same backend.
    return "chc"


@pytest.fixture(scope="session")
def s3_data_source() -> str:
    return "amazon-s3"


@pytest.fixture(scope="module")
def chirps_data_source_output_dir() -> Path:
    path = _SCRATCH / "chirps-backend"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="module")
def s3_era5_data_source_output_dir() -> Path:
    path = _SCRATCH / "s3-era5-backend"
    path.mkdir(parents=True, exist_ok=True)
    return path


# The HTTP transport seam lives in the installed package so every member
# root can reach it; see earthlens.testing for why it cannot live here.
from earthlens.testing import (  # noqa: F401 - fixtures + hooks used by name
    _earthlens_dirs_scratch,
    isolate_earthlens_dirs,
    pytest_runtest_call,
    pytest_sessionfinish,
    real_pooled_session,
    unpooled_http_transport,
)
