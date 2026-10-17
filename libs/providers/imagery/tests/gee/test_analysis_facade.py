"""Tests for `RasterAnalysis` and the `GEE.analyze` wiring."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis import (
    Ndvi,
    OperationChain,
    RasterAnalysis,
    Reclassify,
    Slope,
)
from earthlens.gee.work import Artifact, DownloadResult

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _two_band() -> Dataset:
    """A two-band raster whose NDVI is 0.5 everywhere."""
    stack = np.stack(
        [np.full((2, 2), 0.6, dtype="float32"), np.full((2, 2), 0.2, dtype="float32")]
    )
    return Dataset.from_array(arr=stack, geo_ref=GEO, no_data_value=np.nan)


def _result(*objects: Dataset, paths: tuple[str, ...] = ()) -> DownloadResult:
    """Build a real `DownloadResult` carrying `objects` as materialised.

    A `SimpleNamespace` with an `objects` attribute stood in for this, which is
    precisely what let the duck-typed read go unnoticed: the stand-in has no
    `artifacts`, so the tests never exercised the shape the backend returns.

    Args:
        objects: One in-memory raster per artifact.
        paths: Optional source file names, one per object, which the output
            naming reads.

    Returns:
        A `DownloadResult` whose artifacts carry those objects.
    """
    artifacts = tuple(
        Artifact(
            obj=obj,
            path=Path(paths[index]) if index < len(paths) else None,
            when=dt.datetime(2024, 1, index + 1),
        )
        for index, obj in enumerate(objects)
    )
    return DownloadResult(artifacts=artifacts)


def _first(ds: Dataset) -> float:
    """Read the top-left cell of a raster's first band."""
    return float(np.asarray(ds.read_array(), dtype="float64")[0, 0])


#: How `GEE.analyze` asked for its download, recorded by the stand-in below.
#: Module-level because the recorder is too, and a nested helper inside a test
#: body is against this repo's test conventions.
_CAPTURED: dict[str, object] = {}


def _recording_download(self, *, progress_bar=True, materialize=None, **kwargs):  # noqa: ANN001, ANN201, ARG001
    """Stand in for `GEE.download`, recording the arguments it was given."""
    _CAPTURED["materialize"] = materialize
    _CAPTURED["progress_bar"] = progress_bar
    return _result(_two_band())


def _bare_backend(export_via: str):  # noqa: ANN201
    """An uninitialised `GEE` carrying only the sink `analyze` consults.

    Built without `__init__` on purpose: a real one parses the catalog and
    authenticates, and neither is part of what the delegation does.

    Args:
        export_via: The sink name to resolve onto the instance.

    Returns:
        The instance.
    """
    from earthlens.gee import GEE
    from earthlens.gee.sinks import resolve_sink

    backend = object.__new__(GEE)
    backend._sink = resolve_sink(export_via)
    return backend


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
        with pytest.raises(ValidationError, match="frozen"):
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
        paths_only = DownloadResult(artifacts=(Artifact(path=Path("a.tif")),))
        with pytest.raises(ValueError, match="materialize=True"):
            RasterAnalysis.of(Ndvi()).run_on_result(paths_only)

    def test_write_to_returns_paths_and_writes_them(self, tmp_path):
        """With a directory the derived rasters land on disk."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1))
        written = analysis.run_on_result(_result(_two_band()), write_to=tmp_path)
        assert len(written) == 1
        assert written[0].exists()
        assert written[0].name == "20240101T000000_ndvi.tif"
        assert _first(Dataset.read_file(written[0])) == pytest.approx(0.5)

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

        _CAPTURED.clear()
        monkeypatch.setattr(GEE, "download", _recording_download)
        derived = GEE.analyze(
            _bare_backend("url"), Ndvi(nir=0, red=1), progress_bar=False
        )
        assert _CAPTURED == {"materialize": True, "progress_bar": False}
        assert _first(derived[0]) == pytest.approx(0.5)

    def test_a_batch_sink_is_refused_before_anything_is_downloaded(self):
        """The old path reported a `materialize=True` problem instead.

        `analyze` sets `materialize=True` itself, so `download`'s refusal
        blamed a flag the caller never passed. The sink is asked directly, and
        asked before the download, so nothing is exported first.
        """
        from earthlens.gee import GEE

        with pytest.raises(ValueError, match="exports via 'drive'"):
            GEE.analyze(_bare_backend("drive"), Ndvi(nir=0, red=1))


class TestWhatRunOnResultRefuses:
    """Round-1 finding M8: every wrong argument got the same wrong advice."""

    def test_a_bare_raster_is_told_to_use_run(self):
        """The most likely mistake, and `materialize=True` is not the answer."""
        with pytest.raises(TypeError, match="single raster; call run"):
            RasterAnalysis.of(Ndvi(nir=0, red=1)).run_on_result(_two_band())

    def test_a_list_of_rasters_is_told_how_to_iterate(self):
        """A plain list is the second most likely, and also not a materialize
        problem."""
        rasters = [_two_band(), _two_band()]
        with pytest.raises(TypeError, match="not a sequence of 2"):
            RasterAnalysis.of(Ndvi(nir=0, red=1)).run_on_result(rasters)

    def test_an_unrelated_object_is_named(self):
        """Anything else says what it got rather than guessing why."""
        with pytest.raises(TypeError, match="got int, which carries no artifacts"):
            RasterAnalysis.of(Ndvi(nir=0, red=1)).run_on_result(7)

    def test_the_materialize_advice_is_kept_for_the_case_it_fits(self):
        """A real result with paths but no objects is the one that earns it."""
        paths_only = DownloadResult(
            artifacts=(Artifact(path=Path("a.tif")), Artifact(path=Path("b.tif")))
        )
        with pytest.raises(ValueError, match="2 artifact\\(s\\)"):
            RasterAnalysis.of(Ndvi(nir=0, red=1)).run_on_result(paths_only)


class TestOutputNaming:
    """Round-1 finding M11: `analysis_{index}.tif` tied a file to nothing."""

    def test_an_output_is_named_after_its_source_granule(self, tmp_path):
        """A multi-bucket download has to be tellable apart afterwards."""
        written = RasterAnalysis.of(Ndvi(nir=0, red=1)).run_on_result(
            _result(
                _two_band(), _two_band(), paths=("S2_20240101.tif", "S2_20240102.tif")
            ),
            write_to=tmp_path,
        )
        assert [path.name for path in written] == [
            "S2_20240101_ndvi.tif",
            "S2_20240102_ndvi.tif",
        ]

    def test_the_bucket_timestamp_names_it_when_there_is_no_path(self, tmp_path):
        """An in-memory-only artifact still gets a distinguishing name."""
        written = RasterAnalysis.of(Ndvi(nir=0, red=1)).run_on_result(
            _result(_two_band(), _two_band()), write_to=tmp_path
        )
        assert [path.name for path in written] == [
            "20240101T000000_ndvi.tif",
            "20240102T000000_ndvi.tif",
        ]

    def test_the_chain_is_part_of_the_name(self, tmp_path):
        """Two analyses of one download must not collide either."""
        result = _result(_two_band(), paths=("S2_20240101.tif",))
        analysis = RasterAnalysis.of(
            Ndvi(nir=0, red=1), Reclassify(bins=((0.3, 1.0, 1),))
        )
        written = analysis.run_on_result(result, write_to=tmp_path)
        assert written[0].name == "S2_20240101_ndvi_reclassify.tif"

    def test_a_re_run_reports_the_collision_rather_than_overwriting(self, tmp_path):
        """A time-series loop silently clobbered every earlier iteration."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1))
        result = _result(_two_band(), paths=("S2_20240101.tif",))
        analysis.run_on_result(result, write_to=tmp_path)
        with pytest.raises(FileExistsError, match="overwrite=True"):
            analysis.run_on_result(result, write_to=tmp_path)

    def test_overwrite_replaces_it_on_request(self, tmp_path):
        """Opting in is one argument, and then the re-run is allowed."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1))
        result = _result(_two_band(), paths=("S2_20240101.tif",))
        first = analysis.run_on_result(result, write_to=tmp_path)
        again = analysis.run_on_result(result, write_to=tmp_path, overwrite=True)
        assert first == again


class TestItIsAChain:
    """Round-1 finding L9: the fold lived in two places."""

    def test_an_analysis_is_an_operation_chain(self):
        """One implementation of apply/name/len, not a second copy."""
        analysis = RasterAnalysis.of(Ndvi(nir=0, red=1), Slope())
        assert isinstance(analysis, OperationChain)
        assert analysis.name == "Ndvi >> Slope"
        assert len(analysis) == 2

    def test_a_chain_handed_in_is_still_flattened(self):
        """The one isinstance has to keep covering both types."""
        chain = Ndvi(nir=0, red=1) >> Slope()
        assert len(RasterAnalysis.of(chain, Slope()).operations) == 3
        assert len(RasterAnalysis.of(RasterAnalysis.of(chain)).operations) == 2


class TestTheUncoveredBranches:
    """Round-1 finding M14: reachable paths the suite never took."""

    def test_the_constructor_accepts_one_operation_directly(self):
        """`of` always passes a tuple, so this branch needed the constructor."""
        analysis = RasterAnalysis(operations=Ndvi(nir=0, red=1))
        assert len(analysis.operations) == 1
        assert analysis.name == "Ndvi"

    def test_an_artifact_with_neither_a_path_nor_a_time_falls_back_to_the_index(
        self, tmp_path
    ):
        """The last resort of the output naming, which nothing else reaches."""
        result = DownloadResult(artifacts=(Artifact(obj=_two_band()),))
        written = RasterAnalysis.of(Ndvi(nir=0, red=1)).run_on_result(
            result, write_to=tmp_path
        )
        assert written[0].name == "analysis_0_ndvi.tif"


class TestANonIterableArgument:
    """Round 3 L1: a non-iterable bad argument raised a bare TypeError."""

    @pytest.mark.parametrize("bad", [123, 3.5], ids=["int", "float"])
    def test_a_non_iterable_is_a_validation_error_not_a_bare_type_error(self, bad):
        """`RasterAnalysis.of(7)` used to raise `'int' object is not iterable`.

        The docstring promises a `ValueError`; `ValidationError` is one, and
        now the message names what was expected instead of leaking a loop's
        internal `TypeError`.
        """
        with pytest.raises(ValidationError, match="RasterOperation, a chain"):
            RasterAnalysis.of(bad)

    def test_a_string_is_still_rejected(self):
        """A str is iterable, so it fails later - but still as a validation error."""
        with pytest.raises(ValidationError):
            RasterAnalysis.of("ndvi")
