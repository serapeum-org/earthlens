from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

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
