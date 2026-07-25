"""The `AbstractDataSource` template-method defaults.

`_initialize`, `_create_grid` and `_api` used to be abstract, so all 48 backends
had to declare them even when the body was the same one-liner — 43 identical
`_create_grid`s, 25 `return None` `_initialize`s, 34 identical `_api`s. They now
have defaults on the base class and the identical overrides are gone. These tests
pin the defaults' behaviour, and that a backend can still override each one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import (
    AbstractDataSource,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)


class _Minimal(AbstractDataSource):
    """A backend that declares nothing but the two genuinely required hooks."""

    REQUIRES_TIME_WINDOW = False

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        return TemporalExtent(
            start_date=None,
            end_date=None,
            resolution="all",
            dates=pd.DatetimeIndex([]),
        )

    def download(self, progress_bar: bool = True, **kwargs):
        return self._api()


class _WithSearchFetch(_Minimal):
    """A backend using the search/fetch split, relying on the default `_api`."""

    def _search(self):
        return [RemoteProduct(id="a"), RemoteProduct(id="b")]

    def _fetch(self, products):
        return [product.id for product in products]


class _EmptySearch(_Minimal):
    """A backend whose search matches nothing."""

    def _search(self):
        return []

    def _fetch(self, products):  # pragma: no cover - must never be reached
        raise AssertionError("_fetch called for an empty search")


def _build(cls, tmp_path, **kwargs):
    kwargs.setdefault("variables", ["x"])
    kwargs.setdefault("lat_lim", [4.0, 5.0])
    kwargs.setdefault("lon_lim", [-75.0, -74.0])
    kwargs.setdefault("start", None)
    kwargs.setdefault("end", None)
    return cls(path=str(tmp_path), **kwargs)


class TestMinimalBackendIsConstructible:
    """Only `_check_input_dates` and `download` remain mandatory."""

    def test_backend_without_the_three_hooks_constructs(self, tmp_path):
        """A backend declaring neither hook still builds."""
        backend = _build(_Minimal, tmp_path)
        assert backend.space.west == -75.0

    def test_two_abstract_methods_remain(self):
        """`_check_input_dates` and `download` are the only abstract members."""
        assert AbstractDataSource.__abstractmethods__ == frozenset(
            {"_check_input_dates", "download"}
        )


class TestCreateGridDefault:
    """The default `_create_grid` wraps the bounds verbatim."""

    def test_default_wraps_bounds(self, tmp_path):
        """The bbox lands on `space` unchanged, with no snapping."""
        space = _build(_Minimal, tmp_path).space
        assert (space.south, space.north, space.west, space.east) == (
            4.0,
            5.0,
            -75.0,
            -74.0,
        )

    def test_default_returns_a_spatial_extent(self, tmp_path):
        """The result is the validated frozen value object, not a dict."""
        space = _build(_Minimal, tmp_path).space
        assert space == SpatialExtent.from_pairs(
            lat_lim=[4.0, 5.0], lon_lim=[-75.0, -74.0]
        )

    def test_default_carries_no_resolution(self, tmp_path):
        """No cell size is invented; a backend with one sets it itself."""
        assert _build(_Minimal, tmp_path).space.resolution is None

    def test_inverted_bounds_still_rejected(self, tmp_path):
        """Validation is not bypassed by moving the hook to the base class."""
        with pytest.raises(ValueError, match="latitude_min"):
            _build(_Minimal, tmp_path, lat_lim=[5.0, 4.0])

    def test_override_still_wins(self, tmp_path):
        """A backend that snaps its grid keeps control."""

        class _Snapped(_Minimal):
            def _create_grid(self, lat_lim, lon_lim):
                return SpatialExtent.from_pairs(
                    lat_lim=lat_lim, lon_lim=lon_lim, resolution=0.25
                )

        assert _build(_Snapped, tmp_path).space.resolution == 0.25


class TestInitializeDefault:
    """The default `_initialize` is a no-op returning `None`."""

    def test_default_binds_no_client(self, tmp_path):
        """With no override, nothing is bound to `self.client`."""
        backend = _build(_Minimal, tmp_path)
        assert "client" not in backend.__dict__

    def test_override_return_is_bound_to_client(self, tmp_path):
        """A non-None return is still captured onto `self.client`."""

        class _WithClient(_Minimal):
            def _initialize(self):
                return "connection"

        assert _build(_WithClient, tmp_path).client == "connection"

    def test_override_returning_none_binds_nothing(self, tmp_path):
        """An override that returns None behaves like the default."""

        class _SideEffectOnly(_Minimal):
            def _initialize(self):
                self.prepared = True
                return None

        backend = _build(_SideEffectOnly, tmp_path)
        assert backend.prepared is True
        assert "client" not in backend.__dict__


class TestApiDefault:
    """The default `_api` is the search→fetch composition."""

    def test_default_composes_search_and_fetch(self, tmp_path):
        """Every searched product is passed to `_fetch`, in order."""
        assert _build(_WithSearchFetch, tmp_path).download() == ["a", "b"]

    def test_empty_search_short_circuits(self, tmp_path):
        """An empty search returns `[]` without ever calling `_fetch`."""
        assert _build(_EmptySearch, tmp_path).download() == []

    def test_backend_without_search_raises_not_implemented(self, tmp_path):
        """A backend implementing neither path gets an actionable error."""
        backend = _build(_Minimal, tmp_path)
        with pytest.raises(NotImplementedError, match="does not implement _search"):
            backend.download()

    def test_override_still_wins(self, tmp_path):
        """A backend with an indivisible request keeps its own `_api`."""

        class _Bespoke(_Minimal):
            def _api(self):
                return "one-shot"

        assert _build(_Bespoke, tmp_path).download() == "one-shot"


class TestTemporalExtentFactories:
    """The three `TemporalExtent` archetypes the backends build through."""

    def test_whole_window_holds_both_bounds(self, tmp_path):
        """A whole-window extent carries the two bounds as its date axis."""
        backend = _build(_Minimal, tmp_path)
        extent = backend._whole_window_extent("2024-01-01", "2024-01-31", "%Y-%m-%d")
        assert len(extent.dates) == 2
        assert extent.resolution == "all"

    def test_whole_window_accepts_a_datetime(self, tmp_path):
        """The factory parses through `to_datetime`, so objects work too."""
        import datetime as dt

        backend = _build(_Minimal, tmp_path)
        extent = backend._whole_window_extent(
            dt.date(2024, 1, 1), dt.date(2024, 1, 2), "%Y-%m-%d"
        )
        assert extent.start_date == dt.datetime(2024, 1, 1)

    def test_whole_window_custom_resolution_label(self, tmp_path):
        """A backend can record its own label instead of `"all"`."""
        backend = _build(_Minimal, tmp_path)
        extent = backend._whole_window_extent(
            "2024-01-01", "2024-01-02", "%Y-%m-%d", resolution="raw"
        )
        assert extent.resolution == "raw"

    def test_cadence_expands_the_window(self, tmp_path):
        """A cadence extent expands to one entry per period start."""
        backend = _build(_Minimal, tmp_path)
        extent = backend._cadence_extent(
            "2024-01-01", "2024-03-01", "%Y-%m-%d", "monthly", {"monthly": "MS"}
        )
        assert len(extent.dates) == 3
        assert extent.resolution == "MS"

    def test_cadence_rejects_an_unknown_spelling(self, tmp_path):
        """An unsupported cadence raises rather than substituting a default."""
        backend = _build(_Minimal, tmp_path)
        with pytest.raises(ValueError, match="is not supported by _Minimal"):
            backend._cadence_extent(
                "2024-01-01", "2024-03-01", "%Y-%m-%d", "yearly", {"monthly": "MS"}
            )

    def test_static_extent_has_no_axis(self, tmp_path):
        """A static extent carries no bounds and an empty date axis."""
        extent = _build(_Minimal, tmp_path)._static_extent()
        assert extent.start_date is None
        assert len(extent.dates) == 0
        assert extent.resolution == "static"


class TestAuthenticateRunsBothPaths:
    """`authenticate()` opens a lazy client *and* configures a credential."""

    def test_both_paths_run_for_a_backend_with_both(self, tmp_path):
        """A backend with a lazy client and an auth object gets both, not one."""
        from earthlens.base import LazyClientMixin

        class _Auth:
            configured = False

            def configure(self):
                self.configured = True

        class _Both(LazyClientMixin, _Minimal):
            def _initialize(self):
                self._auth = _Auth()
                return None

            def _open_client(self):
                self.opened = True
                return "connection"

        backend = _build(_Both, tmp_path)
        backend.authenticate()
        assert backend.opened is True
        assert backend._auth.configured is True


class TestAntimeridianExtent:
    """A west-of-east bbox is reported as an antimeridian crossing."""

    def test_spatial_extent_names_the_crossing(self):
        """The validator explains the case rather than just "min > max"."""
        with pytest.raises(ValueError, match="antimeridian crossing"):
            SpatialExtent.from_pairs(lat_lim=[-10.0, 10.0], lon_lim=[170.0, -170.0])

    def test_message_suggests_the_two_halves(self):
        """The remedy names the split at ±180."""
        with pytest.raises(ValueError, match=r"\[170.0, 180\]"):
            SpatialExtent.from_pairs(lat_lim=[-10.0, 10.0], lon_lim=[170.0, -170.0])
