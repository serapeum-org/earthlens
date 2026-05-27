"""Shared fakes and fixtures for the HDX backend tests.

The whole suite runs without touching `data.humdata.org` or needing a
network: a fake `hdx` SDK (`hdx.api.configuration` + `hdx.data.dataset`)
is injected into `sys.modules`, so the lazy imports inside the backend
resolve to the fakes. Mirrors `tests/earthdata/conftest.py`.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest


class FakeConfigurationError(Exception):
    """Stand-in for `hdx.api.configuration.ConfigurationError`."""


class FakeConfiguration:
    """Fake HDX `Configuration` singleton recording `create()` calls."""

    _created: bool = False
    create_calls: list[dict[str, Any]] = []

    @classmethod
    def reset(cls) -> None:
        """Clear the singleton state between tests."""
        cls._created = False
        cls.create_calls = []

    @classmethod
    def read(cls):
        """Return the config or raise when none has been created yet."""
        if not cls._created:
            raise FakeConfigurationError("There is no HDX configuration!")
        return cls

    @classmethod
    def create(cls, **kwargs: Any):
        """Create the singleton once; re-creating raises, like the real SDK."""
        if cls._created:
            raise FakeConfigurationError("Configuration already created!")
        cls._created = True
        cls.create_calls.append(kwargs)
        return "https://data.humdata.org"


class FakeResource(dict):
    """Fake HDX `Resource` — a dict of `name`/`format`/`url` that downloads."""

    def __init__(self, name: str, fmt: str, url: str = "http://x/r"):
        super().__init__(name=name, format=fmt, url=url)
        self.download_calls: list[dict[str, Any]] = []

    def download(self, folder: str | None = None, retriever: Any = None):
        """Write a stub file into `folder` and return `(url, Path)`."""
        self.download_calls.append({"folder": folder})
        target = Path(folder) / self["name"]
        target.write_text("stub", encoding="utf-8")
        return self["url"], target


class FakeDataset(dict):
    """Fake HDX `Dataset` resolving ids from a class-level registry."""

    registry: dict[str, "FakeDataset"] = {}

    def __init__(self, name: str, resources: list[FakeResource]):
        super().__init__(name=name, title=f"Title for {name}")
        self._resources = resources

    @classmethod
    def read_from_hdx(cls, identifier: str, configuration: Any = None):
        """Return the registered dataset for `identifier`, or `None`."""
        return cls.registry.get(identifier)

    @classmethod
    def get_all_dataset_names(cls, configuration: Any = None) -> list[str]:
        """Return every registered dataset id (the whole fake catalogue)."""
        return list(cls.registry)

    def get_resources(self) -> list[FakeResource]:
        """Return this dataset's resources."""
        return list(self._resources)

    def get_organization(self) -> dict[str, str]:
        """Return a stub organisation record."""
        return {"name": "fake-org"}


class FakeHdx:
    """Controller exposing the fake SDK surface to tests."""

    Configuration = FakeConfiguration
    ConfigurationError = FakeConfigurationError
    Dataset = FakeDataset

    @staticmethod
    def add_dataset(hdx_id: str, resources: list[FakeResource]) -> FakeDataset:
        """Register a dataset under `hdx_id` so `read_from_hdx` resolves it."""
        dataset = FakeDataset(hdx_id, resources)
        FakeDataset.registry[hdx_id] = dataset
        return dataset


@pytest.fixture
def fake_hdx(monkeypatch: pytest.MonkeyPatch) -> FakeHdx:
    """Inject a fake `hdx` SDK into `sys.modules` (no network, no real SDK)."""
    FakeConfiguration.reset()
    FakeDataset.registry = {}

    conf_mod = types.ModuleType("hdx.api.configuration")
    conf_mod.Configuration = FakeConfiguration
    conf_mod.ConfigurationError = FakeConfigurationError

    dataset_mod = types.ModuleType("hdx.data.dataset")
    dataset_mod.Dataset = FakeDataset

    hdx_mod = types.ModuleType("hdx")
    api_mod = types.ModuleType("hdx.api")
    data_mod = types.ModuleType("hdx.data")

    for name, module in {
        "hdx": hdx_mod,
        "hdx.api": api_mod,
        "hdx.api.configuration": conf_mod,
        "hdx.data": data_mod,
        "hdx.data.dataset": dataset_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    FakeHdx.add_dataset(
        "kontur-population-dataset",
        [FakeResource("kontur_population.gpkg.gz", "Geopackage")],
    )
    return FakeHdx()
