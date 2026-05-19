"""Unit + integration tests for `earthlens.cmems.backend`."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.base import RemoteProduct, SpatialExtent, TemporalExtent
from earthlens.cmems import CMEMS, AuthenticationError, CmemsAuth, CmemsCredentials
from earthlens.cmems.backend import _safe_filename


class _FakeResponse:
    """Stand-in for `copernicusmarine.ResponseSubset`."""

    def __init__(self, file_path: Path):
        self.file_path = str(file_path)
        self.status = "ok"


class _FakeCmems(types.ModuleType):
    """Stub `copernicusmarine` module supporting login + subset."""

    def __init__(self):
        super().__init__("copernicusmarine")
        self.login_calls: list[dict[str, Any]] = []
        self.subset_calls: list[dict[str, Any]] = []
        self.subset_response: _FakeResponse | None = None
        self.subset_raises: BaseException | None = None
        self.login_result: bool | BaseException = True
        self.InvalidUsernameOrPassword = type(
            "InvalidUsernameOrPassword", (Exception,), {}
        )
        self.CouldNotConnectToAuthenticationSystem = type(
            "CouldNotConnectToAuthenticationSystem", (Exception,), {}
        )
        self.CredentialsCannotBeNone = type(
            "CredentialsCannotBeNone", (Exception,), {}
        )

    def login(self, **kwargs: Any) -> bool:
        self.login_calls.append(dict(kwargs))
        if isinstance(self.login_result, BaseException):
            raise self.login_result
        return bool(self.login_result)

    def subset(self, **kwargs: Any) -> _FakeResponse:
        self.subset_calls.append(dict(kwargs))
        if self.subset_raises is not None:
            raise self.subset_raises
        assert self.subset_response is not None, (
            "test forgot to set `subset_response` on the fake module"
        )
        return self.subset_response


@pytest.fixture
def fake_cmems(monkeypatch: pytest.MonkeyPatch) -> _FakeCmems:
    fake = _FakeCmems()
    monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
    return fake


@pytest.fixture
def cmems_instance(fake_cmems: _FakeCmems, tmp_path: Path) -> CMEMS:
    """Construct a CMEMS instance against the stub toolbox."""
    return CMEMS(
        start="2024-01-01",
        end="2024-01-02",
        variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao", "so"]},
        lat_lim=[40.0, 42.0],
        lon_lim=[-10.0, -8.0],
        temporal_resolution="daily",
        path=str(tmp_path),
        service_username="alice",
        service_password="secret",
    )


@pytest.mark.cmems
class TestCMEMSConstruction:
    """`__init__` wires `space` / `time` / OUTPUT_KIND correctly."""

    def test_output_kind_is_raster(self):
        """CMEMS declares raster output (gridded NetCDF/Zarr)."""
        assert CMEMS.OUTPUT_KIND == "raster", (
            f"CMEMS.OUTPUT_KIND must be 'raster' (the on-disk artefact is "
            f"a gridded NetCDF/Zarr), got {CMEMS.OUTPUT_KIND!r}"
        )

    def test_init_captures_space(self, cmems_instance: CMEMS):
        """The user bbox is captured onto `self.space` via `SpatialExtent`."""
        assert isinstance(cmems_instance.space, SpatialExtent), (
            "self.space must be a SpatialExtent instance"
        )
        assert cmems_instance.space.south == 40.0
        assert cmems_instance.space.north == 42.0
        assert cmems_instance.space.west == -10.0
        assert cmems_instance.space.east == -8.0

    def test_init_captures_time(self, cmems_instance: CMEMS):
        """The parsed dates land on `self.time` as a `TemporalExtent`."""
        assert isinstance(cmems_instance.time, TemporalExtent)
        assert cmems_instance.time.resolution == "D", (
            f"daily cadence should map to pandas 'D' freq, "
            f"got {cmems_instance.time.resolution!r}"
        )

    def test_init_authenticates_once(self, fake_cmems: _FakeCmems, cmems_instance: CMEMS):
        """Construction triggers exactly one toolbox login call."""
        assert len(fake_cmems.login_calls) == 1, (
            f"expected 1 login call during construction, got {len(fake_cmems.login_calls)}"
        )
        assert cmems_instance._auth is not None, "CmemsAuth must be stored on the instance"
        assert isinstance(cmems_instance._auth, CmemsAuth)


@pytest.mark.cmems
class TestCMEMSSearch:
    """`_search` returns one `RemoteProduct` per `(dataset, variables)` group."""

    def test_search_produces_one_product_per_dataset(self, cmems_instance: CMEMS):
        """One dataset id in `self.vars` → one product."""
        products = cmems_instance._search()
        assert len(products) == 1, f"expected 1 product, got {len(products)}"
        assert isinstance(products[0], RemoteProduct)
        assert products[0].id == "cmems_mod_glo_phy_my_0.083deg_P1D-m"

    def test_search_carries_variables_in_metadata(self, cmems_instance: CMEMS):
        """The variable list rides on `product.metadata['variables']`."""
        products = cmems_instance._search()
        assert products[0].metadata["variables"] == ["thetao", "so"], (
            f"variables not carried in metadata; got {products[0].metadata!r}"
        )

    def test_search_multi_dataset(self, fake_cmems: _FakeCmems, tmp_path: Path):
        """Multiple datasets in `self.vars` → multiple products."""
        cm = CMEMS(
            start="2024-01-01",
            end="2024-01-02",
            variables={
                "ds-a": ["v1"],
                "ds-b": ["v2", "v3"],
            },
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            temporal_resolution="daily",
            path=str(tmp_path),
            service_username="u",
            service_password="p",
        )
        products = cm._search()
        assert [p.id for p in products] == ["ds-a", "ds-b"]
        assert products[1].metadata["variables"] == ["v2", "v3"]


@pytest.mark.cmems
class TestCMEMSFetch:
    """`_fetch` forwards the request to `copernicusmarine.subset`."""

    def test_fetch_returns_written_paths(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS, tmp_path: Path
    ):
        """`_fetch` returns one `Path` per successful subset call."""
        target = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        products = cmems_instance._search()
        paths = cmems_instance._fetch(products)
        assert paths == [target], f"_fetch should return [{target}], got {paths!r}"
        assert len(fake_cmems.subset_calls) == 1

    def test_subset_call_forwards_bbox(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS, tmp_path: Path
    ):
        """The bbox + dates are forwarded to the toolbox verbatim."""
        target = tmp_path / "out.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        cmems_instance._fetch(cmems_instance._search())
        call = fake_cmems.subset_calls[0]
        assert call["dataset_id"] == "cmems_mod_glo_phy_my_0.083deg_P1D-m"
        assert call["variables"] == ["thetao", "so"]
        assert call["minimum_longitude"] == -10.0
        assert call["maximum_longitude"] == -8.0
        assert call["minimum_latitude"] == 40.0
        assert call["maximum_latitude"] == 42.0
        assert call["overwrite"] is True

    def test_subset_call_uses_safe_filename(
        self, fake_cmems: _FakeCmems, tmp_path: Path
    ):
        """Filename strips path separators and other illegal chars."""
        cm = CMEMS(
            start="2024-01-01",
            end="2024-01-02",
            variables={"weird/id:foo*bar": ["v"]},
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
            service_username="u",
            service_password="p",
        )
        target = tmp_path / "out.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        cm._fetch(cm._search())
        filename = fake_cmems.subset_calls[0]["output_filename"]
        for bad in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
            assert bad not in filename, (
                f"unsafe character {bad!r} leaked into output_filename: {filename!r}"
            )

    def test_subset_failure_logged_and_dropped(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS
    ):
        """A single bad subset is dropped from the result, not raised."""
        fake_cmems.subset_raises = RuntimeError("server upset")
        paths = cmems_instance._fetch(cmems_instance._search())
        assert paths == [], f"failing subset should drop the path; got {paths!r}"

    def test_subset_missing_file_path_raises(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS
    ):
        """A response without file_path surfaces as FileNotFoundError."""

        class _NoPath:
            file_path = None
            status = "weird"

        fake_cmems.subset_response = _NoPath()  # type: ignore[assignment]
        with pytest.raises(FileNotFoundError, match="no file_path"):
            cmems_instance._subset_one(
                cmems_instance._search()[0], progress_bar=False
            )

    def test_invalid_credentials_midcall_wrapped(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS
    ):
        """An `InvalidUsernameOrPassword` mid-subset becomes `AuthenticationError`."""
        fake_cmems.subset_raises = fake_cmems.InvalidUsernameOrPassword("expired")
        with pytest.raises(AuthenticationError, match="mid-request"):
            cmems_instance._subset_one(
                cmems_instance._search()[0], progress_bar=False
            )


@pytest.mark.cmems
class TestCMEMSDownload:
    """End-to-end `download()` flow against the stub toolbox."""

    def test_download_writes_per_dataset(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS, tmp_path: Path
    ):
        """`download()` returns the list of written paths."""
        target = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        paths = cmems_instance.download(progress_bar=False)
        assert paths == [target], f"download() should return [{target}], got {paths!r}"

    def test_download_rejects_aggregate(self, cmems_instance: CMEMS):
        """`aggregate=` is staged — raises NotImplementedError with hint."""
        with pytest.raises(NotImplementedError, match="staged"):
            cmems_instance.download(
                progress_bar=False,
                aggregate=AggregationConfig(freq="1MS", op="mean"),
            )

    def test_download_disable_progress_forwards(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS, tmp_path: Path
    ):
        """`progress_bar=False` flips `disable_progress_bar=True` on the SDK call."""
        target = tmp_path / "out.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        cmems_instance.download(progress_bar=False)
        assert fake_cmems.subset_calls[0]["disable_progress_bar"] is True


@pytest.mark.cmems
class TestSafeFilename:
    """`_safe_filename` strips OS-illegal chars from CMEMS dataset ids."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("cmems_mod_glo_phy_my_0.083deg_P1D-m", "cmems_mod_glo_phy_my_0.083deg_P1D-m"),
            ("a/b\\c:d", "a_b_c_d"),
            ('a*b?c"d<e>f|g', "a_b_c_d_e_f_g"),
        ],
    )
    def test_safe_filename(self, raw: str, expected: str):
        """Illegal characters are uniformly replaced with `_`."""
        assert _safe_filename(raw) == expected, (
            f"_safe_filename({raw!r}) should be {expected!r}, got {_safe_filename(raw)!r}"
        )
