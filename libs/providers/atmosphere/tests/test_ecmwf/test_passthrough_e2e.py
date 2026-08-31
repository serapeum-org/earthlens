"""Live end-to-end test for the ECMWF raw-request passthrough.

Retrieves an uncurated raw request through the store the id belongs to and
checks a real file comes back. Runs only under `-m e2e` (needs a Copernicus
token and the dataset's licence accepted).
"""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e]


class TestPassthroughE2E:
    """Live passthrough retrieve through the resolved store."""

    def test_live_cds_passthrough_returns_netcdf(
        self, tmp_path, download_within_budget
    ):
        """A raw CDS request downloads and reads back as NetCDF, no curated row."""
        lens = EarthLens(
            data_source="ecmwf",
            dataset="reanalysis-era5-single-levels",
            request={
                "variable": ["2m_temperature"],
                "year": ["2023"],
                "month": ["01"],
                "day": ["01"],
                "time": ["00:00"],
                "area": [1, 0, 0, 1],
                "data_format": "netcdf",
            },
            path=str(tmp_path),
        )
        out = download_within_budget(lens)
        assert len(out) == 1, "one file written"
        target = out[0]
        assert target.exists()
        assert target.stat().st_size > 0, "non-empty file"

        from pyramids.netcdf import NetCDF

        assert "t2m" in NetCDF.read_file(str(target)).variable_names
