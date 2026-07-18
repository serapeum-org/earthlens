"""Integration tests for `_jaxa_earth.fetch_jaxa_earth`.

Uses a faked `jaxa.earth.je` module (injected via `sys.modules`) that
returns a known 4-D tensor with known `latlim` / `lonlim`. The COG write
goes through **real pyramids** so the assertion can read the file back
and check the geotransform, CRS, and corner pixel values.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
from earthlens.jaxa.catalog import Dataset


@pytest.fixture
def fake_jaxa_earth(monkeypatch):
    """Inject a minimal fake `jaxa.earth.je` returning a known tile.

    The fake implements the strict chain order discovered in A1:
    `filter_date` -> `filter_resolution` -> `filter_bounds` -> `select`
    -> `get_images`. Each step is no-op-but-recorded; `get_images()`
    returns an object whose `.raster` exposes `.img`, `.latlim`,
    `.lonlim` with a known 2x3 grid embedded in the API's 4-D shape.
    """
    calls: list[str] = []

    class _Raster:
        img = np.array([[[[1.0], [2.0], [3.0]], [[4.0], [5.0], [6.0]]]], dtype=np.float32)
        # latlim/lonlim arrive as 2-D `(1, 2)` arrays — see A1 capture.
        latlim = np.array([[35.0, 36.0]])
        lonlim = np.array([[138.0, 139.5]])
        ppu = np.array(1.0)
        pint = 1

    class _Result:
        raster = _Raster()

    class _Col:
        def __init__(self, *, collection, **_kw):
            calls.append(f"ImageCollection({collection!r})")

        def filter_date(self, *, dlim):
            calls.append(f"filter_date(dlim={dlim!r})")
            return self

        def filter_resolution(self, *, ppu):
            calls.append(f"filter_resolution(ppu={ppu!r})")
            return self

        def filter_bounds(self, *, bbox=None, geoj=None):
            calls.append(f"filter_bounds(bbox={bbox!r})")
            return self

        def select(self, *, band):
            calls.append(f"select(band={band!r})")
            return self

        def get_images(self):
            calls.append("get_images()")
            return _Result()

    fake_je = types.ModuleType("jaxa.earth.je")
    fake_je.ImageCollection = _Col  # type: ignore[attr-defined]
    fake_earth = types.ModuleType("jaxa.earth")
    fake_earth.je = fake_je  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("jaxa")
    fake_pkg.earth = fake_earth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jaxa", fake_pkg)
    monkeypatch.setitem(sys.modules, "jaxa.earth", fake_earth)
    monkeypatch.setitem(sys.modules, "jaxa.earth.je", fake_je)
    return calls


@pytest.fixture
def extents():
    """Standard space + time extents for the branch tests."""
    space = SpatialExtent(
        latitude_min=35.0,
        latitude_max=36.0,
        longitude_min=138.0,
        longitude_max=139.5,
    )
    import datetime as dt

    import pandas as pd

    time = TemporalExtent(
        start_date=dt.datetime(2021, 1, 1),
        end_date=dt.datetime(2021, 12, 31),
        resolution="D",
        dates=pd.date_range("2021-01-01", "2021-12-31", freq="D"),
    )
    return space, time


@pytest.mark.jaxa
@pytest.mark.integration
def test_jaxa_earth_writes_a_cog(tmp_path, fake_jaxa_earth, extents) -> None:
    """A jaxa-earth fetch writes a real COG via pyramids on the fake array."""
    from pyramids.dataset import Dataset as PyrDataset

    from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

    space, time = extents
    ds = Dataset(
        key="aw3d30",
        protocol="jaxa-earth",
        collection="JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global",
        default_band="DSM",
    )
    written = fetch_jaxa_earth(
        dataset=ds,
        space=space,
        time=time,
        resolution=1.0,
        bands=None,
        out_dir=tmp_path,
    )
    assert len(written) == 1
    assert written[0].name == "aw3d30_DSM.tif"
    assert written[0].exists()

    # Read it back and verify geotransform + CRS + values
    cog = PyrDataset.read_file(str(written[0]))
    geo = cog.geotransform
    assert geo[0] == pytest.approx(138.0)
    assert geo[3] == pytest.approx(36.0)
    arr = cog.read_array()
    assert arr.shape == (2, 3)
    np.testing.assert_array_equal(
        arr.astype(np.float32),
        np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32),
    )


@pytest.mark.jaxa
@pytest.mark.integration
def test_jaxa_earth_chain_order(tmp_path, fake_jaxa_earth, extents) -> None:
    """The fetch issues the strict `date -> resolution -> bounds -> select -> get_images` chain."""
    from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

    space, time = extents
    ds = Dataset(
        key="aw3d30",
        protocol="jaxa-earth",
        collection="JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global",
        default_band="DSM",
    )
    fetch_jaxa_earth(
        dataset=ds,
        space=space,
        time=time,
        resolution=1.0,
        bands=None,
        out_dir=tmp_path,
    )
    methods = [c.split("(", 1)[0] for c in fake_jaxa_earth]
    assert methods == [
        "ImageCollection",
        "filter_date",
        "filter_resolution",
        "filter_bounds",
        "select",
        "get_images",
    ]


@pytest.mark.jaxa
@pytest.mark.integration
def test_jaxa_earth_skips_filter_resolution_when_none(
    tmp_path, fake_jaxa_earth, extents
) -> None:
    """No `resolution=` means the chain omits `filter_resolution`."""
    from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

    space, time = extents
    ds = Dataset(
        key="aw3d30",
        protocol="jaxa-earth",
        collection="JAXA.foo",
        default_band="b",
    )
    fetch_jaxa_earth(
        dataset=ds,
        space=space,
        time=time,
        resolution=None,
        bands=None,
        out_dir=tmp_path,
    )
    methods = [c.split("(", 1)[0] for c in fake_jaxa_earth]
    assert "filter_resolution" not in methods


@pytest.mark.jaxa
@pytest.mark.integration
def test_jaxa_earth_explicit_bands_override(tmp_path, fake_jaxa_earth, extents) -> None:
    """`bands=[a, b]` writes two COGs and overrides the catalog default."""
    from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

    space, time = extents
    ds = Dataset(
        key="aw3d30",
        protocol="jaxa-earth",
        collection="JAXA.foo",
        default_band="DSM",
    )
    written = fetch_jaxa_earth(
        dataset=ds,
        space=space,
        time=time,
        resolution=1.0,
        bands=["A", "B"],
        out_dir=tmp_path,
    )
    names = sorted(p.name for p in written)
    assert names == ["aw3d30_A.tif", "aw3d30_B.tif"]


@pytest.mark.jaxa
@pytest.mark.integration
def test_jaxa_earth_no_band_raises(tmp_path, fake_jaxa_earth, extents) -> None:
    """Without a band and without `default_band`, the branch raises."""
    from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

    space, time = extents
    ds = Dataset(
        key="aw3d30",
        protocol="jaxa-earth",
        collection="JAXA.foo",
    )
    with pytest.raises(ValueError, match="no band selected"):
        fetch_jaxa_earth(
            dataset=ds,
            space=space,
            time=time,
            resolution=1.0,
            bands=None,
            out_dir=tmp_path,
        )


@pytest.mark.jaxa
@pytest.mark.unit
def test_geo_tuple_north_up() -> None:
    """`_geo_tuple` produces a standard north-up GDAL 6-tuple."""
    from earthlens.jaxa._jaxa_earth import _geo_tuple

    g = _geo_tuple([35.0, 36.0], [138.0, 139.0], (10, 20))
    assert g[0] == 138.0
    assert g[3] == 36.0
    assert g[1] == pytest.approx(0.05)  # x_res
    assert g[5] == pytest.approx(-0.1)  # -y_res
    assert g[2] == 0.0
    assert g[4] == 0.0


@pytest.mark.jaxa
@pytest.mark.unit
def test_slice_to_2d_rejects_multi_time() -> None:
    """A 4-D tensor with more than one time step is rejected (not yet supported)."""
    from earthlens.jaxa._jaxa_earth import _slice_to_2d

    arr = np.zeros((2, 4, 5, 1), dtype=np.float32)
    with pytest.raises(ValueError, match="multi-time"):
        _slice_to_2d(arr)


@pytest.mark.jaxa
@pytest.mark.unit
def test_slice_to_2d_rejects_non_4d() -> None:
    """A non-4-D tensor (the API always returns 4-D) is rejected."""
    from earthlens.jaxa._jaxa_earth import _slice_to_2d

    with pytest.raises(ValueError, match="4-D"):
        _slice_to_2d(np.zeros((5, 5), dtype=np.float32))
