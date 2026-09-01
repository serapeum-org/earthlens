"""Cross-backend guard for the polygon `aoi=` contract.

A polygon area of interest is reduced to `lat_lim` / `lon_lim` **and** carried as
a mask on `space.geometry`. Only some backends clip to that mask; the rest return
the polygon's bounding box. These tests pin which backends do which, and that the
bbox-only ones warn rather than degrade silently.
"""

from __future__ import annotations

import warnings

import pytest

from earthlens.base import PolygonAoiWarning, SpatialExtent
from earthlens.earthlens import EarthLens

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

#: The backends that clip to the exact polygon, pinned by class name. Adding a
#: `crop_to_aoi` / `mask_to_geometry` call to a backend means adding it here.
_POLYGON_CAPABLE = {
    "Bathymetry",
    "CHIRPS",
    "ECMWF",
    "FABDEM",
    "GEE",
    "GHSL",
    "JRC",
    "NWP",
    "S3",
    "SoilGrids",
    "STAC",
    "WorldPop",
}


class TestSupportsPolygonAoiRoster:
    """`SUPPORTS_POLYGON_AOI` matches the backends that read the mask."""

    def test_roster_is_exactly_the_mask_reading_backends(self):
        """Only backends that crop to the mask declare polygon support."""
        capable = {
            backend.__name__ for _key, backend in _CASES if backend.SUPPORTS_POLYGON_AOI
        }
        assert capable == _POLYGON_CAPABLE

    @pytest.mark.parametrize("key,backend", _CASES, ids=_IDS)
    def test_flag_is_a_bool(self, key, backend):
        """Every backend resolves the flag to a real bool."""
        assert isinstance(backend.SUPPORTS_POLYGON_AOI, bool)

    def test_default_is_no_polygon_support(self):
        """A new backend must opt in, so it cannot silently claim support."""
        assert EarthLens.DataSources["gdacs"].SUPPORTS_POLYGON_AOI is False


class _Recorder:
    """Stand-in exposing just what `_attach_clip_geometry` touches."""

    SUPPORTS_POLYGON_AOI = False
    OUTPUT_KIND = "raster"

    def __init__(self, supports: bool, output_kind: str = "raster"):
        self.SUPPORTS_POLYGON_AOI = supports
        self.OUTPUT_KIND = output_kind
        self.space = SpatialExtent.from_pairs(lat_lim=[0.0, 1.0], lon_lim=[0.0, 1.0])


class TestAttachClipGeometryWarning:
    """`_attach_clip_geometry` warns exactly when the mask will be ignored."""

    @staticmethod
    def _attach(supports: bool, geometry: object, output_kind: str = "raster"):
        """Run the base implementation against a recorder, returning warnings."""
        from earthlens.base import AbstractDataSource

        recorder = _Recorder(supports, output_kind)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            AbstractDataSource._attach_clip_geometry(recorder, geometry)
        return recorder, caught

    def test_bbox_only_backend_warns(self):
        """A polygon mask on a bbox-only backend raises PolygonAoiWarning."""
        _recorder, caught = self._attach(False, object())
        assert any(issubclass(w.category, PolygonAoiWarning) for w in caught)

    def test_capable_backend_does_not_warn(self):
        """A backend that clips to the polygon stays quiet."""
        _recorder, caught = self._attach(True, object())
        assert not any(issubclass(w.category, PolygonAoiWarning) for w in caught)

    def test_no_geometry_does_not_warn(self):
        """A bbox aoi loses nothing, so it never warns."""
        _recorder, caught = self._attach(False, None)
        assert not any(issubclass(w.category, PolygonAoiWarning) for w in caught)

    def test_mask_is_recorded_even_when_unsupported(self):
        """The mask is still stored, so a later migration needs no facade change."""
        marker = object()
        recorder, _caught = self._attach(False, marker)
        assert recorder.space.geometry is marker

    def test_warning_names_the_backend_and_the_remedy(self):
        """The message identifies the class and points at the post-clip fix."""
        _recorder, caught = self._attach(False, object())
        message = str(caught[0].message)
        assert "_Recorder" in message
        assert "crop(mask=" in message  # _Recorder inherits OUTPUT_KIND="raster"


class TestPolygonAoiThroughFacade:
    """End-to-end: a polygon `aoi=` warns for a bbox-only backend."""

    def test_polygon_aoi_warns_on_bbox_only_backend(self, tmp_path):
        """cmems clips server-side to a bbox, so a polygon aoi warns."""
        with pytest.warns(PolygonAoiWarning, match="bounding box only"):
            EarthLens(
                "cmems",
                variables=["thetao"],
                dataset="cmems_mod_glo_phy_my_0.083deg_P1D-m",
                start="2020-01-01",
                end="2020-01-02",
                aoi="POLYGON ((-10 35, 5 35, -2 45, -10 35))",
                path=str(tmp_path),
            )

    def test_bbox_aoi_is_silent(self, tmp_path):
        """A rectangular aoi is exact, so no warning fires."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            EarthLens(
                "cmems",
                variables=["thetao"],
                dataset="cmems_mod_glo_phy_my_0.083deg_P1D-m",
                start="2020-01-01",
                end="2020-01-02",
                aoi=[-10.0, 35.0, 5.0, 45.0],
                path=str(tmp_path),
            )
        assert not any(issubclass(w.category, PolygonAoiWarning) for w in caught)


class TestRemedyMatchesOutputKind:
    """The warning's advice suits the backend's output shape."""

    @staticmethod
    def _message(output_kind: str) -> str:
        """Return the warning text for a bbox-only backend of `output_kind`."""
        import warnings as w

        from earthlens.base import AbstractDataSource

        recorder = _Recorder(False, output_kind)
        with w.catch_warnings(record=True) as caught:
            w.simplefilter("always")
            AbstractDataSource._attach_clip_geometry(recorder, object())
        return str(caught[0].message)

    @pytest.mark.parametrize("output_kind", ["raster", "mixed"])
    def test_raster_kinds_get_the_pyramids_remedy(self, output_kind):
        """A gridded backend is told to post-clip with pyramids."""
        assert "crop(mask=" in self._message(output_kind)

    @pytest.mark.parametrize("output_kind", ["vector", "tabular"])
    def test_row_kinds_get_a_row_filter_remedy(self, output_kind):
        """A FeatureCollection / DataFrame backend is told to filter rows instead."""
        message = self._message(output_kind)
        assert "crop(mask=" not in message
        assert "within" in message
