"""Tests for `RasterAnalysis` and the `GEE.analyze` wiring."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis import (
    Ndvi,
    OperationChain,
    RasterAnalysis,
    Reclassify,
    Slope,
)

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _two_band() -> Dataset:
    """A two-band raster whose NDVI is 0.5 everywhere."""
    stack = np.stack(
        [np.full((2, 2), 0.6, dtype="float32"), np.full((2, 2), 0.2, dtype="float32")]
    )
    return Dataset.from_array(arr=stack, geo_ref=GEO, no_data_value=np.nan)


def _result(*objects: Dataset) -> SimpleNamespace:
    """Stand in for a `DownloadResult`, which exposes `objects` as a property."""
    return SimpleNamespace(objects=list(objects))


def _first(ds: Dataset) -> float:
    """Read the top-left cell of a raster's first band."""
    return float(np.asarray(ds.read_array(), dtype="float64")[0, 0])


class TestConstruction:
    """What `RasterAnalysis` accepts as its operations."""

    def test_a_single_operation_is_accepted(self):
        """`analyze(Ndvi(...))` should not need a list."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1))
        assert len(analysis.operations) == 1

    def test_a_sequence_is_accepted(self):
        """A list built elsewhere goes in as-is."""
        analysis = RasterAnalysis.of([Ndvi(nir=0, red=1), Slope()])
        assert len(analysis.operations) == 2

    def test_a_chain_is_flattened(self):
        """Handing in a chain extends the run rather than nesting it."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1) >> Slope())
        assert len(analysis.operations) == 2

    def test_an_analysis_is_itself_an_operation(self):
        """So it can be dropped into another chain."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1))
        assert isinstance(analysis >> Slope(), OperationChain)

    def test_the_name_joins_the_operations(self):
        """A run reports what it will do, in order."""
        analysis = RasterAnalysis.of(
            Ndvi(nir=0, red=1), Reclassify(bins=((0.3, 1.0, 1),))
        )
        assert analysis.name == "Ndvi >> Reclassify"

    def test_it_is_frozen(self):
        """An analysis is a value object like every operation."""
        analysis = RasterAnalysis.of(Ndvi())
        with pytest.raises(Exception, match="frozen"):
            analysis.operations = ()


class TestRunning:
    """Applying an analysis to a raster and to a download result."""

    def test_run_applies_every_operation_in_order(self):
        """NDVI then a threshold gives the class, not the index."""
        analysis = RasterAnalysis.of(
            Ndvi(nir=0, red=1), Reclassify(bins=((0.3, 1.0, 1),))
        )
        assert _first(analysis.run(_two_band())) == pytest.approx(1.0)

    def test_run_is_apply(self):
        """The alias exists for readability, not for different behaviour."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1))
        source = _two_band()
        assert _first(analysis.run(source)) == _first(analysis.apply(source))

    def test_an_empty_analysis_returns_the_input(self):
        """Nothing to run means nothing changes."""
        source = _two_band()
        assert RasterAnalysis.of([]).run(source) is source

    def test_it_runs_over_every_object_a_result_carries(self):
        """A multi-bucket download yields one derived raster per object."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1))
        derived = analysis.run_on_result(_result(_two_band(), _two_band()))
        assert len(derived) == 2
        assert all(_first(ds) == pytest.approx(0.5) for ds in derived)

    def test_a_result_with_no_object_says_what_to_do(self):
        """A download without materialize has nothing in memory to analyse."""
        with pytest.raises(ValueError, match="materialize=True"):
            RasterAnalysis.of(Ndvi()).run_on_result(_result())

    def test_write_to_returns_paths_and_writes_them(self, tmp_path):
        """With a directory the derived rasters land on disk."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1))
        written = analysis.run_on_result(_result(_two_band()), write_to=tmp_path)
        assert len(written) == 1
        assert written[0].exists()
        assert _first(Dataset.read_file(str(written[0]))) == pytest.approx(0.5)

    def test_write_to_creates_the_directory(self, tmp_path):
        """A caller should not have to pre-create the output directory."""
        target = tmp_path / "nested" / "out"
        written = RasterAnalysis.of(Ndvi(nir=0, red=1)).run_on_result(
            _result(_two_band()), write_to=target
        )
        assert written[0].parent == target


class TestTheBackendMethod:
    """`GEE.analyze` delegates and asks for the in-memory object."""

    def test_the_backend_exposes_analyze(self):
        """The wiring is a method on the backend, not a separate entry point."""
        from earthlens.gee import GEE

        assert callable(GEE.analyze)

    def test_analyze_downloads_with_materialize_and_runs_the_chain(self, monkeypatch):
        """It must force `materialize=True`, or there is no object to analyse."""
        from earthlens.gee import GEE

        captured: dict[str, object] = {}

        def _fake_download(self, *, progress_bar=True, materialize=None, **kwargs):
            """Record how the analysis asked for the download."""
            captured["materialize"] = materialize
            captured["progress_bar"] = progress_bar
            return _result(_two_band())

        monkeypatch.setattr(GEE, "download", _fake_download)
        derived = GEE.analyze(
            object.__new__(GEE), Ndvi(nir=0, red=1), progress_bar=False
        )
        assert captured == {"materialize": True, "progress_bar": False}
        assert _first(derived[0]) == pytest.approx(0.5)
