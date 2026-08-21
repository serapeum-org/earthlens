"""Live end-to-end test for a satellite CDR (gated).

Runs only under `-m e2e`; needs a Copernicus token and the CDR's licence
accepted. A one-day window returns a single-member zip, so this exercises the
C3 `_unpack_netcdf_archive` single-member branch + a pyramids read (the
multi-member branch is covered offline in `test_output_format.py`).
"""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e]


class TestSatelliteCdrE2E:
    """Live soil-moisture CDR retrieve — unpacked and read."""

    def test_live_soil_moisture_unpacks_and_reads(
        self, tmp_path, download_within_budget
    ):
        """A tiny soil-moisture CDR retrieves, unpacks, and reads with pyramids."""
        lens = EarthLens(
            data_source="ecmwf",
            variables={"satellite-soil-moisture": ["surface-soil-moisture-volumetric"]},
            start="2023-01-01",
            end="2023-01-01",
            temporal_resolution="daily",
            lat_lim=[0.0, 10.0],
            lon_lim=[0.0, 10.0],
            path=str(tmp_path),
        )
        out = download_within_budget(lens)
        assert out, "at least one NetCDF member written"

        from pyramids.netcdf import NetCDF

        assert NetCDF.read_file(str(out[0])).variable_names
