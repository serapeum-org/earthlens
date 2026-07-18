"""Shared fakes + fixtures for the openEO backend tests (no network, no SDK)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


class FakeJobResults:
    """Stand-in for an openEO `JobResults` that records downloads."""

    def __init__(self, log: list) -> None:
        self.log = log

    def download_file(self, target: str) -> None:
        """Record a single-file batch download and write a placeholder."""
        self.log.append(("job_download_file", Path(target).name))
        Path(target).write_text("x", encoding="utf-8")

    def download_files(self, target: str) -> None:
        """Record a multi-file batch download."""
        self.log.append(("job_download_files", Path(target).name))


class FakeJob:
    """Stand-in for an openEO `BatchJob` that completes immediately."""

    def __init__(self, log: list) -> None:
        self.log = log

    def start_and_wait(self) -> FakeJob:
        """Pretend the job ran to completion."""
        self.log.append(("start_and_wait",))
        return self

    def get_results(self) -> FakeJobResults:
        """Return the (fake) results handle."""
        return FakeJobResults(self.log)


class FakeCube:
    """Stand-in for an openEO `DataCube` that records every applied step."""

    def __init__(self, log: list) -> None:
        self.log = log

    def ndvi(self, nir: str, red: str) -> FakeCube:
        """Record an `ndvi` step."""
        self.log.append(("ndvi", nir, red))
        return self

    def reduce_dimension(self, dimension: str, reducer: str) -> FakeCube:
        """Record a `reduce_dimension` step."""
        self.log.append(("reduce_dimension", dimension, reducer))
        return self

    def aggregate_temporal_period(self, period: str, reducer: str) -> FakeCube:
        """Record an `aggregate_temporal_period` step."""
        self.log.append(("aggregate_temporal_period", period, reducer))
        return self

    def process(self, name: str, arguments: dict[str, Any]) -> FakeCube:
        """Record a generic backend process step (e.g. `mask_scl_dilation`)."""
        self.log.append(("process", name, sorted(arguments)))
        return self

    def download(self, target: str, format: str) -> None:
        """Record a synchronous download and write a placeholder file."""
        self.log.append(("download", Path(target).name, format))
        Path(target).write_text("x", encoding="utf-8")

    def create_job(self, out_format: str) -> FakeJob:
        """Record batch-job creation and return a fake job."""
        self.log.append(("create_job", out_format))
        return FakeJob(self.log)


class FakeConnection:
    """Stand-in for an openEO `Connection` that records load + auth calls."""

    def __init__(self, log: list | None = None) -> None:
        self.log = log if log is not None else []

    def load_collection(
        self,
        collection_id: str,
        spatial_extent: dict,
        temporal_extent: list,
        bands: list | None,
        **kwargs: Any,
    ) -> FakeCube:
        """Record a `load_collection` call and return a fake cube."""
        self.log.append(
            (
                "load_collection",
                collection_id,
                bands,
                kwargs,
                spatial_extent,
                temporal_extent,
            )
        )
        return FakeCube(self.log)

    def authenticate_oidc(self, provider_id: str | None = None) -> FakeConnection:
        """Record the interactive-flow auth call."""
        self.log.append(("authenticate_oidc", provider_id))
        return self

    def authenticate_oidc_client_credentials(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        provider_id: str | None = None,
    ) -> FakeConnection:
        """Record the client-credentials auth call."""
        self.log.append(("client_credentials", client_id, client_secret, provider_id))
        return self

    def authenticate_oidc_refresh_token(
        self,
        client_id: str | None = None,
        refresh_token: str | None = None,
        client_secret: str | None = None,
        provider_id: str | None = None,
    ) -> FakeConnection:
        """Record the refresh-token auth call."""
        self.log.append(("refresh_token", client_id, refresh_token, provider_id))
        return self


class FakeOpeneoModule:
    """Stand-in for the `openeo` top-level module exposing `connect`."""

    def __init__(self, connection: FakeConnection | None = None) -> None:
        self.connection = connection or FakeConnection()
        self.connect_calls: list[str] = []

    def connect(self, url: str) -> FakeConnection:
        """Record the endpoint and return the shared fake connection."""
        self.connect_calls.append(url)
        return self.connection


class FakeAuth:
    """A minimal auth double exposing the `connection()` the backend calls."""

    def __init__(self, connection: FakeConnection) -> None:
        self._conn = connection

    def connection(self) -> FakeConnection:
        """Return the pre-built fake connection."""
        return self._conn


@pytest.fixture
def fake_connection() -> FakeConnection:
    """A fresh recording fake openEO connection."""
    return FakeConnection()


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """A temporary output directory for download tests."""
    return tmp_path
