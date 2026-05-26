"""Integration tests: full NWP download through the real centre dispatch.

Unlike the unit tests (which inject a fake centre onto the backend),
these drive `download()` end to end through `resolve_centre` and the
real centre adapters, with only the per-centre SDKs / pyramids reader
faked via `sys.modules` and monkeypatch — so the cycle-grid walk, the
centre import + selector building, and the GRIB2 -> cropped-COG pipeline
are exercised together.
"""

from __future__ import annotations

import pytest

from earthlens.nwp import NWP
from earthlens.nwp.catalog import NWPModel

pytestmark = [pytest.mark.nwp, pytest.mark.integration]


def _nwp(catalog, tmp_path, variables, **kwargs):
    """Build an NWP over the given in-memory catalog and request."""
    params = dict(
        start="2024-06-01",
        end="2024-06-01",
        variables=variables,
        lat_lim=[10, 20],
        lon_lim=[30, 40],
        path=str(tmp_path),
        catalog=catalog,
    )
    params.update(kwargs)
    return NWP(**params)


class TestNOAAFlow:
    """End-to-end GFS fetch via the Herbie dispatch."""

    def test_download_writes_cropped_cogs(self, mini_catalog, tmp_path, fake_herbie, fake_pyramids):
        """download() walks two cycles, fetches via Herbie, and crops each COG."""
        nwp = _nwp(mini_catalog, tmp_path, {"gfs": ["temperature_2m"]})
        paths = nwp.download(progress_bar=False)
        assert [p.name for p in paths] == [
            "gfs_2024060100_f000.tif",
            "gfs_2024060112_f000.tif",
        ]
        assert len(fake_herbie.instances) == 2
        assert fake_herbie.instances[0].download_calls == [":TMP:2 m above ground:"]
        assert fake_pyramids["opened"][0].cropped == ((30.0, 10.0, 40.0, 20.0), 4326)


class TestDWDFlow:
    """End-to-end ICON fetch via the direct-HTTPS dispatch."""

    def test_download_builds_urls_and_crops(self, mini_catalog, tmp_path, fake_requests, fake_pyramids):
        """download() builds per-variable DWD URLs and crops the concatenated GRIB."""
        nwp = _nwp(mini_catalog, tmp_path, {"icon-global": ["temperature_2m", "precipitation_acc"]})
        paths = nwp.download(progress_bar=False)
        assert len(paths) == 2
        assert any("t_2m" in url for url in fake_requests["urls"])
        assert any("tot_prec" in url for url in fake_requests["urls"])
        assert len(fake_pyramids["opened"]) == 2


class TestECMWFFlow:
    """End-to-end IFS fetch via the ecmwf-opendata dispatch."""

    def test_download_retrieves_and_crops(self, tmp_path, fake_ecmwf_client, fake_pyramids):
        """download() retrieves IFS param tokens and crops the result."""
        from earthlens.nwp import Catalog

        catalog = Catalog(
            datasets={
                "ifs-hres": NWPModel(
                    provider="ecmwf-opendata",
                    model_family="ifs",
                    cycles_utc=[0],
                    horizon_h=240,
                    backend="ecmwf-opendata",
                    mirrors=["aws"],
                    bands={"temperature_2m": "2t"},
                )
            }
        )
        nwp = _nwp(catalog, tmp_path, {"ifs-hres": ["temperature_2m"]})
        paths = nwp.download(progress_bar=False)
        assert len(paths) == 1
        assert fake_ecmwf_client.instances[-1].retrieve_calls[-1]["param"] == ["2t"]
