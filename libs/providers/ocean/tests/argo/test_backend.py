"""Tests for the Argo backend (faked argopy, no network)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pytest

import earthlens.argo
from earthlens.argo import ARGO
from earthlens.argo._helpers import ARGO_COLUMNS, region_box

from .conftest import DataNotFound, FakeArgo

pytestmark = pytest.mark.argo


def test_no_xarray_leak_in_source():
    """No argo source module imports xarray (G1 — the headline landmine)."""
    pattern = re.compile(r"import xarray|\bxr\.|to_xarray")
    pkg_dir = Path(earthlens.argo.__file__).parent
    offenders = [
        path.name
        for path in pkg_dir.glob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"xarray reference leaked into argo source: {offenders}"


def test_region_construction_and_dispatch(
    fake_argopy: FakeArgo,
    argo_kwargs: Callable[..., dict[str, Any]],
    info_log: list[str],
):
    """A region request builds the default DataFetcher and calls .region(box)."""
    backend = ARGO(**argo_kwargs())
    result = backend.download()

    assert fake_argopy.ctor_kwargs == {
        "src": "erddap",
        "ds": "phy",
        "mode": "standard",
    }
    assert fake_argopy.method() == "region"
    assert fake_argopy.call_args("region") == region_box(
        backend.space, backend.time, (0.0, 2000.0)
    )
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 3
    # The acknowledgement is logged exactly once on a non-empty fetch.
    acks = [m for m in info_log if "International Argo Program" in m]
    assert len(acks) == 1


def test_acknowledgement_logged_once_per_instance(
    fake_argopy: FakeArgo,
    argo_kwargs: Callable[..., dict[str, Any]],
    info_log: list[str],
):
    """The Argo acknowledgement is logged once per instance, not per download."""
    backend = ARGO(**argo_kwargs())
    backend.download()
    backend.download()
    acks = [m for m in info_log if "International Argo Program" in m]
    assert len(acks) == 1


def test_region_writes_table(
    fake_argopy: FakeArgo, argo_kwargs: Callable[..., dict[str, Any]], tmp_path
):
    """The realised frame is written to root_dir."""
    backend = ARGO(**argo_kwargs(path=str(tmp_path)))
    backend.download()
    assert (tmp_path / "argo_phy_region.csv").exists()


def test_empty_variables_is_region(
    fake_argopy: FakeArgo, argo_kwargs: Callable[..., dict[str, Any]]
):
    """An empty variables list is a region selection with no param validation."""
    backend = ARGO(**argo_kwargs(variables=[]))
    backend.download()
    assert fake_argopy.method() == "region"


def test_float_selector_dispatch(
    fake_argopy: FakeArgo, argo_kwargs: Callable[..., dict[str, Any]]
):
    """A float: selector calls .float([wmo]) and not .region."""
    backend = ARGO(**argo_kwargs(variables=["float:6902746"]))
    backend.download()
    assert fake_argopy.method() == "float"
    assert fake_argopy.call_args("float") == [6902746]
    with pytest.raises(KeyError):
        fake_argopy.call_args("region")


def test_profile_selector_dispatch(
    fake_argopy: FakeArgo, argo_kwargs: Callable[..., dict[str, Any]]
):
    """A profile: selector calls .profile(wmo, cycle)."""
    backend = ARGO(**argo_kwargs(variables=["profile:6902746/12"]))
    backend.download()
    assert fake_argopy.method() == "profile"
    assert fake_argopy.call_args("profile") == (6902746, 12)


def test_bgc_dataset_knob(
    fake_argopy: FakeArgo, argo_kwargs: Callable[..., dict[str, Any]]
):
    """dataset='bgc' constructs the fetcher with ds='bgc'."""
    backend = ARGO(**argo_kwargs(variables=["DOXY"], dataset="bgc"))
    backend.download()
    assert fake_argopy.ctor_kwargs["ds"] == "bgc"


def test_unknown_bgc_param_rejected(argo_kwargs: Callable[..., dict[str, Any]]):
    """An unknown BGC parameter raises with a did-you-mean hint at construction."""
    with pytest.raises(ValueError, match="Did you mean 'DOXY'"):
        ARGO(**argo_kwargs(variables=["DOXyy"], dataset="bgc"))


def test_unknown_phy_param_rejected(argo_kwargs: Callable[..., dict[str, Any]]):
    """A region request validates phy parameter names through the constructor."""
    with pytest.raises(ValueError, match="Did you mean 'TEMP'"):
        ARGO(**argo_kwargs(variables=["TEMPP"], dataset="phy"))


def test_aggregate_rejected(argo_kwargs: Callable[..., dict[str, Any]]):
    """A non-None aggregate= is rejected, pointing at CMEMS."""
    backend = ARGO(**argo_kwargs())
    with pytest.raises(NotImplementedError, match="CMEMS"):
        backend.download(aggregate=object())


def test_empty_via_raise(
    fake_argopy: FakeArgo,
    argo_kwargs: Callable[..., dict[str, Any]],
):
    """A no-data error folds to the canonical empty frame, no crash."""
    fake_argopy.raises = DataNotFound("no floats here")
    backend = ARGO(**argo_kwargs())
    result = backend.download()
    assert list(result.columns) == ARGO_COLUMNS
    assert len(result) == 0


def test_empty_via_empty_frame(
    fake_argopy: FakeArgo, argo_kwargs: Callable[..., dict[str, Any]]
):
    """An already-empty returned frame normalises to the canonical columns."""
    fake_argopy.frame = pd.DataFrame({"SOMETHING_ELSE": []})
    backend = ARGO(**argo_kwargs())
    result = backend.download()
    assert list(result.columns) == ARGO_COLUMNS
    assert len(result) == 0


def test_no_xarray_accessor_used(
    fake_argopy: FakeArgo, argo_kwargs: Callable[..., dict[str, Any]]
):
    """The fake has no .to_xarray, so a clean fetch proves only pandas is used."""
    backend = ARGO(**argo_kwargs())
    # A successful download means the backend only touched .to_dataframe().
    assert isinstance(backend.download(), pd.DataFrame)


def test_bad_source_rejected(argo_kwargs: Callable[..., dict[str, Any]]):
    """An unknown source= is rejected at construction."""
    with pytest.raises(ValueError, match="source must be one of"):
        ARGO(**argo_kwargs(source="nope"))


def test_bad_dataset_rejected(argo_kwargs: Callable[..., dict[str, Any]]):
    """An unknown dataset= is rejected at construction."""
    with pytest.raises(ValueError, match="dataset must be one of"):
        ARGO(**argo_kwargs(dataset="nope"))


def test_bad_mode_rejected(argo_kwargs: Callable[..., dict[str, Any]]):
    """An unknown mode= is rejected at construction."""
    with pytest.raises(ValueError, match="mode must be one of"):
        ARGO(**argo_kwargs(mode="nope"))


def test_bad_output_format_rejected(argo_kwargs: Callable[..., dict[str, Any]]):
    """An unknown output_format= is rejected at construction."""
    with pytest.raises(ValueError, match="output_format must be one of"):
        ARGO(**argo_kwargs(output_format="tsv"))


def test_variables_mapping_rejected(argo_kwargs: Callable[..., dict[str, Any]]):
    """A mapping variables= is rejected (this backend takes a flat list)."""
    with pytest.raises(TypeError, match="must be a list"):
        ARGO(**argo_kwargs(variables={"phy": ["TEMP"]}))


def test_parquet_output(
    fake_argopy: FakeArgo, argo_kwargs: Callable[..., dict[str, Any]], tmp_path
):
    """output_format='parquet' writes a .parquet file."""
    pytest.importorskip("pyarrow")
    backend = ARGO(**argo_kwargs(path=str(tmp_path), output_format="parquet"))
    backend.download()
    assert (tmp_path / "argo_phy_region.parquet").exists()
