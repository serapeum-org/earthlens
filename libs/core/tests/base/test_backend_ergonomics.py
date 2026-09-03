from __future__ import annotations

import inspect

import pytest

from earthlens.chc import CHIRPS


class TestBackendDirectErgonomics:
    """The aoi= / cadence= / dataset= sugar works on backend classes directly."""

    def _chc(self, tmp_path, **kwargs):
        return CHIRPS(
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path=str(tmp_path),
            **kwargs,
        )

    def test_aoi_on_backend_sets_extent(self, tmp_path):
        """A direct CHIRPS(aoi=...) reduces to the right SpatialExtent."""
        space = self._chc(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64]).space
        assert (space.south, space.north, space.west, space.east) == (
            4.19,
            4.64,
            -75.65,
            -74.73,
        )

    def test_aoi_matches_lat_lon_pairs(self, tmp_path):
        """aoi= and the lat_lim/lon_lim pair yield the same extent."""
        via_aoi = self._chc(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64]).space
        via_pairs = self._chc(
            tmp_path, lat_lim=[4.19, 4.64], lon_lim=[-75.65, -74.73]
        ).space
        assert via_aoi == via_pairs

    def test_point_aoi_with_buffer(self, tmp_path):
        """A point aoi with buffer builds a square extent on the backend."""
        space = self._chc(tmp_path, aoi=(-75.0, 4.0), buffer=0.25).space
        assert (space.south, space.north, space.west, space.east) == (
            3.75,
            4.25,
            -75.25,
            -74.75,
        )

    def test_cadence_on_backend(self, tmp_path):
        """cadence= overrides temporal_resolution on the backend."""
        backend = self._chc(tmp_path, cadence="monthly")
        assert backend.temporal_resolution == "monthly"

    def test_aoi_with_lat_lim_raises(self, tmp_path):
        """Both aoi= and lat_lim= on the backend is rejected."""
        with pytest.raises(ValueError, match="either aoi= or lat_lim"):
            self._chc(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64], lat_lim=[4, 5])

    def test_buffer_without_aoi_raises(self, tmp_path):
        """buffer= without a point aoi= on the backend is rejected."""
        with pytest.raises(ValueError, match="buffer= only applies"):
            self._chc(tmp_path, buffer=0.5)

    def test_signature_keeps_native_params_and_adds_the_ergonomic_ones(self):
        """The wrapper preserves the backend's own parameters and appends its own.

        It deliberately no longer reports the *unwrapped* signature: the four
        kwargs the wrapper accepts were invisible to `help()` and to IDE
        completion while it did.
        """
        params = inspect.signature(CHIRPS.__init__).parameters
        assert "variables" in params and "lat_lim" in params, (
            f"native parameters must survive, got {list(params)}"
        )
        assert not any(p.kind == p.VAR_KEYWORD for p in params.values()), (
            "the wrapper must not degrade the signature to **kwargs"
        )
        for name in ("aoi", "buffer", "cadence", "dataset"):
            assert name in params, f"{name} should be advertised, got {list(params)}"
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{name} must be keyword-only"
            )


class TestBackendPolygonMask:
    """A polygon aoi= records a mask on space; a bbox aoi= does not."""

    def _chc(self, tmp_path, **kwargs):
        return CHIRPS(
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path=str(tmp_path),
            **kwargs,
        )

    def test_polygon_aoi_attaches_geometry(self, tmp_path):
        """A polygon aoi= sets the bbox envelope and a GeoDataFrame mask."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        poly = shapely.geometry.Polygon([(-75, 4), (-74, 4), (-74.5, 5)])
        gdf = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
        space = self._chc(tmp_path, aoi=gdf).space
        assert space.geometry is not None, "polygon aoi should attach a mask"
        assert isinstance(space.geometry, gpd.GeoDataFrame), f"{space.geometry!r}"
        assert (space.west, space.east) == (-75.0, -74.0), "bbox envelope wrong"

    def test_bbox_aoi_leaves_geometry_none(self, tmp_path):
        """A plain bbox aoi= leaves space.geometry as None (bbox clip is exact)."""
        space = self._chc(tmp_path, aoi=[-75.0, 4.0, -74.0, 5.0]).space
        assert space.geometry is None, f"bbox should attach no mask: {space.geometry!r}"

    def test_attach_clip_geometry_copies_extent(self, tmp_path):
        """_attach_clip_geometry replaces space with a geometry-bearing copy."""
        backend = self._chc(tmp_path, aoi=[-75.0, 4.0, -74.0, 5.0])
        before = backend.space
        sentinel = object()
        backend._attach_clip_geometry(sentinel)
        assert backend.space.geometry is sentinel, "geometry not attached"
        assert (backend.space.west, backend.space.east) == (before.west, before.east)
        assert before.geometry is None, "original frozen extent must be unchanged"

    def test_geometry_excluded_from_serialisation(self, tmp_path):
        """The mask is excluded from model_dump so it never breaks serialisation."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        poly = shapely.geometry.Polygon([(-75, 4), (-74, 4), (-74.5, 5)])
        gdf = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
        space = self._chc(tmp_path, aoi=gdf).space
        assert "geometry" not in space.model_dump(), "geometry must not serialise"


class TestSpatialExtentGeometryIdentity:
    """SpatialExtent equality / hashing ignore the polygon geometry (M1)."""

    def _gdf(self, x):
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        return gpd.GeoDataFrame(
            geometry=[shapely.geometry.box(x, x, x + 1, x + 1)], crs="EPSG:4326"
        )

    def test_equal_bbox_different_geometry_compares_equal(self):
        """Two extents over one bbox are equal whatever their masks."""
        from earthlens.base.abstractdatasource import SpatialExtent

        base = SpatialExtent.from_pairs([0.0, 5.0], [0.0, 5.0])
        a = base.model_copy(update={"geometry": self._gdf(0)})
        b = base.model_copy(update={"geometry": self._gdf(1)})
        assert a == b, "same bbox must compare equal regardless of geometry"

    def test_geometry_vs_none_does_not_raise(self):
        """Comparing a polygon extent to a bbox extent returns a bool, not a crash."""
        from earthlens.base.abstractdatasource import SpatialExtent

        base = SpatialExtent.from_pairs([0.0, 5.0], [0.0, 5.0])
        with_geom = base.model_copy(update={"geometry": self._gdf(0)})
        assert with_geom == base, "geometry-vs-None must not raise and must be equal"

    def test_different_bbox_not_equal(self):
        """A different bbox is not equal even with the same mask object."""
        from earthlens.base.abstractdatasource import SpatialExtent

        gdf = self._gdf(0)
        a = SpatialExtent.from_pairs([0.0, 5.0], [0.0, 5.0]).model_copy(
            update={"geometry": gdf}
        )
        b = SpatialExtent.from_pairs([0.0, 9.0], [0.0, 5.0]).model_copy(
            update={"geometry": gdf}
        )
        assert a != b, "different bbox must not compare equal"

    def test_polygon_extent_is_hashable(self):
        """A polygon-aoi extent hashes (by bbox) and can go in a set."""
        from earthlens.base.abstractdatasource import SpatialExtent

        base = SpatialExtent.from_pairs([0.0, 5.0], [0.0, 5.0])
        a = base.model_copy(update={"geometry": self._gdf(0)})
        assert hash(a) == hash(base), "geometry must not affect the hash"
        assert len({a, base}) == 1, "equal-bbox extents collapse in a set"


@pytest.mark.ecmwf
class TestBackendDirectDatasetSplit:
    """dataset= splits into the keyed variables dict on a dataset-keyed backend."""

    def test_dataset_composes_variables_dict(self, tmp_path):
        """ECMWF(dataset=, variables=[...]) composes the {dataset: [...]} dict."""
        from earthlens.ecmwf import ECMWF

        backend = ECMWF(
            dataset="reanalysis-era5-single-levels",
            variables=["2m-temperature"],
            cadence="monthly",
            start="2022-01-01",
            end="2022-02-01",
            aoi=[-75.0, 4.0, -74.0, 5.0],
            path=str(tmp_path),
        )
        assert backend.vars == {"reanalysis-era5-single-levels": ["2m-temperature"]}
        assert backend.temporal_resolution == "monthly"
        assert (backend.space.west, backend.space.east) == (-75.0, -74.0)

    def test_dataset_with_dict_variables_raises(self, tmp_path):
        """dataset= with a dict variables on the backend is rejected."""
        from earthlens.ecmwf import ECMWF

        with pytest.raises(ValueError, match="pass variables= as a list"):
            ECMWF(
                dataset="reanalysis-era5-single-levels",
                variables={"reanalysis-era5-single-levels": ["2m-temperature"]},
                start="2022-01-01",
                end="2022-02-01",
                lat_lim=[4.0, 5.0],
                lon_lim=[-75.0, -74.0],
                path=str(tmp_path),
            )


class TestAuthenticate:
    """The explicit authenticate() surface and the unified error type."""

    def test_no_auth_backend_is_noop(self, tmp_path):
        """authenticate() on a credential-free backend (CHIRPS) returns self."""
        chc = CHIRPS(
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            lat_lim=[4, 5],
            lon_lim=[-75, -74],
            path=str(tmp_path),
        )
        assert chc.authenticate() is chc, (
            "authenticate() should return self for chaining"
        )

    def test_authentication_errors_share_a_base(self):
        """Every backend's AuthenticationError subclasses the shared base."""
        from earthlens.base import AuthenticationError as BaseAuthError
        from earthlens.ecmwf import AuthenticationError as EcmwfAuthError
        from earthlens.gee import AuthenticationError as GeeAuthError

        assert issubclass(EcmwfAuthError, BaseAuthError), "ECMWF must subclass base"
        assert issubclass(GeeAuthError, BaseAuthError), "GEE must subclass base"


class TestLazyClientMixin:
    """The mixin's default _open_client guard."""

    def test_missing_open_client_raises(self):
        """A LazyClientMixin user that forgets _open_client fails on access."""
        from earthlens.base import LazyClientMixin

        class _NoOpen(LazyClientMixin):
            pass

        with pytest.raises(NotImplementedError, match="_open_client"):
            _NoOpen().client


class TestNativeAoiBackendUnaffected:
    """A backend with its own aoi= (WorldPop) keeps interpreting it."""

    def test_worldpop_native_aoi(self, tmp_path):
        """WorldPop(aoi='COM') passes the ISO3 through, not normalize_aoi."""
        pytest.importorskip("earthlens.worldpop")
        from earthlens.worldpop import WorldPop

        backend = WorldPop(
            variables=["pop"],
            start="2020",
            end="2020",
            fmt="%Y",
            aoi="COM",
            lat_lim=[-90, 90],
            lon_lim=[-180, 180],
            path=str(tmp_path),
        )
        assert backend is not None
