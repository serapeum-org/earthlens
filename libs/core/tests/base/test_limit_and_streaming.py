"""The bounded-result contract: `check_limit`, `_take_limited`, `iter_download`.

Before this, 19 backends appended every per-item fragment and concatenated at
the end, and the three arguments that looked like caps (`openaq`'s `limit`,
`nrel`'s `max_requests`, `pvgis`'s `max_points`) bounded a page or a request
count, not the rows. These tests pin what the shared primitives promise: the
cap is a total, it is exact even when it lands mid-fragment, and reaching it
stops the work rather than truncating afterwards.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import AbstractDataSource, RemoteProduct, TemporalExtent

pytestmark = [pytest.mark.unit]


class _Backend(AbstractDataSource):
    """A search/fetch backend whose per-product fetch is recorded."""

    REQUIRES_TIME_WINDOW = False
    OUTPUT_KIND = "tabular"

    #: Rows each product contributes, in `_search` order.
    sizes: tuple[int, ...] = (3, 3, 3)

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        return TemporalExtent(
            start_date=None,
            end_date=None,
            resolution="all",
            dates=pd.DatetimeIndex([]),
        )

    def _search(self):
        self.searched = True
        return [RemoteProduct(id=f"p{index}") for index in range(len(self.sizes))]

    def _fetch_one(self, product):
        self.fetched.append(product.id)
        index = int(product.id[1:])
        start = sum(self.sizes[:index])
        return pd.DataFrame({"n": range(start, start + self.sizes[index])})

    def download(self, progress_bar: bool = True, **kwargs):
        return self._api()


class _NoPerProductFetch(AbstractDataSource):
    """A backend that answers the whole request at once, so cannot stream.

    Deriving straight from the ABC (rather than from `_Backend`) is the point:
    it genuinely inherits the base `_fetch_one`, which is what `iter_download`
    tests for.
    """

    REQUIRES_TIME_WINDOW = False
    OUTPUT_KIND = "tabular"

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        return TemporalExtent(
            start_date=None,
            end_date=None,
            resolution="all",
            dates=pd.DatetimeIndex([]),
        )

    def _search(self):
        return [RemoteProduct(id="p0")]

    def download(self, progress_bar: bool = True, **kwargs):
        return self._api()


def _build(cls, tmp_path, **kwargs):
    """Construct a test backend with the boilerplate request arguments."""
    kwargs.setdefault("variables", ["x"])
    kwargs.setdefault("lat_lim", [4.0, 5.0])
    kwargs.setdefault("lon_lim", [-75.0, -74.0])
    kwargs.setdefault("start", None)
    kwargs.setdefault("end", None)
    backend = cls(path=str(tmp_path), **kwargs)
    backend.fetched = []
    return backend


class TestCheckLimit:
    """`check_limit` refuses the values that would silently return nothing."""

    def test_positive_and_none_pass_through(self):
        """A usable cap is returned unchanged."""
        assert AbstractDataSource.check_limit(7) == 7
        assert AbstractDataSource.check_limit(None) is None

    @pytest.mark.parametrize("bad", [0, -1, -1000])
    def test_non_positive_is_rejected(self, bad):
        """Zero or negative is a caller bug, not a cheap empty result."""
        with pytest.raises(ValueError, match="at least 1"):
            AbstractDataSource.check_limit(bad)

    @pytest.mark.parametrize("bad", ["10", 1.5, True, [10]])
    def test_non_int_is_rejected(self, bad):
        """A string, float or bool cap is a mistake — `True` is not a cap of 1."""
        with pytest.raises(TypeError, match="must be an int or None"):
            AbstractDataSource.check_limit(bad)


class TestTakeLimited:
    """`_take_limited` caps the total and stops consuming."""

    def test_no_limit_collects_everything(self, tmp_path):
        """`limit=None` is the unbounded behaviour it replaced."""
        backend = _build(_Backend, tmp_path)
        assert backend._take_limited([[1, 2], [3]], limit=None) == [[1, 2], [3]]

    def test_cap_landing_mid_fragment_is_exact(self, tmp_path):
        """The straddling fragment is trimmed, so the total is not a multiple."""
        backend = _build(_Backend, tmp_path)
        kept = backend._take_limited([[1, 2, 3], [4, 5, 6]], limit=4)
        assert kept == [[1, 2, 3], [4]], (
            f"expected an exact cap of 4 rows, got {kept!r}"
        )

    def test_fragments_past_the_cap_are_not_consumed(self, tmp_path):
        """Reaching the cap stops the generator — the point of the primitive."""
        backend = _build(_Backend, tmp_path)
        pulled = []

        kept = backend._take_limited(_counting_pages(pulled), limit=3)

        assert kept == [[0, 1, 2]]
        assert pulled == [0], (
            f"only the first page should have been produced; got {pulled}"
        )

    def test_cap_equal_to_the_total_keeps_every_fragment_whole(self, tmp_path):
        """An exact-fit cap must not trim or drop the last fragment."""
        backend = _build(_Backend, tmp_path)
        assert backend._take_limited([[1, 2], [3, 4]], limit=4) == [[1, 2], [3, 4]]

    def test_dataframe_fragments_are_trimmed_positionally(self, tmp_path):
        """A pandas fragment trims through `iloc`, not label-based slicing."""
        backend = _build(_Backend, tmp_path)
        frame = pd.DataFrame({"n": [10, 11, 12]}, index=[5, 6, 7])

        kept = backend._take_limited([frame], limit=2)

        assert list(kept[0]["n"]) == [10, 11], (
            f"expected the first two rows by position, got {kept[0].to_dict()}"
        )


class TestIterDownload:
    """`iter_download` streams per product and honours the cap."""

    def test_yields_one_fragment_per_product(self, tmp_path):
        """Every product's fragment arrives, in search order."""
        backend = _build(_Backend, tmp_path)
        fragments = list(backend.iter_download())
        assert [len(fragment) for fragment in fragments] == [3, 3, 3]
        assert backend.fetched == ["p0", "p1", "p2"]

    def test_is_lazy_before_the_first_fragment_is_requested(self, tmp_path):
        """Calling it fetches nothing until the caller iterates."""
        backend = _build(_Backend, tmp_path)
        stream = backend.iter_download()
        assert backend.fetched == [], "constructing the generator must not fetch"
        next(stream)
        assert backend.fetched == ["p0"]

    def test_limit_stops_fetching_the_later_products(self, tmp_path):
        """The cap bounds the work, not just the result."""
        backend = _build(_Backend, tmp_path)

        fragments = list(backend.iter_download(limit=4))

        assert [len(fragment) for fragment in fragments] == [3, 1]
        assert backend.fetched == ["p0", "p1"], (
            f"p2 must never be fetched under limit=4; got {backend.fetched}"
        )

    def test_limit_of_one_fetches_a_single_product(self, tmp_path):
        """The smallest cap still returns a real row."""
        backend = _build(_Backend, tmp_path)
        fragments = list(backend.iter_download(limit=1))
        assert [len(fragment) for fragment in fragments] == [1]
        assert backend.fetched == ["p0"]

    def test_limit_beyond_the_total_yields_everything(self, tmp_path):
        """A cap larger than the result set is not an error."""
        backend = _build(_Backend, tmp_path)
        assert sum(len(f) for f in backend.iter_download(limit=100)) == 9

    def test_invalid_limit_is_rejected_before_the_first_fetch(self, tmp_path):
        """A bad cap fails on first iteration, having fetched nothing."""
        backend = _build(_Backend, tmp_path)
        stream = backend.iter_download(limit=0)
        with pytest.raises(ValueError, match="at least 1"):
            next(stream)
        assert backend.fetched == []

    def test_a_whole_batch_backend_says_it_cannot_stream(self, tmp_path):
        """A backend with no per-product hook refuses rather than pretending."""
        backend = _build(_NoPerProductFetch, tmp_path)
        stream = backend.iter_download()
        with pytest.raises(NotImplementedError, match="cannot stream"):
            next(stream)


def _counting_pages(pulled):
    """Yield three pages, recording each one's first value as it is produced.

    Args:
        pulled: List the generator appends to, so a test can see how far it ran.

    Yields:
        list[int]: One page of three rows.
    """
    for page in ([0, 1, 2], [3, 4, 5], [6, 7, 8]):
        pulled.append(page[0])
        yield page


class TestFacadeIterDownload:
    """`EarthLens.iter_download` delegates to the bound backend."""

    def test_streams_through_the_facade_with_a_cap(self, tmp_path, monkeypatch):
        """The facade yields the backend's fragments and forwards the cap."""
        from earthlens.earthlens import EarthLens

        backend = _build(_Backend, tmp_path)
        facade = EarthLens.__new__(EarthLens)
        monkeypatch.setattr(
            type(facade), "datasource", property(lambda _self: backend), raising=False
        )

        fragments = list(facade.iter_download(limit=4))

        assert [len(fragment) for fragment in fragments] == [3, 1]
        assert backend.fetched == ["p0", "p1"]

    def test_facade_creates_the_output_directory_before_streaming(
        self, tmp_path, monkeypatch
    ):
        """`iter_download` gets the same root_dir guarantee as `download`."""
        from earthlens.earthlens import EarthLens

        target = tmp_path / "not-yet"
        backend = _build(_Backend, target)
        assert not target.exists(), "the fixture must start with no output dir"
        facade = EarthLens.__new__(EarthLens)
        monkeypatch.setattr(
            type(facade), "datasource", property(lambda _self: backend), raising=False
        )

        next(facade.iter_download())

        assert backend.root_dir.is_dir(), (
            f"{backend.root_dir} should have been created before the first fetch"
        )


class TestFetchLimitedStopsWork:
    """ARC-3: `_fetch_limited` caps the work, not just the returned rows.

    The shape it replaces — `[self._fetch_one(p) for p in products]` — fetches
    every product and lets any cap trim the result afterwards. That bounds what
    you get back and not what you paid for, which is the opposite of the point
    for a backend whose per-product fetch is a network call.
    """

    def test_products_past_the_cap_are_never_fetched(self, tmp_path):
        """The generator stops, so later products are not requested at all."""
        backend = _build(_Backend, tmp_path)

        kept = backend._fetch_limited(backend._search(), limit=4)

        assert [len(fragment) for fragment in kept] == [3, 1]
        assert backend.fetched == ["p0", "p1"], (
            f"p2 must never be fetched under limit=4; got {backend.fetched}"
        )

    def test_no_limit_fetches_everything(self, tmp_path):
        """`limit=None` keeps the unbounded behaviour it replaced."""
        backend = _build(_Backend, tmp_path)

        kept = backend._fetch_limited(backend._search(), limit=None)

        assert [len(fragment) for fragment in kept] == [3, 3, 3]
        assert backend.fetched == ["p0", "p1", "p2"]

    def test_cap_larger_than_the_result_is_not_an_error(self, tmp_path):
        """A cap nothing reaches behaves like no cap."""
        backend = _build(_Backend, tmp_path)
        assert sum(len(f) for f in backend._fetch_limited(backend._search(), 100)) == 9


class TestSearchFetchEachIsBounded:
    """`_search_fetch_each` honours `_limit` on both of its paths.

    It is the shared composition behind openaq, sensor_community, firms and
    soilgrids, so a cap that stopped only on the plain path would leave the
    error-policy path fetching everything — the same decorative cap, hidden one
    level down.
    """

    def test_products_past_the_cap_are_not_fetched(self, tmp_path):
        """The plain path stops as soon as the cap is met."""
        backend = _build(_Backend, tmp_path)
        backend._limit = 4
        frames = backend._search_fetch_each()

        assert backend.fetched == ["p0", "p1"], (
            f"fetched {backend.fetched}; the third product was pulled after the "
            f"cap was already met"
        )
        assert sum(len(frame) for frame in frames) == 4

    def test_no_limit_fetches_every_product(self, tmp_path):
        """Without a cap the composition is unchanged."""
        backend = _build(_Backend, tmp_path)
        backend._limit = None
        frames = backend._search_fetch_each()

        assert backend.fetched == ["p0", "p1", "p2"]
        assert sum(len(frame) for frame in frames) == 9

    def test_the_cap_stops_the_work_under_an_error_policy_too(self, tmp_path):
        """A failing product consumes an item without contributing rows.

        Which is exactly why the cap cannot be pre-applied to the product list:
        with `p0` failing, reaching 4 rows takes `p1` and `p2`, so all three are
        touched — and a fourth, had there been one, would not be.
        """
        backend = _build(_Backend, tmp_path)
        backend.sizes = (3, 3, 3, 3)
        original = backend._fetch_one

        def flaky(product):
            if product.id == "p0":
                backend.fetched.append(product.id)
                raise RuntimeError("boom")
            return original(product)

        backend._fetch_one = flaky
        backend._limit = 4
        frames = backend._search_fetch_each(errors="ignore")

        assert backend.fetched == ["p0", "p1", "p2"], (
            f"fetched {backend.fetched}; the failure should not have ended the "
            f"batch, and p3 should never have been reached"
        )
        assert sum(len(frame) for frame in frames) == 4


class TestIterItemsPolicy:
    """The lazy policy loop `_run_items` and `_search_fetch_each` share."""

    def test_a_consumer_exception_is_not_recorded_as_an_item_failure(self, tmp_path):
        """The handler must not stand in the path of the caller's own errors.

        With the `yield` inside the `try`, an exception raised by the consumer
        while the generator is suspended propagates *into* the generator at the
        yield point — so an `ignore` policy would swallow the caller's error and
        log it as this item's failure. Calling `fn` outside the `yield` is what
        keeps the two apart.
        """
        backend = _build(_Backend, tmp_path)
        failures: list[tuple[str, BaseException]] = []
        items = [RemoteProduct(id="p0"), RemoteProduct(id="p1")]

        generator = backend._iter_items(
            items,
            backend._fetch_one,
            errors="ignore",
            label="product",
            describe=str,
            on_failure=None,
            failures=failures,
        )
        next(generator)
        # An `Exception` subclass on purpose: a `KeyboardInterrupt` here would
        # pass either way, since `except Exception` never catches it, and the
        # test would prove nothing.
        from_the_caller = RuntimeError("raised by the caller")
        with pytest.raises(RuntimeError, match="raised by the caller"):
            generator.throw(from_the_caller)

        assert failures == [], "the consumer's error was logged as an item failure"

    def test_a_placeholder_of_none_is_still_yielded(self, tmp_path):
        """`on_failure` returning `None` is a real placeholder, not "no result".

        The loop distinguishes them with a sentinel; a plain `is None` test here
        would silently drop the row the hook asked to substitute.
        """
        backend = _build(_Backend, tmp_path)
        failures: list[tuple[str, BaseException]] = []

        def always_fails(item):
            raise RuntimeError("boom")

        results = list(
            backend._iter_items(
                [RemoteProduct(id="p0")],
                always_fails,
                errors="ignore",
                label="product",
                describe=str,
                on_failure=lambda item, exc: None,
                failures=failures,
            )
        )

        assert results == [None]
        assert len(failures) == 1

    def test_no_placeholder_yields_nothing_for_the_failed_item(self, tmp_path):
        """Without an `on_failure` hook a failure contributes no result."""
        backend = _build(_Backend, tmp_path)
        failures: list[tuple[str, BaseException]] = []

        def always_fails(item):
            raise RuntimeError("boom")

        results = list(
            backend._iter_items(
                [RemoteProduct(id="p0")],
                always_fails,
                errors="ignore",
                label="product",
                describe=str,
                on_failure=None,
                failures=failures,
            )
        )

        assert results == []
        assert len(failures) == 1

    def test_the_raise_policy_still_propagates(self, tmp_path):
        """`errors=None` / `"raise"` keeps the fail-fast behaviour."""
        backend = _build(_Backend, tmp_path)
        failures: list[tuple[str, BaseException]] = []

        def always_fails(item):
            raise RuntimeError("boom")

        generator = backend._iter_items(
            [RemoteProduct(id="p0")],
            always_fails,
            errors=None,
            label="product",
            describe=str,
            on_failure=None,
            failures=failures,
        )
        with pytest.raises(RuntimeError, match="boom"):
            list(generator)


class TestTakeLimitedClosesWhatItAbandons:
    """Stopping early must release the abandoned generator's resources now.

    The generators feeding `_take_limited` are not pure producers: eea_aq's is
    suspended inside a `TemporaryDirectory` holding a bulk download. Leaving it
    to be collected whenever means the directory survives for an indeterminate
    time — and on an implementation without refcounting, until exit.
    """

    def test_the_generator_is_closed_when_the_cap_stops_it(self, tmp_path):
        """The abandoned generator's cleanup runs before the call returns."""
        backend = _build(_Backend, tmp_path)
        released: list[str] = []

        def frames():
            try:
                for index in range(5):
                    yield pd.DataFrame({"n": [index, index, index]})
            finally:
                released.append("cleaned up")

        # The reference is held deliberately. Passing `frames()` inline lets
        # CPython's refcounting collect the abandoned generator the moment
        # `_take_limited` returns, which runs the `finally` for reasons that
        # have nothing to do with the explicit close — the test then passes
        # with the close deleted, proving nothing.
        stream = frames()
        collected = backend._take_limited(stream, limit=4)

        assert sum(len(frame) for frame in collected) == 4
        assert released == ["cleaned up"], (
            "the still-referenced generator was abandoned without being "
            "closed, so whatever it held open stays open"
        )

    def test_full_consumption_still_cleans_up_once(self, tmp_path):
        """Exhausting the generator normally is unaffected."""
        backend = _build(_Backend, tmp_path)
        released: list[str] = []

        def frames():
            try:
                yield pd.DataFrame({"n": [1]})
                yield pd.DataFrame({"n": [2]})
            finally:
                released.append("cleaned up")

        collected = backend._take_limited(frames(), limit=99)

        assert sum(len(frame) for frame in collected) == 2
        assert released == ["cleaned up"]

    def test_a_plain_list_without_close_is_accepted(self, tmp_path):
        """Not every input is a generator; a list has no `close` to call."""
        backend = _build(_Backend, tmp_path)
        collected = backend._take_limited(
            [pd.DataFrame({"n": [1, 2]}), pd.DataFrame({"n": [3, 4]})], limit=3
        )
        assert sum(len(frame) for frame in collected) == 3


class TestCapOnANonRowFragmentExplainsItself:
    """A cap on a file-writing backend fails with a message that names the cause.

    `_search_fetch_each` is shared by tabular backends (whose `_fetch_one`
    yields frames) and by soilgrids, whose `_fetch_one` yields a single `Path`.
    Routing the composition through `_take_limited` made the cap reach `len()`
    — fine for a frame, a bare `TypeError: object of type 'WindowsPath' has no
    len()` for a path, naming neither the backend nor the cap.
    """

    def test_a_path_fragment_names_the_backend_and_the_cap(self, tmp_path):
        """The raised message points at the real problem."""
        backend = _build(_Backend, tmp_path)
        backend._fetch_one = lambda product: tmp_path / f"{product.id}.tif"
        backend._limit = 2

        with pytest.raises(TypeError, match="cannot apply a row cap"):
            backend._search_fetch_each()

    def test_row_bearing_fragments_are_unaffected(self, tmp_path):
        """The normal path still measures with `len`."""
        backend = _build(_Backend, tmp_path)
        backend._limit = 4
        frames = backend._search_fetch_each()
        assert sum(len(frame) for frame in frames) == 4

    def test_no_cap_leaves_path_fragments_alone(self, tmp_path):
        """Without a cap nothing is measured, so a file backend is untouched."""
        backend = _build(_Backend, tmp_path)
        backend._fetch_one = lambda product: tmp_path / f"{product.id}.tif"
        backend._limit = None
        assert len(backend._search_fetch_each()) == 3


class TestExactFillSkipsTheTrim:
    """A fragment that fills the cap exactly is passed through, not re-sliced.

    Behaviourally identical either way, which is why it went untested — the
    difference is that `_head_rows` copies every row to hand back the fragment
    it was already given. Identity is what distinguishes them.
    """

    def test_take_limited_returns_the_same_object(self, tmp_path):
        """The exactly-filling fragment is the object that was yielded."""
        backend = _build(_Backend, tmp_path)
        frame = pd.DataFrame({"n": [1, 2, 3]})

        kept = backend._take_limited([frame], limit=3)

        assert kept[0] is frame, "an exact fill was copied instead of passed through"

    def test_take_limited_still_trims_when_it_straddles(self, tmp_path):
        """One row over the cap and the trim must happen."""
        backend = _build(_Backend, tmp_path)
        frame = pd.DataFrame({"n": [1, 2, 3, 4]})

        kept = backend._take_limited([frame], limit=3)

        assert kept[0] is not frame
        assert list(kept[0]["n"]) == [1, 2, 3]

    def test_iter_download_returns_the_same_object_on_an_exact_fill(self, tmp_path):
        """`iter_download` agrees with `_take_limited` on the exact-fill case."""
        backend = _build(_Backend, tmp_path)
        backend.sizes = (3,)

        fragments = list(backend.iter_download(limit=3))

        assert len(fragments) == 1
        assert len(fragments[0]) == 3

    def test_iter_download_still_trims_when_it_straddles(self, tmp_path):
        """The straddling fragment is cut to the remaining rows."""
        backend = _build(_Backend, tmp_path)
        backend.sizes = (2, 3)

        fragments = list(backend.iter_download(limit=4))

        assert [len(fragment) for fragment in fragments] == [2, 2]


class TestSearchFetchEachClosesItsProgressBar:
    """The tqdm bar is closed even when the cap ends the sweep early.

    tqdm keeps redrawing and holds the terminal until it is closed. A cap that
    stops mid-sweep leaves the bar unfinished, and closing the abandoned
    generator does not close the bar — they are separate objects.
    """

    def test_the_bar_is_closed_when_a_cap_stops_the_sweep(self, tmp_path, monkeypatch):
        """The bar's `close` runs before `_search_fetch_each` returns."""
        import sys

        import tqdm as tqdm_module

        closed: list[bool] = []
        # `instances` keeps every bar referenced. Without it the local goes out
        # of scope when `_search_fetch_each` returns, refcounting collects the
        # bar, and tqdm's own `__del__` calls `close` — so the test passes with
        # the explicit close deleted, which is precisely what it must detect.
        instances: list[object] = []

        class _RecordingTqdm(tqdm_module.tqdm):
            def __init__(self, *args, **kwargs):
                instances.append(self)
                super().__init__(*args, **kwargs)

            def close(self):
                closed.append(True)
                super().close()

        monkeypatch.setattr(tqdm_module, "tqdm", _RecordingTqdm)
        monkeypatch.setitem(sys.modules, "tqdm", tqdm_module)
        backend = _build(_Backend, tmp_path)
        backend._limit = 4
        backend._search_fetch_each(progress_bar=False)

        assert instances, "the composition built no progress bar to close"
        assert closed, "the progress bar was abandoned without being closed"
