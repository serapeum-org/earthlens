from __future__ import annotations

import os
from pathlib import Path

import pytest


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
def chirps_base_dir() -> str:
    rpath = Path("tests/data/delete/chirps")
    if not os.path.exists(rpath):
        os.makedirs(rpath)
    return Path(rpath).absolute()
