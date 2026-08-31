"""Cross-backend guard for the `start` / `end` parsing contract.

The facade types `start` / `end` as `str | datetime | date | None` and documents
the lenient ISO fallback, but each backend does its own parsing in
`_check_input_dates`. These tests pin that contract for **every** registered
backend at once, so a new backend cannot reintroduce a raw `strptime` that
rejects a `datetime` or a `"…T06:00:00"` string.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect

import pytest

from earthlens.earthlens import EarthLens

#: One canonical key per distinct backend module (aliases collapse).
_BACKENDS = sorted(
    {module: key for key, module, _ in EarthLens.DataSources.entries()}.items()
)

#: The date forms the facade promises to accept, all denoting 2024-01-05.
_EQUIVALENT_FORMS = {
    "iso-date-string": "2024-01-05",
    "datetime": dt.datetime(2024, 1, 5),
    "date": dt.date(2024, 1, 5),
}


def _backend_classes():
    """Yield `(key, class)` for every importable registered backend."""
    for _module, key in _BACKENDS:
        try:
            yield key, EarthLens.DataSources[key]
        except ImportError:  # pragma: no cover - optional SDK absent
            continue


_CASES = list(_backend_classes())
#: Test ids use the backend class name, not the registry key: `entries()` yields
#: several aliases per module and the surviving key is whichever comes last, so
#: ids like `gdo` (drought) or `etopo` (bathymetry) would name the alias rather
#: than the backend a reader is looking for.
_IDS = [backend.__name__ for _key, backend in _CASES]


class TestNoRawDateParsing:
    """No backend parses user dates with `strptime` / `pd.to_datetime`."""

    @pytest.mark.parametrize("key,backend", _CASES, ids=_IDS)
    def test_hook_delegates_to_to_datetime(self, key, backend):
        """`_check_input_dates` must not call strptime or pd.to_datetime directly."""
        hook = backend._check_input_dates
        source = inspect.getsource(hook)
        tree = ast.parse(source.lstrip())
        body = [
            node
            for node in tree.body[0].body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        code = "\n".join(ast.unparse(node) for node in body)
        assert "strptime(" not in code, f"{key} still parses dates with strptime"
        assert "pd.to_datetime(" not in code, f"{key} still parses dates with pandas"


class TestFlexibleDateForms:
    """Every backend accepts a string, a `datetime`, and a `date` alike."""

    @staticmethod
    def _extent(backend, value):
        """Parse dates on an uninitialised instance, or None if state is needed.

        `object.__new__` skips `__init__` (no network, no catalog load) while
        still giving real bound methods, so the hook can reach the inherited
        `_whole_window_extent` / `_cadence_extent` factories. A backend whose
        hook also reads constructor-set state (s3 and drought read the resolved
        dataset row) raises `AttributeError` and is covered by the static guard.
        """
        instance = object.__new__(backend)
        try:
            return instance._check_input_dates(value, value, "daily", "%Y-%m-%d")
        except AttributeError:
            return None

    @pytest.mark.parametrize("key,backend", _CASES, ids=_IDS)
    def test_equivalent_forms_agree(self, key, backend):
        """A string, datetime, and date for the same day give the same extent."""
        results = {
            name: self._extent(backend, value)
            for name, value in _EQUIVALENT_FORMS.items()
        }
        if any(extent is None for extent in results.values()):
            pytest.skip(f"{key} needs instance state to resolve its cadence")
        starts = {name: extent.start_date for name, extent in results.items()}
        assert len(set(starts.values())) == 1, (
            f"{key} parses the forms differently: {starts}"
        )

    @pytest.mark.parametrize("key,backend", _CASES, ids=_IDS)
    def test_iso_datetime_string_accepted(self, key, backend):
        """A sub-daily `YYYY-MM-DDTHH:MM:SS` string parses despite a date-only fmt."""
        extent = self._extent(backend, "2024-01-05T06:30:00")
        if extent is None:
            pytest.skip(f"{key} needs instance state to resolve its cadence")
        if extent.start_date is None:
            pytest.skip(f"{key} has no time axis")
        assert extent.start_date == dt.datetime(2024, 1, 5, 6, 30)


#: The backends with no per-step time axis, which accept a missing `start` /
#: `end`. Pinned by class name (registry keys include aliases, so the canonical
#: key is not unique) so flipping the flag on a backend that really needs a
#: window — or dropping it from one that does not — fails here.
_NO_WINDOW_REQUIRED = {
    "AdminBoundaries",
    "Aqueduct",
    "Bathymetry",
    "CatRaRE",
    "DEM",
    "EMDAT",
    "FABDEM",
    "FLODIS",
    "FLOPROS",
    "Glaciers",
    "HANZE",
    "JRC",
    "NSI",
    "OSM",
    "Overture",
    "RiskIndicators",
    "SoilGrids",
    "SolarWindAtlas",
}


class TestRequiresTimeWindowDeclared:
    """The `REQUIRES_TIME_WINDOW` opt-out roster is explicit and stable."""

    def test_opt_out_roster_is_exactly_the_snapshot_backends(self):
        """Only the documented no-time-axis backends opt out of the window guard."""
        opted_out = {
            backend.__name__
            for _key, backend in _CASES
            if not backend.REQUIRES_TIME_WINDOW
        }
        assert opted_out == _NO_WINDOW_REQUIRED

    @pytest.mark.parametrize("key,backend", _CASES, ids=_IDS)
    def test_flag_is_a_bool(self, key, backend):
        """Every backend resolves the flag to a real bool, not a truthy stand-in."""
        assert isinstance(backend.REQUIRES_TIME_WINDOW, bool)
