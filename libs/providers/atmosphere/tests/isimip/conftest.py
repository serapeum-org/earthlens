"""Shared fixtures for the ISIMIP backend tests (no network, injected client)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from earthlens.isimip import ISIMIP


def make_dataset(
    var: str,
    *,
    restricted: bool = False,
    rights: str = "CC0 1.0",
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one canned ISIMIP dataset dict for `var`, decade-split by default."""
    if files is None:
        files = [
            {
                "name": f"gfdl-esm4_r1i1p1f1_w5e5_ssp585_{var}_global_daily_2015_2020.nc",
                "path": f"ISIMIP3b/InputData/.../{var}_global_daily_2015_2020.nc",
                "file_url": f"https://files.isimip.org/.../{var}_2015_2020.nc",
            },
            {
                "name": f"gfdl-esm4_r1i1p1f1_w5e5_ssp585_{var}_global_daily_2091_2100.nc",
                "path": f"ISIMIP3b/InputData/.../{var}_global_daily_2091_2100.nc",
                "file_url": f"https://files.isimip.org/.../{var}_2091_2100.nc",
            },
        ]
    return {
        "id": f"id-{var}",
        "name": f"gfdl-esm4_r1i1p1f1_w5e5_ssp585_{var}_global_daily",
        "restricted": restricted,
        "rights": {"short": rights},
        "files": files,
    }


class FakeClient:
    """In-memory stand-in for `isimip-client`'s `ISIMIPClient` that records calls."""

    def __init__(
        self,
        *,
        datasets_by_var: dict[str, list[dict[str, Any]]] | None = None,
        restricted: bool = False,
        rights: str = "CC0 1.0",
        job_status: str = "finished",
        empty_vars: tuple[str, ...] = (),
        job: dict[str, Any] | None = None,
        writes_output: bool = True,
    ) -> None:
        self._datasets_by_var = datasets_by_var
        self._restricted = restricted
        self._rights = rights
        self._job_status = job_status
        self._empty = set(empty_vars)
        self._job = job
        self._writes_output = writes_output
        self.datasets_calls: list[dict[str, Any]] = []
        self.cutout_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []

    def datasets(self, **facets: Any) -> list[dict[str, Any]]:
        """Return the canned datasets for the requested `climate_variable`."""
        self.datasets_calls.append(facets)
        var = facets["climate_variable"]
        if var in self._empty:
            return []
        if self._datasets_by_var is not None:
            return self._datasets_by_var.get(var, [])
        return [make_dataset(var, restricted=self._restricted, rights=self._rights)]

    def cutout_bbox(
        self,
        paths: list[str],
        west: float,
        east: float,
        south: float,
        north: float,
        poll: float | None = None,
    ) -> dict[str, Any]:
        """Record the cutout request and return a canned (finished) job dict."""
        self.cutout_calls.append(
            {
                "paths": list(paths),
                "west": west,
                "east": east,
                "south": south,
                "north": north,
                "poll": poll,
            }
        )
        if self._job is not None:
            return self._job
        return {
            "status": self._job_status,
            "file_url": "https://files.isimip.org/api/v2/output/job.zip",
            "file_name": "job.zip",
        }

    def download(
        self, url: str, path: str | None = None, extract: bool = False
    ) -> None:
        """Record the download and drop a tiny fake `.nc` into the output dir."""
        self.download_calls.append({"url": url, "path": path, "extract": extract})
        if not self._writes_output:
            return
        directory = Path(path or ".")
        (directory / f"cut_{len(self.download_calls)}.nc").write_bytes(b"CDF\x01 nc")
        if extract:
            (directory / "isimip-download.zip").write_bytes(b"PK\x03\x04 zip")
            (directory / "README.txt").write_text("terms", encoding="utf-8")


@pytest.fixture
def fake_client() -> FakeClient:
    """A default recording FakeClient (one CC0 dataset per variable)."""
    return FakeClient()


@pytest.fixture
def make_backend(tmp_path):
    """Factory building an ISIMIP backend over an injected client and tmp_path."""

    def _make(client: FakeClient | None = None, **overrides: Any) -> ISIMIP:
        kwargs: dict[str, Any] = {
            "start": "2016-01-01",
            "end": "2018-12-31",
            "dataset": "ISIMIP3b",
            "variables": ["pr"],
            "scenario": "ssp585",
            "gcm": "gfdl-esm4",
            "lat_lim": [51.0, 53.0],
            "lon_lim": [6.0, 8.0],
            "path": str(tmp_path),
        }
        kwargs.update(overrides)
        return ISIMIP(client=client if client is not None else FakeClient(), **kwargs)

    return _make
