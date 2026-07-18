from __future__ import annotations

import os
from pathlib import Path

import pytest


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
def chirps_data_source_output_dir() -> str:
    path = "tests/data/delete/chirps-backend"
    if not os.path.exists(path):
        os.makedirs(path)
    return Path(path).absolute()


@pytest.fixture(scope="module")
def s3_era5_data_source_output_dir() -> str:
    path = "tests/data/delete/s3-era5-backend"
    if not os.path.exists(path):
        os.makedirs(path)
    return Path(path).absolute()
