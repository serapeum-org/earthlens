"""Tests for tools/cmems/probe_cmems_netcdf.py."""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest

import probe_cmems_netcdf as pcn  # noqa: E402 (sys.path injection in conftest)
from tests.cmems.tools.conftest import FakeCmemsModule


class _FakeVarAttrs:
    def __init__(self, long_name: str = "", unit: str = "") -> None:
        self.long_name = long_name
        self.unit = unit


class _FakeMetaData:
    def __init__(self, variables: dict[str, _FakeVarAttrs]) -> None:
        self.variables = variables


class _FakeNetCDFHandle:
    def __init__(self, variables: dict[str, _FakeVarAttrs]) -> None:
        self.meta_data = _FakeMetaData(variables)

    def __enter__(self) -> "_FakeNetCDFHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub `pyramids.netcdf.NetCDF.read_file` returning a fake handle."""
    captured: dict[str, Any] = {}

    class _FakeNetCDFClass:
        @classmethod
        def read_file(cls, path: str, *args: Any, **kwargs: Any) -> _FakeNetCDFHandle:
            captured["path"] = path
            return _FakeNetCDFHandle(captured["variables"])

    fake_pkg = types.ModuleType("pyramids")
    fake_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_netcdf = types.ModuleType("pyramids.netcdf")
    fake_netcdf.NetCDF = _FakeNetCDFClass  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "pyramids", fake_pkg)
    monkeypatch.setitem(__import__("sys").modules, "pyramids.netcdf", fake_netcdf)
    return captured


class TestSafeFilename:
    """`safe_filename` character substitution."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("cmems_mod_glo_phy_my_0.083deg_P1D-m", "cmems_mod_glo_phy_my_0_083deg_P1D-m"),
            ("path/with/slashes", "path_with_slashes"),
            ("has:colons", "has_colons"),
            ("safe_id", "safe_id"),
        ],
    )
    def test_replaces_unsafe(self, raw: str, expected: str) -> None:
        """Forbidden Windows path chars get replaced with underscores."""
        assert pcn.safe_filename(raw) == expected


class TestFetchOne:
    """`fetch_one` toolbox-call wrapper."""

    def test_returns_cached_target_without_calling_subset(
        self,
        fake_cm_module: FakeCmemsModule,
        tmp_path: Path,
    ) -> None:
        """If the cache file already exists, subset() is not invoked."""
        target = tmp_path / "cached.nc"
        target.write_bytes(b"fake")
        result = pcn.fetch_one("any_ds", ["v"], target)
        assert result == target
        assert fake_cm_module.subset_calls == []

    def test_invokes_subset_with_expected_kwargs(
        self,
        fake_cm_module: FakeCmemsModule,
        tmp_path: Path,
    ) -> None:
        """Default kwargs match the documented probe window."""
        target = tmp_path / "fresh.nc"
        fake_cm_module.subset_response = types.SimpleNamespace(file_path=str(target))
        pcn.fetch_one("ds-1", ["thetao"], target)
        assert len(fake_cm_module.subset_calls) == 1
        call = fake_cm_module.subset_calls[0]
        assert call["dataset_id"] == "ds-1"
        assert call["variables"] == ["thetao"]
        assert call["minimum_longitude"] == 0.0
        assert call["maximum_longitude"] == 1.0
        assert call["minimum_latitude"] == 0.0
        assert call["maximum_latitude"] == 1.0
        assert call["minimum_depth"] == 0.0
        assert call["maximum_depth"] == 5.0
        assert call["start_datetime"] == call["end_datetime"]
        assert call["disable_progress_bar"] is True
        assert call["file_format"] == "netcdf"

    def test_depth_none_omits_depth_kwargs(
        self,
        fake_cm_module: FakeCmemsModule,
        tmp_path: Path,
    ) -> None:
        """Passing `depth_range=None` drops minimum_depth/maximum_depth from the call."""
        target = tmp_path / "fresh.nc"
        fake_cm_module.subset_response = types.SimpleNamespace(file_path=str(target))
        pcn.fetch_one("ds-1", ["v"], target, depth_range=None)
        call = fake_cm_module.subset_calls[0]
        assert "minimum_depth" not in call
        assert "maximum_depth" not in call

    def test_credentials_file_forwarded(
        self,
        fake_cm_module: FakeCmemsModule,
        tmp_path: Path,
    ) -> None:
        """When given, `credentials_file=` is forwarded to subset()."""
        target = tmp_path / "fresh.nc"
        creds = tmp_path / "creds"
        fake_cm_module.subset_response = types.SimpleNamespace(file_path=str(target))
        pcn.fetch_one("ds-1", ["v"], target, credentials_file=creds)
        call = fake_cm_module.subset_calls[0]
        assert call["credentials_file"] == str(creds)


class TestCollectMetadata:
    """NetCDF metadata extraction."""

    def test_skips_coordinate_variables(
        self, fake_pyramids: dict[str, Any], tmp_path: Path
    ) -> None:
        """Coord vars (lat / lon / time / depth) are not in the output sidecar."""
        fake_pyramids["variables"] = {
            "lat": _FakeVarAttrs("latitude", "degrees_north"),
            "lon": _FakeVarAttrs("longitude", "degrees_east"),
            "time": _FakeVarAttrs("time", "seconds since 1970"),
            "depth": _FakeVarAttrs("depth", "m"),
            "thetao": _FakeVarAttrs("Sea water potential temperature", "degrees_C"),
            "so": _FakeVarAttrs("Sea water salinity", "1e-3"),
        }
        nc_path = tmp_path / "fake.nc"
        nc_path.write_bytes(b"fake")
        out = pcn.collect_metadata(nc_path)
        assert set(out.keys()) == {"thetao", "so"}
        assert out["thetao"]["long_name"] == "Sea water potential temperature"
        assert out["thetao"]["units"] == "degrees_C"
        assert out["thetao"]["nc_short_name"] == "thetao"

    def test_empty_metadata_when_no_data_vars(
        self, fake_pyramids: dict[str, Any], tmp_path: Path
    ) -> None:
        """Only-coord file returns an empty dict."""
        fake_pyramids["variables"] = {"time": _FakeVarAttrs("time", "s")}
        out = pcn.collect_metadata(tmp_path / "fake.nc")
        assert out == {}


class TestProbeOneDataset:
    """`probe_one_dataset` happy/error paths."""

    def test_happy_path(
        self,
        fake_cm_module: FakeCmemsModule,
        fake_pyramids: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Successful fetch + open returns a per-variable metadata dict."""
        target = tmp_path / "ds-id.nc"
        fake_cm_module.subset_response = types.SimpleNamespace(file_path=str(target))
        fake_pyramids["variables"] = {
            "thetao": _FakeVarAttrs("Sea water potential temperature", "degrees_C"),
        }
        # Mimic toolbox writing the file - probe_one_dataset checks target.exists()
        result = pcn.probe_one_dataset("ds-id", ["thetao"], tmp_path)
        assert "__error__" not in result
        assert "thetao" in result
        assert result["thetao"]["units"] == "degrees_C"

    def test_fetch_failure_captured(
        self,
        fake_cm_module: FakeCmemsModule,
        tmp_path: Path,
    ) -> None:
        """A subset() failure shows up as `__error__` in the sidecar."""
        fake_cm_module.subset_raises = fake_cm_module.DatasetNotFound(
            "unknown id 'ds-bogus'"
        )
        result = pcn.probe_one_dataset("ds-bogus", ["thetao"], tmp_path)
        assert "__error__" in result
        assert "DatasetNotFound" in result["__error__"]["error"]

    def test_collect_failure_captured(
        self,
        fake_cm_module: FakeCmemsModule,
        fake_pyramids: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pyramids.NetCDF failure is also captured as `__error__`."""
        target = tmp_path / "ds-id.nc"
        target.write_bytes(b"fake")  # pretend it's cached so fetch is skipped
        # patch collect_metadata to raise
        monkeypatch.setattr(
            pcn,
            "collect_metadata",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("bad file")),
        )
        result = pcn.probe_one_dataset("ds-id", ["v"], tmp_path)
        assert "__error__" in result
        assert "RuntimeError" in result["__error__"]["error"]
        assert "bad file" in result["__error__"]["error"]


class TestParseBbox:
    """`_parse_bbox` CLI parser."""

    def test_valid(self) -> None:
        """Four comma-separated floats parse into a tuple."""
        assert pcn._parse_bbox("0,1,2,3") == (0.0, 1.0, 2.0, 3.0)

    def test_invalid_arity(self) -> None:
        """Three values raise ArgumentTypeError."""
        import argparse

        with pytest.raises(argparse.ArgumentTypeError, match="4 floats"):
            pcn._parse_bbox("0,1,2")


class TestParseDepth:
    """`_parse_depth` CLI parser."""

    def test_valid_pair(self) -> None:
        """`min,max` parses to a tuple."""
        assert pcn._parse_depth("0,5") == (0.0, 5.0)

    def test_none_sentinel(self) -> None:
        """`none`, `null`, `skip` all parse to None."""
        for token in ("none", "null", "skip", "NONE", "Skip"):
            assert pcn._parse_depth(token) is None, f"failed for {token!r}"

    def test_invalid_arity(self) -> None:
        """One value raises ArgumentTypeError."""
        import argparse

        with pytest.raises(argparse.ArgumentTypeError, match="min,max"):
            pcn._parse_depth("0")


class TestCuratedDatasetVariables:
    """`_curated_dataset_variables` reads the bundled YAML."""

    def test_returns_mapping(self) -> None:
        """The bundled catalog yields a non-empty `{dataset_id: [vars]}` map."""
        out = pcn._curated_dataset_variables()
        assert isinstance(out, dict)
        assert len(out) > 0
        for ds_id, vars_ in out.items():
            assert isinstance(ds_id, str)
            assert isinstance(vars_, list)


class TestMain:
    """CLI dispatch."""

    def test_help(self) -> None:
        """`--help` exits 0."""
        with pytest.raises(SystemExit) as exc_info:
            pcn.main(["--help"])
        assert exc_info.value.code == 0

    def test_single_mode_writes_sidecar(
        self,
        fake_cm_module: FakeCmemsModule,
        fake_pyramids: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """`--dataset` + `--variables` + `--out` writes the JSON sidecar."""
        target = tmp_path / "cache" / "ds-id.nc"
        target.parent.mkdir(parents=True, exist_ok=True)
        fake_cm_module.subset_response = types.SimpleNamespace(file_path=str(target))
        fake_pyramids["variables"] = {
            "thetao": _FakeVarAttrs("Sea water potential temperature", "degrees_C"),
        }
        out_path = tmp_path / "sidecar.json"
        rc = pcn.main(
            [
                "--dataset",
                "ds-id",
                "--variables",
                "thetao",
                "--out",
                str(out_path),
                "--cache-dir",
                str(target.parent),
            ]
        )
        assert rc == 0
        sidecar = json.loads(out_path.read_text())
        assert "thetao" in sidecar

    def test_dataset_without_variables_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--dataset` without `--variables` exits with an argparse error."""
        with pytest.raises(SystemExit):
            pcn.main(["--dataset", "ds-id", "--out", "out.json"])

    def test_all_curated_without_out_dir_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--all-curated` requires `--out-dir`."""
        with pytest.raises(SystemExit):
            pcn.main(["--all-curated"])
