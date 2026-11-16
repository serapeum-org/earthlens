"""Lock-in for the SPEI GeoTIFF rows: the v1->C3_Merra remap plus the new FLDAS variant."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.chc import CHIRPS, Catalog

pytestmark = [pytest.mark.chc]

#: Each SPEI row keyed to its first in-range month and the exact remote path the
#: backend builds -- every path was confirmed to resolve live on data.chc.ucsb.edu.
_ROW_PATHS = [
    (
        "spei-v1-01-month",
        "1981-01-01",
        "products/SPEI/C3_Merra_SPEI/01_month/SPEI_C3_Merra_Global_01_198101.tif",
    ),
    (
        "spei-v1-02-month",
        "1981-02-01",
        "products/SPEI/C3_Merra_SPEI/02_month/SPEI_C3_Merra_Global_02_198102.tif",
    ),
    (
        "spei-v1-03-month",
        "1981-03-01",
        "products/SPEI/C3_Merra_SPEI/03_month/SPEI_C3_Merra_Global_03_198103.tif",
    ),
    (
        "spei-v1-06-month",
        "1981-06-01",
        "products/SPEI/C3_Merra_SPEI/06_month/SPEI_C3_Merra_Global_06_198106.tif",
    ),
    (
        "spei-v1-09-month",
        "1981-09-01",
        "products/SPEI/C3_Merra_SPEI/09_month/SPEI_C3_Merra_Global_09_198109.tif",
    ),
    (
        "spei-v1-12-month",
        "1981-12-01",
        "products/SPEI/C3_Merra_SPEI/12_month/SPEI_C3_Merra_Global_12_198112.tif",
    ),
    (
        "spei-fldas-01-month",
        "1982-01-01",
        "products/SPEI/C3_FLDAS_SPEI/01_month/SPEI_C3_FLDAS_Global_01_198201.tif",
    ),
    (
        "spei-fldas-02-month",
        "1982-02-01",
        "products/SPEI/C3_FLDAS_SPEI/02_month/SPEI_C3_FLDAS_Global_02_198202.tif",
    ),
    (
        "spei-fldas-03-month",
        "1982-03-01",
        "products/SPEI/C3_FLDAS_SPEI/03_month/SPEI_C3_FLDAS_Global_03_198203.tif",
    ),
    (
        "spei-fldas-06-month",
        "1982-06-01",
        "products/SPEI/C3_FLDAS_SPEI/06_month/SPEI_C3_FLDAS_Global_06_198206.tif",
    ),
    (
        "spei-fldas-09-month",
        "1982-09-01",
        "products/SPEI/C3_FLDAS_SPEI/09_month/SPEI_C3_FLDAS_Global_09_198209.tif",
    ),
    (
        "spei-fldas-12-month",
        "1982-12-01",
        "products/SPEI/C3_FLDAS_SPEI/12_month/SPEI_C3_FLDAS_Global_12_198212.tif",
    ),
]


@pytest.fixture(scope="module")
def bundled_catalog() -> Catalog:
    """Bundled catalog, loaded once per module."""
    return Catalog()


class TestSpeiMigration:
    """The SPEI rows (MERRA v1 remap + the new FLDAS variant) ship as GeoTIFFs and build live-verified paths."""

    @pytest.mark.parametrize("key, date, expected", _ROW_PATHS)
    def test_builds_the_live_verified_remote_path(
        self, bundled_catalog: Catalog, key: str, date: str, expected: str
    ):
        """Each SPEI row's ftp_base + file_pattern builds the exact tif path confirmed live."""
        ds = bundled_catalog.datasets[key]
        fmt = ds.default_format
        placeholders = CHIRPS._placeholders(
            pd.Timestamp(date), pandas_freq=ds.pandas_freq
        )
        relative = ds.file_patterns[fmt].format(**placeholders)
        result = f"{ds.ftp_bases[fmt].rstrip('/')}/{relative}"
        assert result == expected, f"{key}: built {result!r}, expected {expected!r}"

    @pytest.mark.parametrize("key, _date, _expected", _ROW_PATHS)
    def test_rows_are_signed_geotiffs_not_netcdf(
        self, bundled_catalog: Catalog, key: str, _date: str, _expected: str
    ):
        """Each SPEI row now ships as a `tif` and its `spei` variable stays signed."""
        ds = bundled_catalog.datasets[key]
        assert "tif" in ds.ftp_bases, (
            f"{key}: expected a tif base, got {list(ds.ftp_bases)}"
        )
        assert "netcdf" not in ds.ftp_bases, f"{key}: stale netcdf base remains"
        assert ds.variables["spei"].signed, (
            f"{key}: spei must stay signed (now a raster)"
        )
