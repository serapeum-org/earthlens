"""Shared fixtures for the tools/cmems/ test suite.

Puts `tools/cmems/` on `sys.path` so each helper module can be
imported by its bare name (`import _helpers`, `import
refresh_cmems_catalog`, etc.) — the scripts themselves do exactly the
same `sys.path.insert(Path(__file__).parent, ...)` dance, so the
tests mirror their import path rather than reaching across a separate
test-only module layout.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[3] / "tools" / "cmems"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


class FakeVariable:
    """Minimal stand-in for `CopernicusMarineVariable`."""

    def __init__(
        self,
        short_name: str,
        units: str = "",
        standard_name: str | None = None,
        coordinates: list["FakeCoordinate"] | None = None,
    ) -> None:
        self.short_name = short_name
        self.units = units
        self.standard_name = standard_name
        self.coordinates = coordinates or []


class FakeCoordinate:
    """Minimal stand-in for `CopernicusMarineCoordinate`."""

    def __init__(
        self,
        coordinate_id: str,
        coordinate_unit: str | None = None,
        minimum_value: float | None = None,
        maximum_value: float | None = None,
    ) -> None:
        self.coordinate_id = coordinate_id
        self.coordinate_unit = coordinate_unit
        self.minimum_value = minimum_value
        self.maximum_value = maximum_value


class FakeService:
    """Minimal stand-in for `CopernicusMarineService`."""

    def __init__(self, variables: list[FakeVariable]) -> None:
        self.variables = variables


class FakePart:
    """Minimal stand-in for `CopernicusMarinePart`."""

    def __init__(self, services: list[FakeService]) -> None:
        self.services = services


class FakeVersion:
    """Minimal stand-in for `CopernicusMarineVersion`."""

    def __init__(self, parts: list[FakePart], label: str = "default") -> None:
        self.parts = parts
        self.label = label


class FakeDataset:
    """Minimal stand-in for `CopernicusMarineDataset`."""

    def __init__(
        self,
        dataset_id: str,
        versions: list[FakeVersion],
        dataset_name: str = "",
    ) -> None:
        self.dataset_id = dataset_id
        self.versions = versions
        self.dataset_name = dataset_name


class FakeProduct:
    """Minimal stand-in for `CopernicusMarineProduct`."""

    def __init__(
        self,
        product_id: str,
        datasets: list[FakeDataset],
        title: str = "",
    ) -> None:
        self.product_id = product_id
        self.datasets = datasets
        self.title = title


class FakeCatalogue:
    """Minimal stand-in for `CopernicusMarineCatalogue`."""

    def __init__(self, products: list[FakeProduct]) -> None:
        self.products = products


def make_dataset(
    dataset_id: str,
    variables: list[FakeVariable],
    *,
    dataset_name: str = "",
) -> FakeDataset:
    """Helper: build a dataset with one (version, part, service) carrying `variables`."""
    return FakeDataset(
        dataset_id,
        [FakeVersion([FakePart([FakeService(variables)])])],
        dataset_name=dataset_name,
    )


@pytest.fixture
def fake_product() -> FakeProduct:
    """A fake product with two datasets carrying physics variables."""
    daily = make_dataset(
        "cmems_mod_glo_phy_my_0.083deg_P1D-m",
        [
            FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature"),
            FakeVariable("so", "1e-3", "sea_water_salinity"),
        ],
        dataset_name="GLORYS12 daily mean",
    )
    monthly = make_dataset(
        "cmems_mod_glo_phy_my_0.083deg_P1M-m",
        [FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature")],
        dataset_name="GLORYS12 monthly mean",
    )
    return FakeProduct(
        "GLOBAL_MULTIYEAR_PHY_001_030",
        [daily, monthly],
        title="Global Ocean Physics Reanalysis",
    )


class FakeCmemsModule(types.ModuleType):
    """`copernicusmarine` stub for the maintainer-script tests.

    Captures `describe()` / `subset()` / `login()` arg lists for
    assertion. Default behaviour: empty 1-product catalogue,
    `DatasetNotFound` for any specific dataset_id.
    """

    def __init__(self) -> None:
        super().__init__("copernicusmarine")
        self.describe_calls: list[dict[str, Any]] = []
        self.subset_calls: list[dict[str, Any]] = []
        self.login_calls: list[dict[str, Any]] = []
        self.describe_response: Any | None = None
        self.describe_by_product: dict[str, Any] = {}
        self.describe_by_dataset: dict[str, Any] = {}
        self.describe_raises: dict[str, BaseException] = {}
        self.subset_response: Any | None = None
        self.subset_raises: BaseException | None = None

        self.DatasetNotFound = type(
            "DatasetNotFound", (Exception,), {}
        )
        self.InvalidUsernameOrPassword = type(
            "InvalidUsernameOrPassword", (Exception,), {}
        )
        self.CouldNotConnectToAuthenticationSystem = type(
            "CouldNotConnectToAuthenticationSystem", (Exception,), {}
        )
        self.CredentialsCannotBeNone = type(
            "CredentialsCannotBeNone", (Exception,), {}
        )

    def describe(self, **kwargs: Any) -> Any:
        self.describe_calls.append(dict(kwargs))
        product_id = kwargs.get("product_id")
        dataset_id = kwargs.get("dataset_id")
        if dataset_id is not None and dataset_id in self.describe_raises:
            raise self.describe_raises[dataset_id]
        if product_id is not None and product_id in self.describe_raises:
            raise self.describe_raises[product_id]
        if dataset_id is not None and dataset_id in self.describe_by_dataset:
            return self.describe_by_dataset[dataset_id]
        if product_id is not None and product_id in self.describe_by_product:
            return self.describe_by_product[product_id]
        if dataset_id is not None:
            raise self.DatasetNotFound(f"unknown dataset id {dataset_id!r}")
        if self.describe_response is None:
            return FakeCatalogue([])
        return self.describe_response

    def subset(self, **kwargs: Any) -> Any:
        self.subset_calls.append(dict(kwargs))
        if self.subset_raises is not None:
            raise self.subset_raises
        return self.subset_response

    def login(self, **kwargs: Any) -> bool:
        self.login_calls.append(dict(kwargs))
        return True


@pytest.fixture
def fake_cm_module(
    monkeypatch: pytest.MonkeyPatch,
) -> FakeCmemsModule:
    """Inject a `copernicusmarine` stub into `sys.modules`."""
    fake = FakeCmemsModule()
    monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
    return fake
