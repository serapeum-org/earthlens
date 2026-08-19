from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# `dates`, `number_downloaded_files`, `daily_temporal_resolution`, `lat_bounds`,
# `lon_bounds` and `chirps_variables` below are intentionally duplicated in
# libs/core/tests/conftest.py (the only other member that uses them) — there is
# no shared root conftest by design (#785). Keep the two copies in sync.

#: Scratch root for the download fixture below. Anchored under the OS temp dir
#: (not the repo) so an e2e/download test never recreates a stray `tests/` tree
#: at the working directory after the per-distribution move (#785).
_SCRATCH = Path(tempfile.gettempdir()) / "earthlens-test-downloads"


@pytest.fixture(scope="session")
def dates() -> list:
    return ["2009-01-01", "2009-01-02"]


@pytest.fixture(scope="session")
def number_downloaded_files() -> int:
    return 2


@pytest.fixture(scope="session")
def daily_temporal_resolution() -> str:
    return "daily"


@pytest.fixture(scope="module")
def lat_bounds() -> list:
    return [4.19, 4.64]


@pytest.fixture(scope="module")
def lon_bounds() -> list:
    return [-75.65, -74.73]


@pytest.fixture(scope="session")
def chirps_variables() -> list[str]:
    return ["precipitation"]


@pytest.fixture(scope="module")
def chirps_base_dir() -> Path:
    path = _SCRATCH / "chirps"
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
