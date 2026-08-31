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
        extent = backend._whole_window_extent(
            "2024-01-01", "2024-01-31", fmt="%Y-%m-%d"
        )
        assert len(extent.dates) == 2
        assert extent.resolution == "all"

    def test_whole_window_accepts_a_datetime(self, tmp_path):
        """The factory parses through `to_datetime`, so objects work too."""
        import datetime as dt

        backend = _build(_Minimal, tmp_path)
        extent = backend._whole_window_extent(
            dt.date(2024, 1, 1), dt.date(2024, 1, 2), fmt="%Y-%m-%d"
        )
        assert extent.start_date == dt.datetime(2024, 1, 1)

    def test_whole_window_custom_resolution_label(self, tmp_path):
        """A backend can record its own label instead of `"all"`."""
        backend = _build(_Minimal, tmp_path)
        extent = backend._whole_window_extent(
            "2024-01-01", "2024-01-02", fmt="%Y-%m-%d", resolution="raw"
        )
        assert extent.resolution == "raw"

    def test_cadence_expands_the_window(self, tmp_path):
        """A cadence extent expands to one entry per period start."""
        backend = _build(_Minimal, tmp_path)
        extent = backend._cadence_extent(
            "2024-01-01",
            "2024-03-01",
            fmt="%Y-%m-%d",
            cadence="monthly",
            accepted={"monthly": "MS"},
        )
        assert len(extent.dates) == 3
        assert extent.resolution == "MS"

    def test_cadence_rejects_an_unknown_spelling(self, tmp_path):
        """An unsupported cadence raises rather than substituting a default."""
        backend = _build(_Minimal, tmp_path)
        with pytest.raises(ValueError, match="is not supported by _Minimal"):
            backend._cadence_extent(
                "2024-01-01",
                "2024-03-01",
                fmt="%Y-%m-%d",
                cadence="yearly",
                accepted={"monthly": "MS"},
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


class TestRunItems:
    """The shared partial-failure loop behind the `errors=` convention."""

    @staticmethod
    def _backend(tmp_path):
        """Build the minimal backend used to exercise `_run_items`."""
        return _build(_Minimal, tmp_path)

    def test_all_succeed(self, tmp_path):
        """Every result is collected, in order, with no failures."""
        results, failures = self._backend(tmp_path)._run_items(
            [1, 2, 3], lambda n: n * 2
        )
        assert results == [2, 4, 6]
        assert failures == []

    def test_warn_continues_past_a_failure(self, tmp_path):
        """The default policy skips the bad item and keeps the good ones."""

        def flaky(n):
            if n == 2:
                raise RuntimeError("boom")
            return n

        results, failures = self._backend(tmp_path)._run_items([1, 2, 3], flaky)
        assert results == [1, 3]
        assert [described for described, _ in failures] == ["2"]

    def test_raise_propagates_the_first_failure(self, tmp_path):
        """`errors="raise"` aborts instead of collecting."""

        def flaky(n):
            if n == 2:
                raise RuntimeError("boom")
            return n

        backend = self._backend(tmp_path)
        with pytest.raises(RuntimeError, match="boom"):
            backend._run_items([1, 2, 3], flaky, errors="raise")

    def test_ignore_is_silent_but_still_reports(self, tmp_path):
        """`errors="ignore"` logs nothing yet still returns the failure list."""

        def flaky(n):
            raise RuntimeError("boom")

        results, failures = self._backend(tmp_path)._run_items(
            [1], flaky, errors="ignore"
        )
        assert results == []
        assert len(failures) == 1

    def test_skip_is_an_alias_for_ignore(self, tmp_path):
        """nwp shipped `"skip"` before the convention settled; it still works."""
        results, failures = self._backend(tmp_path)._run_items(
            [1], lambda n: n, errors="skip"
        )
        assert results == [1]
        assert failures == []

    def test_unknown_policy_rejected(self, tmp_path):
        """A policy outside the accepted set raises with the accepted names."""
        backend = self._backend(tmp_path)
        with pytest.raises(ValueError, match="errors must be"):
            backend._run_items([1], lambda n: n, errors="continue")

    def test_on_failure_keeps_results_aligned(self, tmp_path):
        """A placeholder keeps one result per item, as the vector backends need."""

        def flaky(n):
            if n == 2:
                raise RuntimeError("boom")
            return n

        results, failures = self._backend(tmp_path)._run_items(
            [1, 2, 3], flaky, on_failure=lambda item, _exc: f"empty-{item}"
        )
        assert results == [1, "empty-2", 3]
        assert len(failures) == 1

    def test_describe_labels_the_failure(self, tmp_path):
        """`describe` renders the item for the failure record."""

        def flaky(item):
            raise RuntimeError("boom")

        _results, failures = self._backend(tmp_path)._run_items(
            [{"id": "abc"}], flaky, describe=lambda item: item["id"]
        )
        assert failures[0][0] == "abc"

    def test_empty_items_is_a_noop(self, tmp_path):
        """No items means no results, no failures, and no logging."""
        assert self._backend(tmp_path)._run_items([], lambda n: n) == ([], [])


class TestLazyRootDir:
    """`root_dir` is resolved at construction but only created on download."""

    def test_construction_creates_no_directory(self, tmp_path):
        """Building a backend must not touch the filesystem."""
        target = tmp_path / "not-yet"
        backend = _build(_WithSearchFetch, target)
        assert backend.root_dir == target.absolute(), f"got {backend.root_dir}"
        assert not target.exists(), "construction must leave no directory behind"

    def test_download_creates_the_directory(self, tmp_path):
        """The first download materialises root_dir."""
        target = tmp_path / "made-on-demand"
        backend = _build(_WithSearchFetch, target)
        backend.download()
        assert target.is_dir(), "download() must create root_dir"

    def test_download_creates_nested_parents(self, tmp_path):
        """A multi-level path is created in full, not just its last segment."""
        target = tmp_path / "a" / "b" / "c"
        _build(_WithSearchFetch, target).download()
        assert target.is_dir(), "nested parents must be created too"

    def test_download_tolerates_an_existing_directory(self, tmp_path):
        """Downloading twice is fine; the second call is a no-op."""
        target = tmp_path / "twice"
        backend = _build(_WithSearchFetch, target)
        backend.download()
        backend.download()
        assert target.is_dir()

    def test_ensure_root_dir_returns_the_path(self, tmp_path):
        """The helper hands back the directory it just guaranteed."""
        target = tmp_path / "returned"
        backend = _build(_WithSearchFetch, target)
        assert backend._ensure_root_dir() == target.absolute()
        assert target.is_dir()

    def test_a_rejected_request_leaves_no_directory(self, tmp_path):
        """A constructor that raises must not have created the output dir first."""
        target = tmp_path / "rejected"

        class _Strict(_WithSearchFetch):
            REQUIRES_TIME_WINDOW = True

        with pytest.raises(ValueError):
            _build(_Strict, target)
        assert not target.exists(), "a rejected request must leave no directory"

    def test_wrapper_preserves_the_download_signature(self):
        """functools.wraps keeps the backend's own download introspectable."""
        import inspect

        assert _WithSearchFetch.download.__name__ == "download"
        params = inspect.signature(_WithSearchFetch.download).parameters
        assert "progress_bar" in params, f"got {list(params)}"

    def test_wrapper_is_installed_once_per_class(self, tmp_path):
        """A subclass that does not redefine download is not double-wrapped."""

        class _Inheriting(_WithSearchFetch):
            pass

        assert _Inheriting.download is _WithSearchFetch.download
        target = tmp_path / "inherited"
        _build(_Inheriting, target).download()
        assert target.is_dir()

    def test_download_return_value_is_passed_through(self, tmp_path):
        """The wrapper returns whatever the backend's download returned."""

        class _Returns(_WithSearchFetch):
            def download(self, progress_bar: bool = True, **kwargs):
                return ["sentinel"]

        assert _build(_Returns, tmp_path / "rv").download() == ["sentinel"]

    def test_wrap_download_is_idempotent(self, tmp_path):
        """Re-wrapping an already-wrapped download leaves it untouched."""

        class _Once(_WithSearchFetch):
            def download(self, progress_bar: bool = True, **kwargs):
                return self._api()

        wrapped = _Once.download
        _Once._wrap_download()
        assert _Once.download is wrapped, "download must not be wrapped twice"
        _build(_Once, tmp_path / "idem").download()
        assert (tmp_path / "idem").is_dir()

    def test_every_registered_backend_download_is_wrapped(self):
        """No backend escapes the wrapper — including those with no own __init__."""
        from earthlens.earthlens import EarthLens

        unwrapped = [
            key
            for key, _module, _extra in EarthLens.DataSources.entries()
            if not getattr(
                EarthLens.DataSources[key].download, "_ensures_root_dir", False
            )
        ]
        assert unwrapped == [], f"these backends would not create root_dir: {unwrapped}"


class TestIsComplete:
    """ARC-15: the shared skip-if-exists / resume check."""

    def _backend(self, tmp_path):
        """Build a minimal backend rooted at `tmp_path`."""
        return _build(_WithSearchFetch, tmp_path)

    def test_missing_file_is_incomplete(self, tmp_path):
        """A path that does not exist is never reusable."""
        backend = self._backend(tmp_path)
        assert backend._is_complete(tmp_path / "absent.tif") is False

    def test_empty_file_is_incomplete(self, tmp_path):
        """A zero-byte file is the classic failed-download leftover."""
        target = tmp_path / "empty.tif"
        target.write_bytes(b"")
        assert self._backend(tmp_path)._is_complete(target) is False

    def test_written_file_is_complete(self, tmp_path):
        """A non-empty file is reusable when no size is known."""
        target = tmp_path / "grid.tif"
        target.write_bytes(b"raster-bytes")
        assert self._backend(tmp_path)._is_complete(target) is True

    def test_expected_size_catches_a_truncated_file(self, tmp_path):
        """A short file is rejected once the real size is known."""
        target = tmp_path / "half.tif"
        target.write_bytes(b"12345")
        assert self._backend(tmp_path)._is_complete(target, expected_size=10) is False

    def test_expected_size_accepts_an_exact_match(self, tmp_path):
        """A file of exactly the advertised size is complete."""
        target = tmp_path / "whole.tif"
        target.write_bytes(b"12345")
        assert self._backend(tmp_path)._is_complete(target, expected_size=5) is True

    def test_expected_size_rejects_an_overlong_file(self, tmp_path):
        """More bytes than advertised is as wrong as fewer."""
        target = tmp_path / "long.tif"
        target.write_bytes(b"1234567890")
        assert self._backend(tmp_path)._is_complete(target, expected_size=5) is False

    def test_force_ignores_a_complete_file(self, tmp_path):
        """force=True re-fetches even when the file is present and sized right."""
        target = tmp_path / "grid.tif"
        target.write_bytes(b"12345")
        backend = self._backend(tmp_path)
        assert backend._is_complete(target, expected_size=5, force=True) is False

    def test_accepts_a_string_path(self, tmp_path):
        """A str path works as well as a Path."""
        target = tmp_path / "grid.tif"
        target.write_bytes(b"bytes")
        assert self._backend(tmp_path)._is_complete(str(target)) is True

    def test_a_directory_is_not_a_complete_download(self, tmp_path):
        """A directory at the destination must not be mistaken for a file."""
        target = tmp_path / "adir"
        target.mkdir()
        assert self._backend(tmp_path)._is_complete(target, expected_size=0) is False


class TestNoOverrideSilencers:
    """ARC-5(c): no backend may silence an override error on a base hook.

    A `# type: ignore[override]` on `_fetch` / `_fetch_one` meant the
    subclass signature did not match the base, which is what let ten
    mutually incompatible shapes accumulate — and it disabled the one
    automated check that would have caught the drift.
    """

    #: Base hooks whose signature every backend must actually match.
    HOOKS = ("_fetch", "_fetch_one", "_search", "_api", "_initialize")

    def _backend_sources(self):
        """Yield `(path, source)` for every provider backend module."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            yield path, path.read_text(encoding="utf-8")

    def test_no_hook_carries_an_override_silencer(self):
        """No `def _hook(...)  # type: ignore[override]` survives."""
        import re

        offenders = []
        for path, source in self._backend_sources():
            for hook in self.HOOKS:
                pattern = (
                    rf"def {hook}\((?:[^)]*\))?[^\n]*type:\s*ignore\[[^]]*override"
                )
                if re.search(pattern, source):
                    offenders.append(f"{path.parent.name}.{hook}")
        assert offenders == [], (
            "these hooks silence an override mismatch instead of matching the "
            f"base signature: {offenders}"
        )

    def test_backend_modules_were_actually_scanned(self):
        """Guard the guard: the glob must find the real backend modules."""
        found = [path.parent.name for path, _ in self._backend_sources()]
        assert len(found) > 30, f"only scanned {len(found)} backends: {found[:5]}"
        assert "nwp" in found, f"nwp missing from the scan: {found[:8]}"
        assert "soilgrids" in found, f"soilgrids missing from the scan: {found[:8]}"

    def test_fetch_one_takes_only_a_product(self):
        """Every `_fetch_one` override keeps the base's single-argument shape."""
        import ast as ast_module

        offenders = []
        for path, source in self._backend_sources():
            for node in ast_module.walk(ast_module.parse(source)):
                if (
                    isinstance(node, ast_module.FunctionDef)
                    and node.name == "_fetch_one"
                ):
                    names = [a.arg for a in node.args.args]
                    if names[1:] != ["product"]:
                        offenders.append(f"{path.parent.name}: {names}")
        assert offenders == [], (
            "`_fetch_one` must take exactly `(self, product)`; extra per-batch "
            f"context belongs on `self` or in `RemoteProduct.metadata`: {offenders}"
        )


class TestBackendInheritanceIsGuarded:
    """ARC-6: the no-backend-subclasses-a-backend rule is enforced, not assumed.

    Both a parent and a child backend get an `__init__` wrapper, so an ergonomic
    kwarg forwarded to `super().__init__()` would be resolved twice — `resolve_aoi`
    running on an already-reduced bbox. That produces a plausible-looking box over
    roughly the right area, which is why it is worth failing at class-definition
    time rather than trusting a docstring note.
    """

    def test_subclassing_with_its_own_init_is_refused(self):
        """A child declaring its own `__init__` earns a second wrapper, so it raises."""
        with pytest.raises(TypeError, match="resolved twice"):

            class _Child(_Minimal):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)

    def test_the_error_names_the_opt_out(self):
        """The message tells the author how to declare the safe case."""
        with pytest.raises(TypeError, match="ergonomics_resolved=True"):

            class _Child(_Minimal):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)

    def test_subclassing_without_an_init_is_allowed(self):
        """A child that inherits the constructor cannot double-resolve, so it is legal.

        This is the shape the test helpers in this module use, and it was the
        first thing the guard wrongly rejected.
        """

        class _Child(_Minimal):
            pass

        assert issubclass(_Child, _Minimal)

    def test_opting_in_is_allowed_and_skips_the_second_wrap(self):
        """`ergonomics_resolved=True` permits the subclass and wraps only download."""

        class _Child(_Minimal, ergonomics_resolved=True):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

            def download(self, progress_bar: bool = True, **kwargs):
                return ["child"]

        # The download wrapper is still applied (root_dir creation), which is the
        # piece a subclass must not lose.
        assert getattr(_Child.download, "_ensures_root_dir", False)

    def test_every_shipped_backend_inherits_the_abc_directly(self):
        """No shipped backend relies on the opt-out, so the simple case holds."""
        import ast as ast_module
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3] / "providers"
        offenders = []
        defined: dict[str, str] = {}
        trees = {}
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            tree = ast_module.parse(path.read_text(encoding="utf-8"))
            trees[path] = tree
            for node in tree.body:
                if isinstance(node, ast_module.ClassDef) and any(
                    "AbstractDataSource" in ast_module.unparse(b) for b in node.bases
                ):
                    defined[node.name] = path.parent.name
        for path, tree in trees.items():
            for node in tree.body:
                if not isinstance(node, ast_module.ClassDef):
                    continue
                for base in node.bases:
                    name = ast_module.unparse(base).split(".")[-1]
                    if name in defined:
                        offenders.append(f"{node.name} <- {name} ({path.parent.name})")
        assert offenders == [], (
            f"a backend subclasses another backend: {offenders}. Read the "
            f"__init_subclass__ docstring before adding `ergonomics_resolved=True`."
        )


class TestConcurrencyContractIsDocumented:
    """ARC-11: pin the pickling facts the class docstring states.

    Documentation about what does and does not survive a process boundary drifts
    silently, and the answer here is non-obvious: a bare `requests.Session`
    pickles, an `HttpClient` does not, so a backend flips from picklable to
    unpicklable the first time it caches a client.
    """

    def test_a_bare_session_pickles(self):
        """The surprising half: `requests.Session` itself is picklable."""
        import pickle

        import requests

        assert pickle.loads(pickle.dumps(requests.Session())) is not None

    def test_an_http_client_does_not_pickle(self):
        """`HttpClient` holds the throttle lock, and locks do not pickle."""
        import pickle

        from earthlens.base.http import HttpClient

        client = HttpClient()
        with pytest.raises((TypeError, AttributeError)):
            pickle.dumps(client)

    def test_a_fresh_backend_pickles(self, tmp_path):
        """Before any client materialises, a backend crosses a process boundary."""
        import pickle

        backend = _build(_Minimal, tmp_path)
        assert pickle.loads(pickle.dumps(backend)) is not None

    def test_a_backend_holding_a_client_does_not(self, tmp_path):
        """Caching an `HttpClient` is what makes it unpicklable — the `_http` slot."""
        import pickle

        from earthlens.base.http import HttpClient

        backend = _build(_Minimal, tmp_path)
        backend._http = HttpClient()
        with pytest.raises((TypeError, AttributeError)):
            pickle.dumps(backend)


class TestHooksReturnOneShape:
    """ARC-7: `_create_grid` / `_check_input_dates` return their model, only."""

    def _hook_returns(self, hook: str):
        """Yield `(backend_name, {return kinds})` for each override of `hook`."""
        import ast as ast_module
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            tree = ast_module.parse(path.read_text(encoding="utf-8"))
            for node in ast_module.walk(tree):
                if not (isinstance(node, ast_module.FunctionDef) and node.name == hook):
                    continue
                kinds = set()
                returns = [
                    n for n in ast_module.walk(node) if isinstance(n, ast_module.Return)
                ]
                for ret in returns:
                    if ret.value is None or (
                        isinstance(ret.value, ast_module.Constant)
                        and ret.value.value is None
                    ):
                        kinds.add("None")
                    elif isinstance(ret.value, ast_module.Dict):
                        kinds.add("dict")
                    else:
                        kinds.add("model")
                if not returns:
                    kinds.add("None")
                yield path.parent.name, kinds

    def test_overrides_were_found(self):
        """Guard the guard: the walk must find the `_check_input_dates` overrides."""
        found = list(self._hook_returns("_check_input_dates"))
        assert len(found) > 40, f"only found {len(found)}"

    @pytest.mark.parametrize("hook", ["_create_grid", "_check_input_dates"])
    def test_no_override_returns_a_dict_or_none(self, hook):
        """`__init__` assigns the result directly, so a dict or `None` breaks it.

        These hooks used to accept three return shapes and `__init__` branched on
        `isinstance` to cope. Every override returned the model, so the other two
        branches were dead and are gone — which means a dict or `None` now leaves
        `space` / `time` wrong instead of being quietly converted.
        """
        offenders = [
            f"{name}: {sorted(kinds)}"
            for name, kinds in self._hook_returns(hook)
            if kinds - {"model"}
        ]
        assert offenders == [], (
            f"{hook} must return its extent model; these do not: {offenders}"
        )


class TestErgonomicsResolvedNeedsABackendBase:
    """`ergonomics_resolved=True` is only meaningful under a backend parent.

    The flag says "my `__init__` forwards only resolved parameters, so do not
    wrap it a second time". On a class inheriting `AbstractDataSource` directly
    there is no first wrapper, so honouring it would quietly strip that
    backend's own ergonomic kwargs (`aoi=`, `buffer=`, `cadence=`, `dataset=`)
    — the opposite of the flag's purpose, and invisible until a user passed one.
    """

    def test_declaring_it_without_a_backend_base_is_refused(self):
        """A direct subclass passing the flag is an authoring mistake."""
        with pytest.raises(TypeError, match="no parent wrapper to avoid"):

            class Direct(AbstractDataSource, ergonomics_resolved=True):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)

    def test_it_is_still_accepted_under_a_backend_parent(self):
        """The legitimate use — a backend subclassing a backend — still works."""

        class Parent(AbstractDataSource):
            REQUIRES_TIME_WINDOW = False

            def _check_input_dates(self, start, end, temporal_resolution, fmt):
                return TemporalExtent(
                    start_date=None,
                    end_date=None,
                    resolution="all",
                    dates=pd.DatetimeIndex([]),
                )

            def download(self, progress_bar: bool = True):
                return []

        class Child(Parent, ergonomics_resolved=True):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

        assert issubclass(Child, Parent)


class TestPositionalAggregateIsRefused:
    """The central gate must see `aggregate` however it was passed.

    `aggregate` is the second positional parameter on the backends that declare
    it, so reading it out of `**kwargs` alone let `download(False, config)`
    through. That mattered most for the backends whose own refusal was deleted
    once the gate became central: they would silently ignore the argument
    instead of raising.
    """

    def _backend(self, tmp_path):
        """Build a raster backend that declares `aggregate` positionally."""

        class _Positional(AbstractDataSource):
            REQUIRES_TIME_WINDOW = False
            OUTPUT_KIND = "raster"
            AGGREGATE_REFUSAL_REASON = "not wired here"

            def _check_input_dates(self, start, end, temporal_resolution, fmt):
                return TemporalExtent(
                    start_date=None,
                    end_date=None,
                    resolution="all",
                    dates=pd.DatetimeIndex([]),
                )

            def download(self, progress_bar: bool = True, aggregate=None):
                return ["ran"]

        return _Positional(
            start=None,
            end=None,
            variables=["x"],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
        )

    def test_a_positional_aggregate_is_refused(self, tmp_path):
        """`download(False, config)` raises, exactly as the keyword form does."""
        backend = self._backend(tmp_path)
        with pytest.raises(NotImplementedError, match="not wired here"):
            backend.download(False, object())

    def test_the_keyword_form_still_raises(self, tmp_path):
        """The original path is unchanged."""
        backend = self._backend(tmp_path)
        with pytest.raises(NotImplementedError, match="not wired here"):
            backend.download(aggregate=object())

    def test_a_positional_progress_bar_alone_is_fine(self, tmp_path):
        """Passing only `progress_bar` positionally must not trip the gate."""
        backend = self._backend(tmp_path)
        assert backend.download(False) == ["ran"]

    def test_an_explicit_none_aggregate_is_fine(self, tmp_path):
        """`aggregate=None` means "not asking for one"."""
        backend = self._backend(tmp_path)
        assert backend.download(True, None) == ["ran"]


class TestAggregateNoneStaysAccepted:
    """`download(aggregate=None)` must keep working on every backend.

    Before the refusal was centralised, ~40 backends each declared
    `aggregate=None` in their own `download` signature. Removing the parameter
    from those signatures made a perfectly valid call — "I am not asking for an
    aggregation" — raise `TypeError: got an unexpected keyword argument`, which
    breaks any caller that forwards the argument unconditionally. The wrapper
    absorbs the `None` case for backends that no longer name it.
    """

    def _backend(self, tmp_path):
        """Build a backend whose `download` does not declare `aggregate`."""

        class _NoAggregateParam(AbstractDataSource):
            REQUIRES_TIME_WINDOW = False
            OUTPUT_KIND = "vector"
            AGGREGATE_REFUSAL_REASON = "vector features have no gridded reduction"

            def _check_input_dates(self, start, end, temporal_resolution, fmt):
                return TemporalExtent(
                    start_date=None,
                    end_date=None,
                    resolution="all",
                    dates=pd.DatetimeIndex([]),
                )

            def download(self, progress_bar: bool = True):
                return ["ran"]

        return _NoAggregateParam(
            start=None,
            end=None,
            variables=["x"],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
        )

    def test_an_explicit_none_is_absorbed(self, tmp_path):
        """The call runs instead of raising `TypeError`."""
        backend = self._backend(tmp_path)
        assert backend.download(aggregate=None) == ["ran"]

    def test_a_real_config_is_still_refused(self, tmp_path):
        """Absorbing `None` must not weaken the refusal."""
        backend = self._backend(tmp_path)
        with pytest.raises(NotImplementedError, match="no gridded reduction"):
            backend.download(aggregate=object())

    def test_a_backend_that_declares_it_still_receives_it(self, tmp_path):
        """A real implementer must still get the argument it declared."""
        seen = {}

        class _Implementer(AbstractDataSource):
            REQUIRES_TIME_WINDOW = False
            OUTPUT_KIND = "raster"
            SUPPORTS_AGGREGATE = True

            def _check_input_dates(self, start, end, temporal_resolution, fmt):
                return TemporalExtent(
                    start_date=None,
                    end_date=None,
                    resolution="all",
                    dates=pd.DatetimeIndex([]),
                )

            def download(self, progress_bar: bool = True, aggregate=None):
                seen["aggregate"] = aggregate
                return ["ran"]

        backend = _Implementer(
            start=None,
            end=None,
            variables=["x"],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
        )
        config = object()
        backend.download(aggregate=config)
        assert seen["aggregate"] is config


class TestRunItemsSummary:
    """The warning line the partial-failure loop emits."""

    @staticmethod
    def _backend(tmp_path):
        """Build the minimal backend used to exercise `_run_items`."""
        return _build(_Minimal, tmp_path)

    def test_placeholders_are_not_counted_as_successes(self, tmp_path):
        """With `on_failure`, the summary counts real successes, not results.

        Every failure contributes a placeholder to `results`, so counting that
        list reports as many successes as there were items.
        """
        from loguru import logger as loguru_logger

        def flaky(n):
            if n == 2:
                raise RuntimeError("boom")
            return n

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda message: messages.append(str(message)))
        try:
            self._backend(tmp_path)._run_items(
                [1, 2, 3],
                flaky,
                label="thing",
                on_failure=lambda item, _exc: f"empty-{item}",
            )
        finally:
            loguru_logger.remove(sink_id)

        summary = [m for m in messages if "thing(s) failed" in m]
        assert summary, f"expected a partial-failure summary, got: {messages}"
        assert "1 of 3 thing(s) failed; 2 succeeded" in summary[0], (
            f"the summary should not count placeholders as successes: {summary[0]}"
        )
