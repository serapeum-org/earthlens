"""Lock-in for the CHIRPS-GEFS v3 catalog rows and the paths they build (#1387)."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.chc import CHIRPS, Catalog

pytestmark = [pytest.mark.chc]

#: Every v3 row keyed to a sample date and the exact remote path the backend
#: builds for it -- each path was confirmed to resolve live on data.chc.ucsb.edu.
_ROW_PATHS = [
    (
        "chirps-gefs-v3-05day",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/05_day/global/data/2021/c3g_2021.06.15.tif",
    ),
    (
        "chirps-gefs-v3-05day-anom",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/05_day/global/anom/2021/c3g_anom_2021.06.15.tif",
    ),
    (
        "chirps-gefs-v3-10day",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/10_day/global/data/2021/c3g_2021.06.15.tif",
    ),
    (
        "chirps-gefs-v3-10day-anom",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/10_day/global/anom/2021/c3g_anom_2021.06.15.tif",
    ),
    (
        "chirps-gefs-v3-15day",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/15_day/global/data/2021/c3g_2021.06.15.tif",
    ),
    (
        "chirps-gefs-v3-15day-anom",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/15_day/global/anom/2021/c3g_anom_2021.06.15.tif",
    ),
    (
        "chirps-gefs-v3-16day",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/16_day/2021/06/c3g_2021.06.15.tif",
    ),
    (
        "chirps-gefs-v3-daily",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/daily/global/2021/06/15/c3g_2021.06.15.tif",
    ),
    (
        "chirps-gefs-v3-dekad-lead0",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/dekad_lead_0/global/data/c3g_2021.06.2.tif",
    ),
    (
        "chirps-gefs-v3-dekad-lead0-anom",
        "2021-06-15",
        "products/CHIRPS-GEFS/v3/dekad_lead_0/global/anom/c3g_anom_2021.06.2.tif",
    ),
    (
        "chirps-gefs-v3-pentad-lead0",
        "2021-06-03",
        "products/CHIRPS-GEFS/v3/pentad_lead_0/global/data/c3g_2021.06.1.tif",
    ),
    (
        "chirps-gefs-v3-pentad-lead0-anom",
        "2021-06-03",
        "products/CHIRPS-GEFS/v3/pentad_lead_0/global/anom/c3g_anom_2021.06.1.tif",
    ),
    (
        "chirps-gefs-v3-pentad-lead1",
        "2021-06-08",
        "products/CHIRPS-GEFS/v3/pentad_lead_1/global/data/c3g_2021.06.2.tif",
    ),
    (
        "chirps-gefs-v3-pentad-lead1-anom",
        "2021-06-08",
        "products/CHIRPS-GEFS/v3/pentad_lead_1/global/anom/c3g_anom_2021.06.2.tif",
    ),
    (
        "chirps-gefs-v3-pentad-lead2",
        "2021-06-13",
        "products/CHIRPS-GEFS/v3/pentad_lead_2/global/data/c3g_2021.06.3.tif",
    ),
    (
        "chirps-gefs-v3-pentad-lead2-anom",
        "2021-06-13",
        "products/CHIRPS-GEFS/v3/pentad_lead_2/global/anom/c3g_anom_2021.06.3.tif",
    ),
]

#: Expected (units, types) per row, by the variable each exposes.
_DATA_UNITS = {
    "chirps-gefs-v3-05day": "mm/5-days",
    "chirps-gefs-v3-10day": "mm/10-days",
    "chirps-gefs-v3-15day": "mm/15-days",
    "chirps-gefs-v3-16day": "mm",
    "chirps-gefs-v3-daily": "mm/day",
    "chirps-gefs-v3-dekad-lead0": "mm/dekad",
    "chirps-gefs-v3-pentad-lead0": "mm/pentad",
    "chirps-gefs-v3-pentad-lead1": "mm/pentad",
    "chirps-gefs-v3-pentad-lead2": "mm/pentad",
}


def _remote_path(ds, date: str) -> str:
    """Rebuild the full remote path the backend fetches for `ds` on `date`."""
    fmt = ds.default_format
    placeholders = CHIRPS._placeholders(pd.Timestamp(date), pandas_freq=ds.pandas_freq)
    relative = ds.file_patterns[fmt].format(**placeholders)
    return f"{ds.ftp_bases[fmt].rstrip('/')}/{relative}"


@pytest.fixture(scope="module")
def bundled_catalog() -> Catalog:
    """Bundled catalog, loaded once per module."""
    return Catalog()


class TestGefsV3Catalog:
    """The 16 CHIRPS-GEFS v3 rows parse and build their live-verified paths (#1387)."""

    def test_exactly_the_sixteen_v3_rows_are_present(self, bundled_catalog: Catalog):
        """The catalog ships every expected v3 key and no legacy v12 key."""
        gefs = sorted(
            k for k in bundled_catalog.datasets if k.startswith("chirps-gefs")
        )
        expected = sorted(key for key, _, _ in _ROW_PATHS)
        assert gefs == expected, f"GEFS rows drifted from the v3 set: {gefs}"

    @pytest.mark.parametrize("key, date, expected", _ROW_PATHS)
    def test_builds_the_live_verified_remote_path(
        self, bundled_catalog: Catalog, key: str, date: str, expected: str
    ):
        """Each row's ftp_base + file_pattern builds the exact path confirmed live."""
        ds = bundled_catalog.datasets[key]
        result = _remote_path(ds, date)
        assert result == expected, f"{key}: built {result!r}, expected {expected!r}"

    @pytest.mark.parametrize("key, units", sorted(_DATA_UNITS.items()))
    def test_data_rows_expose_precipitation_flux(
        self, bundled_catalog: Catalog, key: str, units: str
    ):
        """Each forecast row exposes `precipitation` as an unsigned flux with the expected units."""
        var = bundled_catalog.datasets[key].variables["precipitation"]
        assert var.is_flux, f"{key}: precipitation should be flux"
        assert var.units == units, f"{key}: units {var.units!r}, expected {units!r}"
        assert not var.signed, (
            f"{key}: precipitation fill negatives must clobber (unsigned)"
        )

    @pytest.mark.parametrize(
        "key", [k for k, _, _ in _ROW_PATHS if k.endswith("-anom")]
    )
    def test_anom_rows_expose_precipitation_anomaly_flux(
        self, bundled_catalog: Catalog, key: str
    ):
        """Each anomaly row exposes `precipitation_anomaly` as a flux, not `precipitation`."""
        ds = bundled_catalog.datasets[key]
        assert "precipitation_anomaly" in ds.variables, (
            f"{key}: missing anomaly variable"
        )
        assert "precipitation" not in ds.variables, (
            f"{key}: should not expose raw precipitation"
        )
        assert ds.variables["precipitation_anomaly"].is_flux, (
            f"{key}: anomaly should be flux"
        )
        assert ds.variables["precipitation_anomaly"].signed, (
            f"{key}: anomaly must be signed so below-climatology negatives survive (C1)"
        )

    @pytest.mark.parametrize(
        "key, expected_start",
        [
            ("chirps-gefs-v3-pentad-lead0", "2001-01-01"),
            ("chirps-gefs-v3-pentad-lead1", "2001-01-06"),
            ("chirps-gefs-v3-pentad-lead2", "2001-01-11"),
            ("chirps-gefs-v3-pentad-lead1-anom", "2001-01-06"),
            ("chirps-gefs-v3-pentad-lead2-anom", "2001-01-11"),
        ],
    )
    def test_pentad_lead_start_dates_match_the_archive_offset(
        self, bundled_catalog: Catalog, key: str, expected_start: str
    ):
        """Each pentad lead starts at its N-pentad archive offset, not the floor (L1)."""
        start = bundled_catalog.datasets[key].start_date
        assert start == expected_start, (
            f"{key}: start_date {start!r}, expected {expected_start!r}"
        )
