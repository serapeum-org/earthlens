from __future__ import annotations

from types import SimpleNamespace

from earthlens.base.cache import (
    AOI_SIDECAR_SUFFIX,
    aoi_tag,
    sidecar_is_fresh,
    sidecar_path,
    write_sidecar,
)


def _space(west, south, east, north, geometry=None):
    """Return a minimal duck-typed spatial extent for the cache helpers."""
    return SimpleNamespace(
        west=west, south=south, east=east, north=north, geometry=geometry
    )


class TestAoiTag:
    """`aoi_tag` keys a request by bbox, folding in any polygon geometry."""

    def test_bbox_only(self):
        """A geometry-less extent tags to its bare `w,s,e,n` bbox."""
        assert aoi_tag(_space(0.4, 50.4, 0.6, 50.6)) == "0.4,50.4,0.6,50.6"

    def test_polygon_appends_hash(self):
        """A geometry with `to_json` appends a `|<sha256>` segment to the bbox."""
        geom = SimpleNamespace(to_json=lambda: '{"type": "Polygon"}')
        tag = aoi_tag(_space(0.4, 50.4, 0.6, 50.6, geometry=geom))
        assert tag.startswith("0.4,50.4,0.6,50.6|")
        assert len(tag.split("|")[1]) == 64

    def test_same_bbox_different_polygon_differs(self):
        """Two requests sharing a bbox but not a polygon get distinct tags."""
        a = aoi_tag(_space(0, 0, 1, 1, geometry=SimpleNamespace(to_json=lambda: "A")))
        b = aoi_tag(_space(0, 0, 1, 1, geometry=SimpleNamespace(to_json=lambda: "B")))
        assert a != b

    def test_geometry_wkt_fallback(self):
        """A geometry without `to_json` falls back to its `.wkt`."""
        geom = SimpleNamespace(wkt="POLYGON((0 0,1 0,1 1,0 0))")
        assert "|" in aoi_tag(_space(0, 0, 1, 1, geometry=geom))

    def test_geometry_str_fallback(self):
        """A geometry with neither `to_json` nor `wkt` falls back to `str()`."""

        class _Geom:
            def __str__(self) -> str:
                return "geom-repr"

        tag = aoi_tag(_space(0, 0, 1, 1, geometry=_Geom()))
        assert tag.startswith("0,0,1,1|")
        assert len(tag.split("|")[1]) == 64

    def test_falsy_non_none_geometry_still_hashed(self):
        """A geometry that is falsy but not None is still folded into the tag."""

        class _EmptyGeom:
            def __bool__(self) -> bool:
                return False

            def __str__(self) -> str:
                return "empty"

        assert "|" in aoi_tag(_space(0, 0, 1, 1, geometry=_EmptyGeom()))


class TestSidecar:
    """The `<target>.aoi` sidecar records the AOI a cached file was written for."""

    def test_sidecar_path_appends_suffix(self, tmp_path):
        """The sidecar sits beside the target with the `.aoi` suffix appended."""
        target = tmp_path / "fabdem_V1-2.tif"
        assert sidecar_path(target) == tmp_path / (
            "fabdem_V1-2.tif" + AOI_SIDECAR_SUFFIX
        )

    def test_write_then_fresh_round_trip(self, tmp_path):
        """A written sidecar reads back as fresh for the same tag."""
        target = tmp_path / "out.tif"
        target.write_bytes(b"raster")
        write_sidecar(target, "0,0,1,1")
        assert sidecar_is_fresh(target, "0,0,1,1")

    def test_stale_tag_is_not_fresh(self, tmp_path):
        """A sidecar recording a different AOI is not fresh."""
        target = tmp_path / "out.tif"
        target.write_bytes(b"raster")
        write_sidecar(target, "9,9,9.1,9.1")
        assert not sidecar_is_fresh(target, "0,0,1,1")

    def test_missing_output_is_not_fresh(self, tmp_path):
        """A sidecar without its output file is not fresh."""
        target = tmp_path / "out.tif"
        write_sidecar(target, "0,0,1,1")
        assert not sidecar_is_fresh(target, "0,0,1,1")

    def test_missing_sidecar_is_not_fresh(self, tmp_path):
        """An output file with no sidecar is not fresh."""
        target = tmp_path / "out.tif"
        target.write_bytes(b"raster")
        assert not sidecar_is_fresh(target, "0,0,1,1")
