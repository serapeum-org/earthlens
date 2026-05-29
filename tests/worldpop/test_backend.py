"""Tests for the WorldPop backend: construction, search, fetch, localise, tables."""

from __future__ import annotations

import sys

import pandas as pd
import pytest
import requests
from pyramids.dataset import Dataset

from earthlens.aggregate import AggregationConfig
from earthlens.worldpop import WorldPop
from tests.worldpop.conftest import _FakeResponse, age_records, pop_records

pytestmark = pytest.mark.worldpop


def test_empty_variables_raises(wp_kwargs):
    """An empty variables list is rejected."""
    with pytest.raises(ValueError, match="non-empty"):
        WorldPop(**wp_kwargs(variables=[]))


@pytest.mark.parametrize(
    "kw, match",
    [
        ({"api": "bogus"}, "api must be one of"),
        ({"resolution": "5m"}, "resolution must be one of"),
        ({"scope": "planet"}, "scope must be one of"),
        ({"level": "global"}, "level must be one of"),
        ({"generation": "R9999"}, "generation must be one of"),
    ],
)
def test_construct_validates_selectors(wp_kwargs, kw, match):
    """Malformed selector kwargs raise clear ValueErrors."""
    with pytest.raises(ValueError, match=match):
        WorldPop(**wp_kwargs(**kw))


def test_unknown_product_raises(wp_kwargs):
    """An unknown product alias is rejected at construction."""
    with pytest.raises(ValueError, match="not a known WorldPop product"):
        WorldPop(**wp_kwargs(variables=["nope"]))


@pytest.mark.parametrize("kw", [{"variables": ["future_pop"]}, {"variables": ["dependency_ratios"]}])
def test_archive_products_rejected(wp_kwargs, kw):
    """Archive-distributed products (zip / 7z) fail fast at construction."""
    with pytest.raises(NotImplementedError, match="archives"):
        WorldPop(**wp_kwargs(**kw))


def test_global_scope_constructs(wp_kwargs):
    """A global-mosaic request constructs and resolves the global sub-alias."""
    backend = WorldPop(**wp_kwargs(resolution="1km", scope="global"))
    assert backend._subalias_ids == {"pop": "wpgp1km"}


def test_impossible_combo_raises(wp_kwargs):
    """An unavailable selector tuple is rejected at construction."""
    with pytest.raises(ValueError, match="has no variant"):
        WorldPop(**wp_kwargs(constrained=True, resolution="1km", scope="countries"))


def test_aoi_string_resolves_to_iso3(wp_kwargs):
    """An explicit ISO3 string becomes the iso3 set."""
    backend = WorldPop(**wp_kwargs(aoi="ken"))
    assert backend._iso3s == ["KEN"]


def test_aoi_bbox_list_resolves(wp_kwargs):
    """A [w,s,e,n] bbox aoi resolves to the intersecting countries."""
    backend = WorldPop(**wp_kwargs(aoi=[34.0, -1.0, 35.0, 1.0]))
    assert "KEN" in backend._iso3s


def test_aoi_none_uses_bbox(wp_kwargs):
    """aoi=None derives the iso3 set from lat_lim / lon_lim."""
    backend = WorldPop(**wp_kwargs(aoi=None))
    assert "KEN" in backend._iso3s


def test_aoi_list_of_iso3(wp_kwargs):
    """A list of ISO3 strings is normalised and de-duplicated."""
    backend = WorldPop(**wp_kwargs(aoi=["ken", "UGA", "ken"]))
    assert backend._iso3s == ["KEN", "UGA"]


def test_aoi_geodataframe_like(wp_kwargs):
    """An object exposing total_bounds is treated as a GeoDataFrame AOI."""

    class _Frame:
        total_bounds = (34.0, -1.0, 35.0, 1.0)

    backend = WorldPop(**wp_kwargs(aoi=_Frame()))
    assert "KEN" in backend._iso3s


def test_years_from_window(wp_kwargs):
    """The year list comes from the start/end window when years/year unset."""
    backend = WorldPop(**wp_kwargs(start="2018", end="2020"))
    assert backend._years() == [2018, 2019, 2020]


def test_years_explicit_wins(wp_kwargs):
    """An explicit years= overrides the window."""
    backend = WorldPop(**wp_kwargs(years=[2000, 2020]))
    assert backend._years() == [2000, 2020]


def test_year_singular(wp_kwargs):
    """A singular year= selects exactly that year."""
    backend = WorldPop(**wp_kwargs(year=2015))
    assert backend._years() == [2015]


def _patch_global_http(monkeypatch, tiny_tif_bytes, files):
    """Patch requests.get to serve a global listing + `?id=` detail + tiny tifs."""

    def fake_get(url, params=None, timeout=None):
        if url.endswith(".tif"):
            return _FakeResponse(content=tiny_tif_bytes)
        if params and "id" in params:
            return _FakeResponse(json_data={"data": {"id": "1", "files": files}})
        if "/rest/data/" in url:
            return _FakeResponse(json_data={"data": [{"id": "1", "popyear": "2000"}]})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)


def test_global_search_plans_global_item(wp_kwargs, monkeypatch, tiny_tif_bytes):
    """_search plans a global item (iso3='global') for a global-scope request."""
    _patch_global_http(monkeypatch, tiny_tif_bytes, ["https://x/ppp_2000_1km_Aggregated.tif"])
    backend = WorldPop(**wp_kwargs(year=2000, resolution="1km", scope="global"))
    plan = backend._search()
    assert len(plan) == 1
    assert plan[0].metadata["iso3"] == "global"
    assert plan[0].metadata["product"] == "pop"


def test_global_download_crops_to_aoi(wp_kwargs, monkeypatch, tiny_tif_bytes):
    """A global-mosaic download crops the whole-world file to the AOI bbox."""
    _patch_global_http(monkeypatch, tiny_tif_bytes, ["https://x/ppp_2000_1km_Aggregated.tif"])
    backend = WorldPop(**wp_kwargs(year=2000, resolution="1km", scope="global"))
    out = backend.download(progress_bar=False)
    tifs = [p for p in out if str(p).endswith(".tif")]
    assert len(tifs) == 1 and tifs[0].name == "pop_2000_1km.tif"
    assert Dataset.read_file(str(tifs[0])).epsg == 4326


def test_search_plan_pop(wp_kwargs, patch_http):
    """_search plans one product per (product, iso3, year) file for pop."""
    patch_http(pop_records())
    backend = WorldPop(**wp_kwargs(year=2020))
    plan = backend._search()
    assert len(plan) == 1
    assert plan[0].metadata == {
        "product": "pop",
        "iso3": "KEN",
        "year": 2020,
        "subalias": "wpgp",
        "demographic": False,
    }


def test_search_plan_age_has_36(wp_kwargs, patch_http):
    """_search plans 36 cohort files for an age_structures year."""
    patch_http(age_records())
    backend = WorldPop(**wp_kwargs(variables=["age_structures"], year=2020))
    plan = backend._search()
    assert len(plan) == 36


def test_fetch_pop_writes_cropped_geotiff(wp_kwargs, patch_http):
    """download() writes one AOI-cropped GeoTIFF for a pop request."""
    patch_http(pop_records())
    backend = WorldPop(**wp_kwargs(year=2020))
    out = backend.download(progress_bar=False)
    tifs = [p for p in out if str(p).endswith(".tif")]
    assert len(tifs) == 1
    assert tifs[0].name == "pop_2020_100m.tif"
    assert Dataset.read_file(str(tifs[0])).epsg == 4326


def test_fetch_reprojects_when_crs_not_4326(wp_kwargs, patch_http):
    """A non-WGS84 crs= reprojects the localised raster."""
    patch_http(pop_records())
    backend = WorldPop(**wp_kwargs(year=2020, crs="EPSG:3857"))
    out = backend.download(progress_bar=False)
    tif = next(p for p in out if str(p).endswith(".tif"))
    assert Dataset.read_file(str(tif)).epsg == 3857


def test_fetch_groups_cohorts_distinctly(wp_kwargs, patch_http):
    """age_structures writes one raster per cohort plus a table."""
    patch_http(age_records())
    backend = WorldPop(**wp_kwargs(variables=["age_structures"], year=2020))
    out = backend.download(progress_bar=False)
    tifs = [p for p in out if str(p).endswith(".tif")]
    csvs = [p for p in out if str(p).endswith(".csv")]
    assert len(tifs) == 36
    assert len(csvs) == 1
    assert {p.name for p in tifs} >= {
        "age_structures_2020_f_0_100m.tif",
        "age_structures_2020_m_80_100m.tif",
    }


def test_demographic_table_shape(wp_kwargs, patch_http):
    """The age/sex table has one row per cohort with AOI population sums."""
    patch_http(age_records())
    backend = WorldPop(**wp_kwargs(variables=["age_structures"], year=2020))
    out = backend.download(progress_bar=False)
    csv = next(p for p in out if str(p).endswith(".csv"))
    frame = pd.read_csv(csv)
    assert list(frame.columns) == ["aoi", "year", "sex", "age_low", "population"]
    assert len(frame) == 36
    assert (frame["population"] > 0).all()


def test_pop_request_no_table(wp_kwargs, patch_http):
    """A plain population request writes no demographic table."""
    patch_http(pop_records())
    backend = WorldPop(**wp_kwargs(year=2020))
    out = backend.download(progress_bar=False)
    assert not [p for p in out if str(p).endswith(".csv")]


def test_multi_year_writes_per_year(wp_kwargs, patch_http):
    """A multi-year pop request writes one raster per year."""
    patch_http(pop_records())
    backend = WorldPop(**wp_kwargs(years=[2018, 2019, 2020]))
    out = backend.download(progress_bar=False)
    assert len([p for p in out if str(p).endswith(".tif")]) == 3


def test_aggregate_reduces_across_years(wp_kwargs, patch_http):
    """aggregate= reduces the per-year rasters into one window raster."""
    patch_http(pop_records())
    backend = WorldPop(**wp_kwargs(years=[2018, 2020]))
    out = backend.download(
        progress_bar=False, aggregate=AggregationConfig(freq="100YS", op="mean")
    )
    tifs = [p for p in out if str(p).endswith(".tif")]
    assert len(tifs) == 1
    assert "_mean" in tifs[0].name


def test_404_propagates(wp_kwargs, monkeypatch):
    """A 404 on a file download surfaces as an error."""
    import requests as _rq

    from tests.worldpop.conftest import _FakeResponse
    from tests.worldpop.conftest import pop_records as _pr

    def fake_get(url, params=None, timeout=None):
        if "/rest/data/" in url:
            return _FakeResponse(json_data={"data": _pr()})
        return _FakeResponse()  # 404 for the .tif

    monkeypatch.setattr(_rq, "get", fake_get)
    backend = WorldPop(**wp_kwargs(year=2020))
    with pytest.raises(_rq.HTTPError):
        backend.download(progress_bar=False)


def test_http_get_retries_transient_then_succeeds(wp_kwargs, monkeypatch, tiny_tif_bytes):
    """A transient ConnectionError is retried with backoff, then succeeds."""
    import earthlens.worldpop.backend as backend_mod

    monkeypatch.setattr(backend_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky_get(url, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("dropped")
        return _FakeResponse(content=tiny_tif_bytes)

    monkeypatch.setattr(requests, "get", flaky_get)
    backend = WorldPop(**wp_kwargs(year=2020))
    dest = backend._http_get("https://x/ken_ppp_2020.tif", backend._raw_dir() / "r.tif")
    assert dest.exists() and calls["n"] == 3


def test_http_get_skips_existing_file(wp_kwargs, monkeypatch, tiny_tif_bytes):
    """A non-empty destination is returned without re-fetching."""
    backend = WorldPop(**wp_kwargs(year=2020))
    dest = backend._raw_dir() / "cached.tif"
    dest.write_bytes(tiny_tif_bytes)

    def boom(*_a, **_k):
        raise AssertionError("should not download an existing file")

    monkeypatch.setattr(requests, "get", boom)
    assert backend._http_get("https://x/cached.tif", dest) == dest


def test_http_get_raises_after_retries_exhausted(wp_kwargs, monkeypatch):
    """A persistent transient error raises once retries are exhausted."""
    import earthlens.worldpop.backend as backend_mod

    monkeypatch.setattr(backend_mod.time, "sleep", lambda *_: None)

    def always_drop(url, timeout=None):
        raise requests.ConnectionError("dropped")

    monkeypatch.setattr(requests, "get", always_drop)
    backend = WorldPop(**wp_kwargs(year=2020))
    with pytest.raises(requests.ConnectionError):
        backend._http_get("https://x/ken_ppp_2020.tif", backend._raw_dir() / "r.tif")


def test_api_hook_composes_search_fetch(wp_kwargs, patch_http):
    """The _api ABC hook composes _search + _fetch like download()."""
    patch_http(pop_records())
    backend = WorldPop(**wp_kwargs(year=2020))
    out = backend._api()
    assert any(str(p).endswith("pop_2020_100m.tif") for p in out)


def test_worldpoppy_already_cached_fallback(wp_kwargs, fake_worldpoppy, tiny_tif_bytes):
    """When wp_raster adds no new file, the cache is matched by the fallback."""
    (fake_worldpoppy / "ken_ppp_2020.tif").write_bytes(tiny_tif_bytes)  # pre-seed
    backend = WorldPop(**wp_kwargs(year=2020, api="worldpoppy"))
    out = backend.download(progress_bar=False)
    assert any(p.name == "pop_2020_100m.tif" for p in out)


def test_http_get_does_not_retry_http_error(wp_kwargs, monkeypatch):
    """An HTTP status error (404) is not retried — it raises immediately."""
    import earthlens.worldpop.backend as backend_mod

    monkeypatch.setattr(backend_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def not_found(url, timeout=None):
        calls["n"] += 1
        return _FakeResponse()  # raise_for_status -> HTTPError

    monkeypatch.setattr(requests, "get", not_found)
    backend = WorldPop(**wp_kwargs(year=2020))
    with pytest.raises(requests.HTTPError):
        backend._http_get("https://x/missing.tif", backend._raw_dir() / "m.tif")
    assert calls["n"] == 1


def test_worldpoppy_missing_extra_raises(wp_kwargs, monkeypatch):
    """api='worldpoppy' without the SDK raises a friendly ImportError."""
    monkeypatch.setitem(sys.modules, "worldpoppy", None)  # force import failure
    with pytest.raises(ImportError, match=r"earthlens\[worldpop\]"):
        WorldPop(**wp_kwargs(api="worldpoppy"))


def test_worldpoppy_path_reads_cache(wp_kwargs, fake_worldpoppy):
    """api='worldpoppy' reads the SDK cache GeoTIFFs and localises them."""
    backend = WorldPop(**wp_kwargs(year=2020, api="worldpoppy"))
    out = backend.download(progress_bar=False)
    tifs = [p for p in out if str(p).endswith(".tif")]
    assert len(tifs) == 1
    assert tifs[0].name == "pop_2020_100m.tif"


def test_worldpoppy_search_skips_rest(wp_kwargs, fake_worldpoppy):
    """In worldpoppy mode _search plans without hitting REST (href is None)."""
    backend = WorldPop(**wp_kwargs(year=2020, api="worldpoppy"))
    plan = backend._search()
    assert plan and all(rp.href is None for rp in plan)


def test_worldpoppy_multiproduct_attribution(wp_kwargs, monkeypatch, tiny_tif_bytes, tmp_path):
    """Two products via worldpoppy are attributed to distinct files by provenance."""
    import sys
    import types

    cache = tmp_path / "wp_cache_multi"
    cache.mkdir()

    def wp_raster(product_name, aoi, years, download_dry_run=False):
        # write a product-specific filename so a naive cohort-only match would
        # mis-assign, but the before/after snapshot attributes correctly.
        for iso3 in aoi:
            for year in years:
                (cache / f"{iso3.lower()}_{product_name}_{year}.tif").write_bytes(
                    tiny_tif_bytes
                )
        return "XARRAY"

    module = types.ModuleType("worldpoppy")
    module.wp_raster = wp_raster
    module.get_cache_dir = lambda: str(cache)
    monkeypatch.setitem(sys.modules, "worldpoppy", module)

    backend = WorldPop(
        **wp_kwargs(
            variables=["pop", "pop_density"], year=2020, resolution="1km", api="worldpoppy"
        )
    )
    out = backend.download(progress_bar=False)
    names = {p.name for p in out if str(p).endswith(".tif")}
    assert names == {"pop_2020_1km.tif", "pop_density_2020_1km.tif"}


def test_worldpoppy_unmapped_product_raises(wp_kwargs, fake_worldpoppy):
    """A product with no worldpoppy_id raises a clear error in worldpoppy mode."""
    # urban_change is country-scoped (so it passes the global guard) but carries
    # no worldpoppy_id mapping.
    backend = WorldPop(
        **wp_kwargs(variables=["urban_change"], year=2020, api="worldpoppy")
    )
    with pytest.raises(ValueError, match="no worldpoppy_id"):
        backend.download(progress_bar=False)
