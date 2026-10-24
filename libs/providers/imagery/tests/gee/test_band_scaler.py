"""Tests for `BandScaler`, `BandFactor` and the ee-side `MaskChain` (Task 6.12)."""

from __future__ import annotations

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee.catalog import Band, Dataset, Extent  # noqa: E402
from earthlens.gee.cloud_masks import (  # noqa: E402
    BandFactor,
    BandScaler,
    ImageMask,
    MaskChain,
    Sentinel2Qa60Mask,
    Sentinel2SclMask,
    SpeckleFilter,
    sentinel2_scl,
)
from earthlens.gee.configs import EngineConfig  # noqa: E402

_TIME_START = 1718236800000


class _Recorder:
    """A per-image op that records being called and returns its input."""

    def __init__(self, label, log):
        self.label = label
        self.log = log

    def __call__(self, image):
        self.log.append(self.label)
        return image


class _Term:
    """A band expression recording the arithmetic applied to it."""

    def __init__(self, expr):
        self.expr = expr

    def multiply(self, value):
        return _Term(("mul", self.expr, value))

    def add(self, value):
        return _Term(("add", self.expr, value))

    def rename(self, name):
        return _Term(("rename", self.expr, name))

    def neq(self, value):
        return _Term(("neq", self.expr, value))

    def And(self, other):  # noqa: N802 - Earth Engine name
        return _Term(("and", self.expr, other.expr))


class _FakeImage:
    """Recorder image tracking band writes and property propagation."""

    def __init__(self, properties=None, bands=("B4", "B3", "SCL")):
        self.properties = (
            dict(properties)
            if properties is not None
            else {"system:time_start": _TIME_START, "CLOUD_COVER": 7}
        )
        self.bands = tuple(bands)
        self.selects: list[str] = []
        self.added: list[_Term] = []
        self.copied_from = None
        self.copied_properties = None
        self.focal_kwargs = None
        self.overwrite = None
        self.mask = None

    def select(self, band):
        self.selects.append(band)
        return _Term(("band", band))

    def addBands(self, src, overwrite=False):  # noqa: N802 - Earth Engine name
        derived = _FakeImage(properties=None, bands=self.bands)
        # A rebuilt image carries no metadata, which is the whole reason
        # the scaler has to copy the properties back on.
        derived.properties = {}
        derived.added = [*self.added, src]
        derived.overwrite = overwrite
        derived.selects = self.selects
        return derived

    def focalMedian(self, **kwargs):  # noqa: N802 - Earth Engine name
        derived = _FakeImage(properties=None, bands=self.bands)
        derived.properties = {}
        derived.focal_kwargs = kwargs
        return derived

    def updateMask(self, mask):  # noqa: N802 - Earth Engine name
        derived = _FakeImage(properties=self.properties, bands=self.bands)
        derived.mask = mask
        return derived

    def propertyNames(self):  # noqa: N802 - Earth Engine name
        return list(self.properties)

    def copyProperties(self, source, properties=None):  # noqa: N802 - EE name
        derived = _FakeImage(properties=None, bands=self.bands)
        derived.added = self.added
        derived.selects = self.selects
        derived.focal_kwargs = self.focal_kwargs
        derived.overwrite = self.overwrite
        derived.copied_from = source
        derived.copied_properties = list(properties) if properties else None
        derived.properties = {
            key: value
            for key, value in source.properties.items()
            if properties is None or key in properties
        }
        return derived


@pytest.fixture
def warnings_buffer():
    """Collect loguru's output, which pytest's `caplog` never sees.

    loguru writes through its own handler set, bound to the original stderr at
    registration time, so a dedicated sink is the only way to read it back.
    """
    from loguru import logger

    buffer: list[str] = []
    sink_id = logger.add(buffer.append, level="WARNING")
    yield buffer
    logger.remove(sink_id)


@pytest.fixture
def ee_image_passthrough(monkeypatch):
    """Make `ee.Image(x)` hand back `x`, so the fakes survive the wrap."""
    from earthlens.gee import cloud_masks

    monkeypatch.setattr(cloud_masks.ee, "Image", lambda value: value)
    return cloud_masks


def _s2_row():
    """A Sentinel-2 catalog row declaring a scale on one band only."""
    return Dataset(
        id="COPERNICUS/S2_SR_HARMONIZED",
        title="S2 L2A",
        extent=Extent(start_date="2017-03-28"),
        bands={
            "B4": Band(id="B4", scale=0.0001),
            "B3": Band(id="B3", scale=0.0001),
            "SCL": Band(id="SCL"),
        },
    )


class TestBandFactor:
    """The per-band conversion is a validated value object."""

    def test_the_halves_default_to_the_identity(self):
        """A factor declaring only a scale leaves the offset at zero."""
        factor = BandFactor(band="B4", scale=0.0001)
        assert (factor.band, factor.scale, factor.offset) == ("B4", 0.0001, 0.0)

    def test_it_is_frozen(self):
        """A factor cannot drift after construction."""
        with pytest.raises(ValidationError):
            BandFactor(band="B4").scale = 2.0

    def test_an_empty_band_id_is_refused(self):
        """A factor naming no band is refused at construction."""
        with pytest.raises(ValidationError):
            BandFactor(band="")

    def test_a_typo_is_refused(self):
        """An unknown keyword raises instead of being ignored."""
        with pytest.raises(ValidationError):
            BandFactor(band="B4", scalee=0.0001)


class TestBandScalerResolution:
    """`for_dataset` reads the factors the catalog declares."""

    def test_only_bands_declaring_a_factor_are_included(self):
        """A band with neither scale nor offset is skipped."""
        scaler = BandScaler.for_dataset(_s2_row())
        assert [(f.band, f.scale) for f in scaler.factors] == [
            ("B4", 0.0001),
            ("B3", 0.0001),
        ]

    def test_the_band_list_narrows_the_factors(self):
        """Passing `bands` restricts the scaler to the requested ones."""
        scaler = BandScaler.for_dataset(_s2_row(), bands=["B3", "SCL"])
        assert [f.band for f in scaler.factors] == ["B3"]

    def test_an_unknown_band_is_skipped(self):
        """A band the row does not carry does not become a factor."""
        scaler = BandScaler.for_dataset(_s2_row(), bands=["B4", "B12"])
        assert [f.band for f in scaler.factors] == ["B4"]

    def test_a_missing_half_falls_back_to_its_identity(self):
        """A row declaring only an offset gets scale 1.0, and vice versa."""
        row = Dataset(
            id="X/Y",
            title="X",
            extent=Extent(start_date="2000-01-01"),
            bands={"a": Band(id="a", offset=-0.2), "b": Band(id="b", scale=2.0)},
        )
        resolved = {
            f.band: (f.scale, f.offset) for f in BandScaler.for_dataset(row).factors
        }
        assert resolved == {"a": (1.0, -0.2), "b": (2.0, 0.0)}

    def test_a_row_with_no_factors_warns_and_yields_an_identity(self, warnings_buffer):
        """Scaling an unscalable dataset says so rather than no-op'ing silently."""
        row = Dataset(
            id="USGS/SRTMGL1_003",
            title="SRTM v3",
            extent=Extent(start_date="2000-02-11"),
            bands={"elevation": Band(id="elevation")},
        )
        scaler = BandScaler.for_dataset(row)
        assert scaler.factors == ()
        assert "declares no band scale/offset" in "".join(warnings_buffer)

    def test_landsat_declares_both_halves(self):
        """The Landsat C2-L2 reflectance factors resolve as a scale and offset."""
        row = Dataset(
            id="LANDSAT/LC08/C02/T1_L2",
            title="LS8 C2 L2",
            extent=Extent(start_date="2013-03-18"),
            bands={"SR_B4": Band(id="SR_B4", scale=2.75e-05, offset=-0.2)},
        )
        factor = BandScaler.for_dataset(row).factors[0]
        assert (factor.scale, factor.offset) == (2.75e-05, -0.2)


class TestBandScalerCall:
    """The call applies the arithmetic server-side and keeps the metadata."""

    def test_it_is_an_image_mask(self):
        """The scaler rides the `cloud_mask=` hook as an `ImageMask`."""
        assert isinstance(BandScaler(), ImageMask)

    def test_each_band_is_scaled_then_offset_and_written_back(
        self, ee_image_passthrough
    ):
        """The graph is `select -> multiply -> add -> rename`, overwritten in place."""
        image = _FakeImage()
        out = BandScaler(factors=(BandFactor(band="B4", scale=0.0001, offset=-0.1),))(
            image
        )
        assert out.selects == ["B4"]
        assert [term.expr for term in out.added] == [
            ("rename", ("add", ("mul", ("band", "B4"), 0.0001), -0.1), "B4")
        ]
        assert out.overwrite is True

    def test_bands_declaring_no_factor_are_never_touched(self, ee_image_passthrough):
        """Only the named bands are selected, so the rest pass through."""
        image = _FakeImage()
        out = BandScaler.for_dataset(_s2_row())(image)
        assert out.selects == ["B4", "B3"]
        assert len(out.added) == 2

    def test_the_image_properties_survive_the_arithmetic(self, ee_image_passthrough):
        """`system:time_start` is still there after the bands are rebuilt.

        Load-bearing: `CompositeEngine.composite_ee` filters each bucket by date
        *after* the mask hook, so an op that dropped the timestamp would empty
        every bucket.
        """
        image = _FakeImage()
        out = BandScaler(factors=(BandFactor(band="B4", scale=0.0001),))(image)
        assert out.copied_from is image
        assert out.properties["system:time_start"] == _TIME_START
        assert out.properties["CLOUD_COVER"] == 7

    def test_the_system_properties_are_named_explicitly(self, ee_image_passthrough):
        """`copyProperties` is given the property list, since it skips `system:*`."""
        image = _FakeImage()
        out = BandScaler(factors=(BandFactor(band="B4", scale=0.0001),))(image)
        assert "system:time_start" in out.copied_properties

    def test_an_identity_scaler_still_preserves_the_properties(
        self, ee_image_passthrough
    ):
        """With no factors the image passes through, metadata included."""
        image = _FakeImage()
        out = BandScaler()(image)
        assert out.added == []
        assert out.properties["system:time_start"] == _TIME_START

    def test_it_forces_server_side_compute(self):
        """A scaler on the hook declines the EEDAI fast path, like any mask."""
        config = EngineConfig(cloud_mask=BandScaler())
        assert config.needs_server_side_compute is True

    def test_it_is_frozen(self):
        """The factors cannot drift after construction."""
        with pytest.raises(ValidationError):
            BandScaler().factors = ()


class TestSpeckleFilter:
    """The server-side focal median behind the Sentinel-1 recipe."""

    def test_the_defaults_are_a_fifty_metre_circular_median(self):
        """The default kernel is a 50 m circle applied once."""
        speckle = SpeckleFilter()
        assert (
            speckle.radius,
            speckle.units,
            speckle.kernel_type,
            speckle.iterations,
        ) == (50.0, "meters", "circle", 1)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"radius": 0},
            {"radius": -1},
            {"units": "degrees"},
            {"kernel_type": "hexagon"},
            {"iterations": 0},
        ],
    )
    def test_an_out_of_range_kernel_is_refused(self, kwargs):
        """A kernel Earth Engine would reject raises at construction."""
        with pytest.raises(ValidationError):
            SpeckleFilter(**kwargs)

    def test_it_calls_focal_median_with_the_configured_kernel(
        self, ee_image_passthrough
    ):
        """The config reaches `ee.Image.focalMedian` under its EE spellings."""
        out = SpeckleFilter(radius=3, units="pixels", iterations=2)(_FakeImage())
        assert out.focal_kwargs == {
            "radius": 3.0,
            "kernelType": "circle",
            "units": "pixels",
            "iterations": 2,
        }

    def test_the_image_properties_survive(self, ee_image_passthrough):
        """`focalMedian` rebuilds the image, so the properties are copied back."""
        image = _FakeImage()
        out = SpeckleFilter()(image)
        assert out.properties["system:time_start"] == _TIME_START


class TestMaskChain:
    """The ee-side composition the single `cloud_mask` slot needs."""

    def test_a_chain_is_an_image_mask(self):
        """A chain goes wherever one mask goes."""
        assert isinstance(MaskChain.of(Sentinel2SclMask()), ImageMask)

    def test_rshift_composes_two_ops(self):
        """`>>` yields a two-element chain in written order."""
        chain = Sentinel2SclMask() >> BandScaler()
        assert isinstance(chain, MaskChain)
        assert [type(op).__name__ for op in chain.masks] == [
            "Sentinel2SclMask",
            "BandScaler",
        ]

    def test_composition_flattens(self):
        """Grouping does not change the chain, so `name` stays readable."""
        a, b, c = Sentinel2SclMask(), Sentinel2Qa60Mask(), BandScaler()
        assert len(((a >> b) >> c).masks) == len((a >> (b >> c)).masks) == 3

    def test_the_name_lists_the_elements_in_order(self):
        """The chain name reads left to right, functions included."""
        chain = MaskChain.of(sentinel2_scl, BandScaler())
        assert chain.name == "sentinel2_scl >> BandScaler"

    def test_composing_with_a_non_mask_raises(self):
        """`>>` refuses a right-hand side that is not an `ImageMask`."""
        with pytest.raises(TypeError, match="MaskChain.of"):
            Sentinel2SclMask() >> sentinel2_scl

    def test_an_empty_chain_is_refused(self):
        """A chain of nothing is a construction error, not a no-op."""
        with pytest.raises(ValidationError):
            MaskChain(masks=())

    def test_the_ops_run_left_to_right(self, ee_image_passthrough):
        """Each element receives the previous one's output, in order."""
        order: list[str] = []
        chain = MaskChain.of(
            _Recorder("first", order),
            _Recorder("second", order),
        )
        chain(_FakeImage())
        assert order == ["first", "second"]

    def test_the_mask_runs_before_the_scaler(self, ee_image_passthrough):
        """Mask-then-scale: the quality band is read before any arithmetic.

        The scaler's `select` list is empty on the image the mask handed over
        only if the mask ran first, since `updateMask` returns a fresh image.
        """
        image = _FakeImage()
        out = (Sentinel2SclMask() >> BandScaler.for_dataset(_s2_row()))(image)
        assert image.selects == ["SCL"]
        assert out.selects == ["B4", "B3"]

    def test_a_chain_preserves_the_properties_end_to_end(self, ee_image_passthrough):
        """`system:time_start` survives a mask followed by a scaler."""
        out = (Sentinel2SclMask() >> BandScaler.for_dataset(_s2_row()))(_FakeImage())
        assert out.properties["system:time_start"] == _TIME_START

    def test_a_chain_forces_server_side_compute(self):
        """A chain on the hook declines the EEDAI path, like a single mask."""
        chain = Sentinel2SclMask() >> BandScaler()
        assert EngineConfig(cloud_mask=chain).needs_server_side_compute is True

    def test_it_is_frozen(self):
        """The chain cannot be rewritten after construction."""
        chain = MaskChain.of(BandScaler())
        with pytest.raises(ValidationError):
            chain.masks = ()
