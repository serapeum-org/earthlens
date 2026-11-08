"""Cross-backend guard for the `start` / `end` parsing contract.

The facade types `start` / `end` as `str | datetime | date | None` and documents
the lenient ISO fallback, but each backend does its own parsing in
`_check_input_dates`. These tests pin that contract for **every** registered
backend at once, so a new backend cannot reintroduce a raw `strptime` that
rejects a `datetime` or a `"…T06:00:00"` string.

The assertions themselves live in `earthlens.testing` so a provider's own test
directory — or a backend shipped outside this repo — can subclass
`BaseBackendDateContract` and get exactly these checks, attributed to its own
lane. This module is the registry sweep over them: it cannot be forgotten when
a backend is added, which the per-provider subclass can.
"""

from __future__ import annotations

import pytest

from earthlens.earthlens import EarthLens
from earthlens.testing import (
    EQUIVALENT_DATE_FORMS,
    assert_capability_flags_are_bools,
    assert_equivalent_date_forms_agree,
    assert_iso_datetime_string_accepted,
    assert_no_raw_date_parsing,
)

#: One canonical key per distinct backend module (aliases collapse).
_BACKENDS = sorted(
    {module: key for key, module, _ in EarthLens.DataSources.entries()}.items()
)


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
        assert_no_raw_date_parsing(backend, key)


class TestFlexibleDateForms:
    """Every backend accepts a string, a `datetime`, and a `date` alike."""

    @pytest.mark.parametrize("key,backend", _CASES, ids=_IDS)
    def test_equivalent_forms_agree(self, key, backend):
        """A string, datetime, and date for the same day give the same extent."""
        assert_equivalent_date_forms_agree(backend, key)

    @pytest.mark.parametrize("key,backend", _CASES, ids=_IDS)
    def test_iso_datetime_string_accepted(self, key, backend):
        """A sub-daily `YYYY-MM-DDTHH:MM:SS` string parses despite a date-only fmt."""
        assert_iso_datetime_string_accepted(backend, key)

    def test_the_shared_forms_are_the_ones_documented(self):
        """The subclassable contract and this sweep agree on what is equivalent."""
        assert set(EQUIVALENT_DATE_FORMS) == {
            "iso-date-string",
            "datetime",
            "date",
        }


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
        assert_capability_flags_are_bools(backend, key)
