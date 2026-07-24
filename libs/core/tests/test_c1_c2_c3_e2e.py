"""End-to-end smoke tests for C1 / C2 / C3 framework changes.

The C1 / C2 / C3 changes are framework-level: a class attribute, an
ABC, and two new hooks. They do not introduce new live network
surfaces of their own. These e2e tests confirm the framework wiring
is correct **against a real backend** — using the existing CHIRPS
FTP infrastructure that other e2e tests already exercise — without
introducing a new network dependency.

All tests in this module are marked `@pytest.mark.e2e` and skipped
unless the user opts in with `pytest -m e2e`.
"""

from __future__ import annotations

import glob
import os
import shutil

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.base import AbstractAuth, AuthenticationError, RemoteProduct
from earthlens.earthlens import EarthLens


@pytest.mark.e2e
@pytest.mark.chc
class TestC1FacadeGuardAgainstLiveChirps:
    """The C1 aggregate-guard does not break a real CHIRPS download."""

    def test_chirps_download_with_aggregate_none(self, tmp_path):
        """CHIRPS (OUTPUT_KIND='raster') downloads with `aggregate=None` (default)."""
        el = EarthLens(
            data_source="chc",
            start="2009-01-01",
            end="2009-01-01",
            variables=["precipitation"],
            lat_lim=[4.19, 4.64],
            lon_lim=[-75.65, -74.73],
            path=str(tmp_path),
        )
        # Confirm the C1 contract before exercising the live FTP:
        assert el.datasource.OUTPUT_KIND == "raster"

        el.download(progress_bar=False)

        files = glob.glob(os.path.join(str(tmp_path), "*.tif"))
        assert files, f"no files downloaded into {tmp_path!r}: {os.listdir(tmp_path)!r}"
        # Tidy up — the conftest tmp_path already auto-cleans, but be explicit.
        try:
            shutil.rmtree(str(tmp_path), ignore_errors=True)
        except PermissionError:
            pass

    def test_chirps_rejects_aggregate_for_a_hypothetical_vector_backend(self, tmp_path):
        """If we relabel CHIRPS' OUTPUT_KIND as vector, the facade rejects aggregate."""
        el = EarthLens(
            data_source="chc",
            start="2009-01-01",
            end="2009-01-01",
            variables=["precipitation"],
            lat_lim=[4.19, 4.64],
            lon_lim=[-75.65, -74.73],
            path=str(tmp_path),
        )
        # Patch only the instance attribute — not the class — so other
        # tests are unaffected.
        el.datasource.OUTPUT_KIND = "vector"
        with pytest.raises(NotImplementedError, match="aggregate= is not supported"):
            el.download(
                progress_bar=False,
                aggregate=AggregationConfig(freq="1MS", op="sum"),
            )


@pytest.mark.e2e
class TestC3SearchFetchHookSurface:
    """The C3 split is wired through `AbstractDataSource` and importable from the package root."""

    def test_remote_product_importable_from_base(self):
        """`from earthlens.base import RemoteProduct` resolves."""
        rp = RemoteProduct(id="x", href="s3://b/k", metadata={"a": 1})
        assert rp.id == "x"
        assert rp.metadata == {"a": 1}

    def test_abstract_auth_and_error_importable_from_base(self):
        """`AbstractAuth` and `AuthenticationError` are exported from `earthlens.base`."""
        # Smoke-import; mypy would catch this statically too.
        assert AbstractAuth.__name__ == "AbstractAuth"
        assert issubclass(AuthenticationError, Exception)
