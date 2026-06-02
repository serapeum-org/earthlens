"""Facade-routing tests for the AWS Open-Data S3 backend."""

from __future__ import annotations

import pytest

from earthlens import EarthLens
from earthlens.s3 import S3

pytestmark = [pytest.mark.s3]


def test_amazon_s3_key_registered():
    """The facade exposes the amazon-s3 key."""
    assert "amazon-s3" in EarthLens.DataSources


def test_amazon_s3_routes_to_the_s3_backend(fake_client_factory, patch_auth):
    """EarthLens('amazon-s3', ...) builds the S3 backend and forwards dataset=."""
    patch_auth(fake_client_factory())
    facade = EarthLens(
        variables=None, data_source="amazon-s3",
        start="2021-01-01", end="2021-01-01",
        lat_lim=[0.4, 0.6], lon_lim=[6.4, 6.6], dataset="copernicus-dem",
    )
    assert isinstance(facade.datasource, S3)
    assert facade.datasource._dataset.bucket == "copernicus-dem-30m"


def test_key_resolves_to_the_s3_class():
    """The registry maps the key to the S3 class."""
    assert EarthLens.DataSources["amazon-s3"] is S3
