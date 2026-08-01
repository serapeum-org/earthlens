"""Unit tests for the ECMWF output-format handling (zip-of-NetCDF; GRIB; detect).

Covers magic-byte format detection, multi-member zip unpacking, and a real
pyramids read of an unpacked member — all offline.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Variable
from earthlens.ecmwf.backend import (
    ECMWF,
    _detect_output_format,
    _unpack_netcdf_archive,
)

pytestmark = [pytest.mark.unit]


def _write_netcdf(path: Path) -> None:
    """Write a minimal lat/lon NetCDF pyramids can read (xarray is test-only)."""
    import xarray as xr

    dataset = xr.Dataset(
        {"t2m": (("latitude", "longitude"), np.ones((2, 2), dtype="f4"))},
        coords={"latitude": [1.0, 0.0], "longitude": [0.0, 1.0]},
    )
    dataset.to_netcdf(path)


class TestDetectOutputFormat:
    """`_detect_output_format` classifies by magic bytes."""

    @pytest.mark.parametrize(
        "head, expected",
        [
            (b"PK\x03\x04rest", "zip"),
            (b"GRIB....", "grib"),
            (b"CDF\x01xxx", "netcdf"),
            (b"\x89HDFxxxx", "netcdf"),
            (b"id,value", "unknown"),
        ],
    )
    def test_magic_bytes(self, tmp_path, head, expected):
        """Each magic-byte prefix maps to the right format."""
        target = tmp_path / "blob"
        target.write_bytes(head)
        assert _detect_output_format(target) == expected


class TestUnpackArchive:
    """`_unpack_netcdf_archive` unpacks zip-of-NetCDF to member NetCDFs."""

    def test_non_zip_returned_unchanged(self, tmp_path):
        """A plain file is returned as-is."""
        target = tmp_path / "plain.nc"
        target.write_bytes(b"CDFdata")
        assert _unpack_netcdf_archive(target) == [target]

    def test_single_member_unwrapped_in_place(self, tmp_path):
        """A one-member zip is unwrapped, keeping the target path."""
        target = tmp_path / "ds.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("only.nc", b"CDFonly")
        assert _unpack_netcdf_archive(target) == [target]
        assert target.read_bytes() == b"CDFonly"

    def test_multi_member_extracted_to_sibling_dir(self, tmp_path):
        """A multi-member zip extracts members and removes the archive."""
        target = tmp_path / "cdr.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("2020.nc", b"CDF2020")
            archive.writestr("2021.nc", b"CDF2021")
        out = _unpack_netcdf_archive(target)
        assert [p.name for p in out] == ["2020.nc", "2021.nc"]
        assert not target.exists(), "archive removed"
        assert out[0].parent == tmp_path / "cdr"

    def test_zip_without_nc_member_left_raw(self, tmp_path):
        """A zip with no `.nc` member is left raw."""
        target = tmp_path / "other.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("readme.txt", b"hi")
        assert _unpack_netcdf_archive(target) == [target]
        assert zipfile.is_zipfile(target)

    def test_path_traversal_member_flattened(self, tmp_path):
        """A member with a traversal path is written to the sibling dir only."""
        target = tmp_path / "evil.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("a.nc", b"CDFa")
            archive.writestr("../../escape.nc", b"CDFb")
        out = _unpack_netcdf_archive(target)
        assert all((tmp_path / "evil") in p.parents for p in out), "no escape"
        assert not (tmp_path.parent.parent / "escape.nc").exists()


class _ZipClient:
    """Fake cdsapi client whose retrieve writes a multi-member zip to `target`."""

    def __init__(self, members: list[str]):
        self._members = members

    def retrieve(self, dataset, request, target):
        with zipfile.ZipFile(target, "w") as archive:
            for name in self._members:
                archive.writestr(name, b"CDF" + name.encode())


def _curated_backend(tmp_path: Path) -> ECMWF:
    """A stub ECMWF wired for a curated `_api` retrieve (constraints skipped)."""
    backend = ECMWF.__new__(ECMWF)
    backend.root_dir = tmp_path
    backend.skip_constraints = True
    backend.temporal_resolution = "monthly"
    backend.time = TemporalExtent(
        start_date=pd.Timestamp("2020-01-01"),
        end_date=pd.Timestamp("2021-12-01"),
        resolution="MS",
        dates=pd.date_range("2020-01-01", "2021-12-01", freq="MS"),
    )
    backend.space = SpatialExtent(
        latitude_min=0.0,
        latitude_max=1.0,
        longitude_min=0.0,
        longitude_max=1.0,
        resolution=0.1,
    )
    return backend


class TestCuratedApiMultiMemberZip:
    """The curated `_api` path unpacks a multi-member zip instead of crashing (H1)."""

    def test_multi_member_retrieve_unpacks_not_raises(self, tmp_path):
        """A curated retrieve returning a 2-member zip unpacks; returns a member."""
        backend = _curated_backend(tmp_path)
        backend._client_for = lambda endpoint: _ZipClient(["2020.nc", "2021.nc"])
        var = Variable(
            cds_dataset="satellite-soil-moisture",
            cds_variable="volumetric_surface_soil_moisture",
            nc_variable="sm",
            units="m3 m-3",
            request_kind="satellite_cdr",
            extras={"data_format": "zip"},
        )
        out = backend._api(var)
        member_dir = tmp_path / f"{var.cds_variable}_{var.cds_dataset}"
        assert out.parent == member_dir, "returns a member under the sibling dir"
        assert sorted(p.name for p in member_dir.glob("*.nc")) == ["2020.nc", "2021.nc"]


class TestPyramidsReadsUnpackedMember:
    """An unpacked member NetCDF reads into a pyramids Dataset (C3 acceptance)."""

    def test_unpacked_member_is_readable(self, tmp_path):
        """A zipped 'CDR' is unpacked and each member reads with pyramids."""
        member = tmp_path / "src.nc"
        _write_netcdf(member)
        archive = tmp_path / "cdr.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(member, "t_2020.nc")
            zf.write(member, "t_2021.nc")

        out = _unpack_netcdf_archive(archive)
        assert len(out) == 2

        from pyramids.netcdf import NetCDF

        assert "t2m" in NetCDF.read_file(str(out[0])).variable_names
