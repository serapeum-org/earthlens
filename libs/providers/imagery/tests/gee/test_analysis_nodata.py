"""The no-data contract every operation owes, tested per operation.

Two halves, and the branch originally got both wrong in four modules:

1. **Input.** A no-data sentinel is a *number* - `-9999` is finite - so an
   operation that filters with `np.isfinite` treats the void as data. One void
   six cells away was enough to change a texture measure from 6.5 to 0.
2. **Output.** `Dataset.dataset_like` copies the *source's* `no_data_value`
   onto a result that holds new values in a new dtype, so an unmatched cell
   written as NaN was declared `-9999` and read back as data, and an integer
   result declared a wrapped sentinel of 241.

These tests state the contract once per operation rather than per module, so a
new operation that forgets it fails here.
"""

from __future__ import annotations

import numpy as np
import pytest
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis import (
    Aspect,
    BandExpression,
    Classify,
    Cluster,
    FocalMean,
    FocalMedian,
    FocalStd,
    Hillshade,
    NormalizedDifference,
    Reclassify,
    Slope,
    Texture,
    TrainingExtractor,
)

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)
SENTINEL = -9999.0


def _raster(values: np.ndarray, no_data: float = SENTINEL) -> Dataset:
    """Build a raster declaring `no_data`, which is a finite number by design."""
    return Dataset.from_array(
        arr=values.astype("float32"), geo_ref=GEO, no_data_value=no_data
    )


def _voided_ramp() -> Dataset:
    """A 7x7 ramp whose top-left cell is the declared sentinel."""
    values = np.tile(np.arange(7, dtype="float32"), (7, 1))
    values[0, 0] = SENTINEL
    return _raster(values)


def _clean_ramp() -> Dataset:
    """The same ramp with no void, as the control."""
    return _raster(np.tile(np.arange(7, dtype="float32"), (7, 1)))


def _two_band_with_void() -> Dataset:
    """A two-band raster whose second cell is the sentinel in both bands."""
    stack = np.stack(
        [
            np.array([[0.6, SENTINEL]], dtype="float32"),
            np.array([[0.2, SENTINEL]], dtype="float32"),
        ]
    )
    return Dataset.from_array(arr=stack, geo_ref=GEO, no_data_value=SENTINEL)


def _mask_of(ds: Dataset) -> np.ndarray:
    """The mask a reader gets from a masked read, which is what honours no-data."""
    return np.ma.getmaskarray(ds.read_array(masked=True))


class TestTheOutputDeclaresItsOwnNoData:
    """A result holds new values, so it must declare its own sentinel."""

    def test_reclassify_declares_the_unmatched_cells(self):
        """An unmatched cell has to be visible to a masked read."""
        source = _raster(np.array([[0.0, 5.0, 15.0, 40.0]], dtype="float32"))
        result = Reclassify(bins=((30.0, float("inf"), 9),)).apply(source)
        assert _mask_of(result).ravel().tolist() == [True, True, True, False]

    def test_reclassify_does_not_inherit_the_source_sentinel(self):
        """-9999 is the input's sentinel; it means nothing in a class raster."""
        source = _raster(np.array([[0.0, 40.0]], dtype="float32"))
        result = Reclassify(bins=((30.0, float("inf"), 9),)).apply(source)
        assert SENTINEL not in tuple(result.no_data_value)

    def test_an_integer_dtype_uses_a_representable_sentinel(self):
        """NaN cast to uint8 is 0, which collides with a legitimate class 0."""
        source = _raster(np.array([[0.0, 40.0]], dtype="float32"))
        result = Reclassify(
            bins=((30.0, float("inf"), 9),), dtype="uint8", no_data=255
        ).apply(source)
        values = np.asarray(result.read_array())
        assert values.ravel().tolist() == [255, 9]
        assert _mask_of(result).ravel().tolist() == [True, False]

    def test_an_unrepresentable_sentinel_is_refused_at_construction(self):
        """NaN cannot be stored in an integer band, so asking for it must fail."""
        # Deliberately broad: the refusal comes from numpy or pyramids depending on the dtype.
        with pytest.raises(Exception, match="(?i)no_data|integer"):
            Reclassify(bins=((0.0, 1.0, 1),), dtype="uint8", no_data=float("nan"))

    def test_the_index_declares_its_undefined_cells(self):
        """A zero denominator and a void are both undefined, and must read so."""
        source = _two_band_with_void()
        result = NormalizedDifference().apply(source)
        assert _mask_of(result).ravel().tolist() == [False, True]

    def test_cluster_declares_its_no_data_label(self):
        """The label a cluster writes for a void must be the declared sentinel."""
        values = np.array([[1.0, 1.0, 50.0, SENTINEL]] * 4, dtype="float32")
        result = Cluster(n_clusters=2).apply(_raster(values))
        assert _mask_of(result)[0].tolist() == [False, False, False, True]

    def test_classify_declares_its_no_data_label(self):
        """Same contract for the supervised path."""
        values = np.array([[1.0, 1.0, 50.0, 50.0]] * 4, dtype="float32")
        source = _raster(values)
        marks = np.full((4, 4), -1.0, dtype="float32")
        marks[0, 0] = 7.0
        marks[0, 3] = 9.0
        training = TrainingExtractor().extract(source, _raster(marks, no_data=-1.0))
        holed = values.copy()
        holed[3, 3] = SENTINEL
        result = Classify(training=training).apply(_raster(holed))
        assert bool(_mask_of(result)[3, 3])


class TestTheInputHonoursTheDeclaredNoData:
    """A finite sentinel must not be read as data."""

    def test_a_void_does_not_change_texture_elsewhere(self):
        """One void must not flatten the quantisation range of the whole band."""
        clean = float(
            np.asarray(Texture(measure="contrast").apply(_clean_ramp()).read_array())[
                3, 3
            ]
        )
        voided = float(
            np.asarray(Texture(measure="contrast").apply(_voided_ramp()).read_array())[
                3, 3
            ]
        )
        assert voided == pytest.approx(clean)

    def test_an_index_over_a_void_is_undefined_not_zero(self):
        """`(-9999 - -9999) / (-9999 + -9999)` is -0.0, a plausible index."""
        values = np.asarray(
            NormalizedDifference().apply(_two_band_with_void()).read_array()
        )
        assert np.isnan(values[0, 1])

    def test_a_formula_over_a_void_is_undefined(self):
        """A band expression reads the same bands, so it owes the same contract."""
        result = BandExpression(formula="(b0 - b1) / (b0 + b1)").apply(
            _two_band_with_void()
        )
        assert np.isnan(np.asarray(result.read_array())[0, 1])

    def test_reclassify_leaves_a_void_unmatched_even_with_an_open_bin(self):
        """An open lower bound must not sweep the sentinel into the lowest class."""
        source = _raster(np.array([[5.0, SENTINEL, 40.0]], dtype="float32"))
        result = Reclassify(
            bins=((-float("inf"), 10.0, 1), (10.0, float("inf"), 2))
        ).apply(source)
        assert _mask_of(result).ravel().tolist() == [False, True, False]

    def test_a_formula_sees_voids_as_nan_not_as_the_sentinel(self):
        """So `b0 > 0` cannot be true for a void just because -9999 < 0."""
        source = _raster(np.array([[5.0, SENTINEL]], dtype="float32"))
        result = BandExpression(formula="b0 * 2").apply(source)
        assert np.isnan(np.asarray(result.read_array())[0, 1])


class TestTheArraySeam:
    """The two remaining branches in the shared read/write helpers."""

    def test_a_band_outside_the_raster_is_named(self):
        """An out-of-range band should say so, not index past the end."""
        from earthlens.gee.analysis._arrays import read_masked

        source = Dataset.from_array(
            arr=np.zeros((2, 2), dtype="float32"), geo_ref=GEO, no_data_value=SENTINEL
        )
        with pytest.raises(IndexError, match="band 3 was requested"):
            read_masked(source, 3)

    def test_a_raster_cannot_carry_fewer_names_than_bands(self):
        """Which is why the namespace's length guard is defensive, not untested.

        pyramids names every band - `band_names` on a two-band raster is
        `['Band_1', 'Band_2']` - and assigning a shorter list raises, so a
        namespace built from a short list is unreachable from here.
        """
        stack = np.stack(
            [
                np.full((2, 2), 0.6, dtype="float32"),
                np.full((2, 2), 0.2, dtype="float32"),
            ]
        )
        source = Dataset.from_array(arr=stack, geo_ref=GEO, no_data_value=np.nan)
        assert source.band_names == ['Band_1', 'Band_2']
        with pytest.raises(IndexError):
            source.band_names = ['first']


class TestTheTerrainContractAtVoids:
    """The no-data contract for the terrain family, the gap round 2 found.

    `Slope`/`Aspect`/`Hillshade` were absent from this suite, and that is why
    `Slope(units="percent")` shipped leaking the sentinel: it did `tan` on the
    raw array, where a void still held the finite `-9999`, producing a
    plausible `82.44` that a masked read took as real.
    """

    @staticmethod
    def _ramp_with_a_void() -> Dataset:
        """A metric ramp with one void cell in the interior."""
        metric = GeoReference(top_left_corner=(0.0, 0.0), cell_size=1.0, epsg=3857)
        values = np.tile(np.arange(6, dtype="float32"), (6, 1))
        values[3, 3] = SENTINEL
        return Dataset.from_array(arr=values, geo_ref=metric, no_data_value=SENTINEL)

    @pytest.mark.parametrize(
        "operation",
        [
            Slope(),
            Slope(units="radians"),
            Slope(units="percent"),
            Aspect(),
            Hillshade(),
        ],
        ids=["degrees", "radians", "percent", "aspect", "hillshade"],
    )
    def test_a_terrain_derivative_masks_the_void(self, operation):
        """Every terrain path must leave a void masked, percent included."""
        result = operation.apply(self._ramp_with_a_void())
        masked = result.read_array(masked=True)
        assert bool(np.ma.getmaskarray(masked)[3, 3]), (
            f"{operation.name} reports a real value at a void cell"
        )

    def test_percent_does_not_report_the_sentinel_as_a_slope(self):
        """The specific corruption: 82.44 where the DEM had no data."""
        result = Slope(units="percent").apply(self._ramp_with_a_void())
        void = float(np.asarray(result.read_array())[3, 3])
        assert void != pytest.approx(np.tan(SENTINEL) * 100.0), (
            "the percent path computed tan(-9999)*100 over the sentinel"
        )


class TestWarningsAsErrors:
    """Round 2 L1: the window reducers crashed a caller running -W error."""

    @staticmethod
    def _mostly_void() -> Dataset:
        """A raster whose interior windows are entirely no-data."""
        values = np.full((7, 7), SENTINEL, dtype="float32")
        values[0, 0] = 5.0
        return _raster(values)

    @pytest.mark.parametrize(
        "operation",
        [
            FocalMean(radius=1),
            FocalStd(radius=1),
            FocalMedian(radius=1),
            Texture(radius=1),
        ],
        ids=["mean", "std", "median", "texture"],
    )
    def test_a_window_full_of_voids_does_not_raise_under_w_error(self, operation):
        """`BandExpression` is hardened against this; the siblings now are too.

        `np.nanstd` over an all-NaN window otherwise surfaces not even the
        `RuntimeWarning` but a `SystemError` from the ufunc dispatcher when
        warnings are errors.
        """
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            operation.apply(self._mostly_void())
