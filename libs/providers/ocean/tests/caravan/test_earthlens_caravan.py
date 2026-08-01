"""Facade-routing tests for the Caravan backend (`EarthLens` -> `Caravan`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import earthlens.caravan
from earthlens.aggregate import AggregationConfig
from earthlens.base.http import HttpClient
from earthlens.earthlens import EarthLens

from .conftest import FakeRangeSession, build_zip

pytestmark = pytest.mark.caravan


def _facade(tmp_path: Path, catalog: Any, **overrides: Any) -> EarthLens:
    """Construct an EarthLens facade bound to the Caravan backend."""
    params: dict[str, Any] = dict(
        data_source="caravan",
        variables=["streamflow"],
        start="2020-01-01",
        end="2020-12-31",
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
        dataset="demo",
        gauge_ids=["dk_1"],
        catalog=catalog,
        client=HttpClient(session=FakeRangeSession(build_zip())),
        write_table=False,
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.parametrize("key", ["caravan", "caravan-grdc", "grdc-caravan"])
def test_keys_resolve_to_caravan(key):
    """Every alias resolves to the Caravan backend class."""
    assert EarthLens.DataSources[key] is earthlens.caravan.Caravan


def test_the_grdc_aliases_bind_the_grdc_extension():
    """GRDC-Caravan is reachable under its own name, defaulting to that row."""
    assert EarthLens.DataSources.default_kwargs("grdc-caravan") == {"dataset": "grdc"}


def test_the_plain_key_binds_no_extension():
    """`caravan` leaves the choice of extension to the caller."""
    assert EarthLens.DataSources.default_kwargs("caravan") == {}


def test_caravan_needs_no_extra():
    """The backend reads static archives over plain HTTP, so it needs no SDK."""
    extras = {key: extra for key, _, extra in EarthLens.DataSources.entries()}

    assert extras["caravan"] == ""


def test_facade_builds_a_caravan_backend(tmp_path, catalog):
    """The facade binds a Caravan instance as its datasource."""
    assert isinstance(_facade(tmp_path, catalog).datasource, earthlens.caravan.Caravan)


def test_backend_kwargs_are_forwarded(tmp_path, catalog):
    """dataset= / gauge_ids= ride through **backend_kwargs to the backend."""
    facade = _facade(tmp_path, catalog, gauge_ids=["dk_2"])

    assert facade.datasource._gauge_ids == ["dk_2"]


def test_download_returns_a_dataframe(tmp_path, catalog):
    """The facade returns the backend's frame unchanged."""
    frame = _facade(tmp_path, catalog).download()

    assert list(frame["gauge_id"].unique()) == ["dk_1"]


def test_aggregate_rejected_for_tabular(tmp_path, catalog):
    """A tabular OUTPUT_KIND cannot be reduced, exactly as for openaq."""
    facade = _facade(tmp_path, catalog)

    with pytest.raises(NotImplementedError):
        facade.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
