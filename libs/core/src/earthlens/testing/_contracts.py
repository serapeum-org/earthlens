"""Cross-backend contracts, as functions plus the `Base*` classes to subclass.

Four contracts apply to every backend — date parsing, the polygon `aoi=` mask,
`OUTPUT_KIND`, and the catalog shape — and core's test suite checks all four by
sweeping the registry. That sweep is worth keeping: it cannot be forgotten, so
a new in-repo backend is covered the moment its entry point lands.

What it cannot do is reach a backend that is *not* in this repo, and it reports
a failure against core's lane rather than the provider's. So the assertions
live here as functions, and the `Base*` classes below let a backend's own test
directory opt into the same checks the way `langchain-tests` and
`dbt-tests-adapter` do:

```python
# libs/providers/land/tests/wdpa/test_contract.py
from earthlens.testing import BaseBackendDateContract, BasePolygonAoiContract
from earthlens.wdpa import WDPA


class TestWdpaContracts(BaseBackendDateContract, BasePolygonAoiContract):
    backend = WDPA
    clips_to_polygon_mask = False
```

The classes are named `Base*` rather than `Test*` so pytest does not collect
the bases themselves and run every contract twice — the same reason
dbt-tests-adapter uses that prefix.

Import from `earthlens.testing`, not from here.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from typing import Any, get_args

import pytest

from earthlens.base import AbstractDataSource

#: The date spellings the facade documents as equivalent, all meaning the same
#: day. A backend that parses one differently from another has reintroduced the
#: raw `strptime` the shared `to_datetime` helper exists to replace.
EQUIVALENT_DATE_FORMS: dict[str, Any] = {
    "iso-date-string": "2024-01-05",
    "datetime": dt.datetime(2024, 1, 5),
    "date": dt.date(2024, 1, 5),
}


def assert_no_raw_date_parsing(
    backend: type[AbstractDataSource], label: str | None = None
) -> None:
    """Assert `_check_input_dates` delegates instead of parsing dates itself.

    Args:
        backend: The `AbstractDataSource` subclass to inspect.
        label: Name to use in the failure message; defaults to the class name.
    """
    name = label or backend.__name__
    source = inspect.getsource(backend._check_input_dates)
    tree = ast.parse(source.lstrip())
    body = [
        node
        for node in tree.body[0].body  # type: ignore[attr-defined]
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    code = "\n".join(ast.unparse(node) for node in body)
    assert "strptime(" not in code, f"{name} still parses dates with strptime"
    assert "pd.to_datetime(" not in code, f"{name} still parses dates with pandas"


def parse_dates_unbound(backend: type[AbstractDataSource], value: Any) -> Any:
    """Parse a date on an uninitialised instance, or return `None`.

    `object.__new__` skips `__init__` — no network, no catalog load — while
    still giving real bound methods, so the hook can reach the inherited
    `_whole_window_extent` / `_cadence_extent` factories. A backend whose hook
    also reads constructor-set state (s3 and drought read the resolved dataset
    row) raises `AttributeError`; the static guard covers those.

    Args:
        backend: The `AbstractDataSource` subclass.
        value: The `start` / `end` value to parse.

    Returns:
        The parsed `TemporalExtent`, or `None` when the hook needs state.
    """
    instance: AbstractDataSource = object.__new__(backend)
    try:
        return instance._check_input_dates(value, value, "daily", "%Y-%m-%d")
    except AttributeError:
        return None


def assert_equivalent_date_forms_agree(
    backend: type[AbstractDataSource], label: str | None = None
) -> None:
    """Assert a string, a `datetime` and a `date` for one day parse alike.

    Args:
        backend: The `AbstractDataSource` subclass.
        label: Name to use in the failure message.
    """
    name = label or backend.__name__
    results = {
        form: parse_dates_unbound(backend, value)
        for form, value in EQUIVALENT_DATE_FORMS.items()
    }
    if any(extent is None for extent in results.values()):
        pytest.skip(f"{name} needs instance state to resolve its cadence")
    starts = {form: extent.start_date for form, extent in results.items()}
    assert len(set(starts.values())) == 1, (
        f"{name} parses the forms differently: {starts}"
    )


def assert_iso_datetime_string_accepted(
    backend: type[AbstractDataSource], label: str | None = None
) -> None:
    """Assert a sub-daily ISO string parses even against a date-only `fmt`.

    Args:
        backend: The `AbstractDataSource` subclass.
        label: Name to use in the failure message.
    """
    name = label or backend.__name__
    extent = parse_dates_unbound(backend, "2024-01-05T06:30:00")
    if extent is None:
        pytest.skip(f"{name} needs instance state to resolve its cadence")
    if extent.start_date is None:
        pytest.skip(f"{name} has no time axis")
    assert extent.start_date == dt.datetime(2024, 1, 5, 6, 30)


def assert_capability_flags_are_bools(
    backend: type[AbstractDataSource], label: str | None = None
) -> None:
    """Assert the opt-in capability flags are real `bool`s, not truthy stand-ins.

    Args:
        backend: The `AbstractDataSource` subclass.
        label: Name to use in the failure message.
    """
    name = label or backend.__name__
    for flag in ("REQUIRES_TIME_WINDOW", "SUPPORTS_POLYGON_AOI"):
        value = getattr(backend, flag)
        assert isinstance(value, bool), f"{name}.{flag} is {value!r}, not a bool"


def assert_polygon_support_matches(
    backend: type[AbstractDataSource],
    *,
    clips_to_polygon_mask: bool,
    label: str | None = None,
) -> None:
    """Assert the declared polygon capability matches what the backend does.

    A backend that only honours the bounding box must leave
    `SUPPORTS_POLYGON_AOI` false, so the facade warns rather than silently
    returning more data than the mask asked for.

    Args:
        backend: The `AbstractDataSource` subclass.
        clips_to_polygon_mask: What the backend actually does, declared by the
            subclass the way LangChain's standard tests declare a capability.
        label: Name to use in the failure message.
    """
    name = label or backend.__name__
    assert backend.SUPPORTS_POLYGON_AOI is clips_to_polygon_mask, (
        f"{name}.SUPPORTS_POLYGON_AOI is {backend.SUPPORTS_POLYGON_AOI}, but the "
        f"contract declares clips_to_polygon_mask={clips_to_polygon_mask}"
    )


def assert_output_kind_is_declared(
    backend: type[AbstractDataSource], label: str | None = None
) -> None:
    """Assert `OUTPUT_KIND` is one of the literal values the facade branches on.

    Args:
        backend: The `AbstractDataSource` subclass.
        label: Name to use in the failure message.
    """
    from earthlens.base import OutputKind

    name = label or backend.__name__
    allowed = set(get_args(OutputKind))
    assert backend.OUTPUT_KIND in allowed, (
        f"{name}.OUTPUT_KIND is {backend.OUTPUT_KIND!r}, not one of {sorted(allowed)}"
    )


def assert_catalog_loader_shape(catalog_module: Any, label: str | None = None) -> None:
    """Assert a backend's `catalog.py` exposes the shared loader surface.

    Every catalog module carries a module-level `CATALOG_PATH`, a
    `clear_catalog_cache()` and a pydantic `Catalog` — the contract
    `catalog-consistency.md` describes and the sharded and single-file layouts
    both satisfy.

    Args:
        catalog_module: The backend's imported `catalog` module.
        label: Name to use in the failure message.
    """
    name = label or getattr(catalog_module, "__name__", "catalog")
    for attribute in ("CATALOG_PATH", "clear_catalog_cache", "Catalog"):
        assert hasattr(catalog_module, attribute), (
            f"{name} does not expose `{attribute}`, which the shared catalog "
            "loader contract requires"
        )


class BaseBackendDateContract:
    """Subclass with `backend = <YourBackend>` to pin the date contract.

    Attributes:
        backend: The `AbstractDataSource` subclass under test.
    """

    backend: type[AbstractDataSource]

    def test_no_raw_date_parsing(self) -> None:
        """The hook delegates to the shared parser, never to `strptime`."""
        assert_no_raw_date_parsing(self.backend)

    def test_equivalent_date_forms_agree(self) -> None:
        """A string, a `datetime` and a `date` for one day parse the same."""
        assert_equivalent_date_forms_agree(self.backend)

    def test_iso_datetime_string_accepted(self) -> None:
        """A sub-daily ISO string parses against a date-only `fmt`."""
        assert_iso_datetime_string_accepted(self.backend)

    def test_capability_flags_are_bools(self) -> None:
        """The opt-in flags are real booleans."""
        assert_capability_flags_are_bools(self.backend)


class BasePolygonAoiContract:
    """Subclass to declare whether the backend clips to a polygon mask.

    Attributes:
        backend: The `AbstractDataSource` subclass under test.
        clips_to_polygon_mask: `True` only when the backend crops to the exact
            polygon rather than its bounding box. Opt-in, so a new backend
            cannot claim support by inheriting it.
    """

    backend: type[AbstractDataSource]
    clips_to_polygon_mask: bool = False

    def test_polygon_support_is_declared_honestly(self) -> None:
        """`SUPPORTS_POLYGON_AOI` agrees with what the backend really does."""
        assert_polygon_support_matches(
            self.backend, clips_to_polygon_mask=self.clips_to_polygon_mask
        )


class BaseOutputKindContract:
    """Subclass to pin the backend's declared output shape.

    Attributes:
        backend: The `AbstractDataSource` subclass under test.
        output_kind: The value `OUTPUT_KIND` is expected to carry.
    """

    backend: type[AbstractDataSource]
    output_kind: str = "raster"

    def test_output_kind_is_a_declared_literal(self) -> None:
        """`OUTPUT_KIND` is one of the values the facade branches on."""
        assert_output_kind_is_declared(self.backend)

    def test_output_kind_is_the_declared_one(self) -> None:
        """The backend carries the shape its contract declares."""
        assert self.backend.OUTPUT_KIND == self.output_kind, (
            f"{self.backend.__name__}.OUTPUT_KIND is "
            f"{self.backend.OUTPUT_KIND!r}, not {self.output_kind!r}"
        )


class BaseCatalogContract:
    """Subclass with `catalog_module = earthlens.<backend>.catalog`.

    Attributes:
        catalog_module: The backend's imported `catalog` module.
    """

    catalog_module: Any

    def test_catalog_module_exposes_the_loader_surface(self) -> None:
        """`CATALOG_PATH`, `clear_catalog_cache` and `Catalog` are all present."""
        assert_catalog_loader_shape(self.catalog_module)
