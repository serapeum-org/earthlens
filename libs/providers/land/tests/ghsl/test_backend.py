"""Unit + integration tests for `earthlens.ghsl.backend`."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.base import close_quietly
from earthlens.ghsl import backend as backend_mod
from earthlens.ghsl.backend import GHSL, _epsg_int

from .conftest import make_tiny_tif


def _build(tmp_path: Path, variables, **kw) -> GHSL:
    """Construct a GHSL bound to a tmp output dir over a Moroccan AOI."""
    defaults = dict(
        start="2020-01-01",
        end="2020-12-31",
        lat_lim=[30.5, 31.0],
        lon_lim=[-9.0, -8.5],
        path=str(tmp_path),
    )
    defaults.update(kw)
    return GHSL(variables=variables, **defaults)


@pytest.fixture
def patched_io(monkeypatch, tmp_path):
    """Fake the per-URL download (real 4326 tif) + record merge_rasters args."""
    records: list[dict] = []

    def fake_download(url, dest_dir, *, session=None, **kw):
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        name = url.rsplit("/", 1)[-1][: -len(".zip")] + ".tif"
        target = dest / name
        if not target.exists():
            make_tiny_tif(target, epsg=4326)
        return target

    def fake_merge(
        src, dst, *, dst_crs=None, resampling=None, no_data_value=None, **kw
    ):
        records.append(
            {
                "n": len(src),
                "dst_crs": dst_crs,
                "resampling": resampling,
                "no_data_value": no_data_value,
            }
        )
        shutil.copy(src[0], dst)

    monkeypatch.setattr(backend_mod, "download_and_unzip", fake_download)
    monkeypatch.setattr("pyramids.dataset.merge.merge_rasters", fake_merge)
    return records


@pytest.mark.ghsl
class TestEpsgInt:
    """EPSG-code parsing."""

    @pytest.mark.parametrize(
        "value, expected",
        [("EPSG:4326", 4326), ("4326", 4326), (3035, 3035), ("epsg:3857", 3857)],
    )
    def test_parses(self, value, expected):
        """Common EPSG spellings parse to the integer code."""
        assert _epsg_int(value) == expected

    def test_bad_raises(self):
        """An unparseable CRS string raises."""
        with pytest.raises(ValueError, match="EPSG code"):
            _epsg_int("not-a-crs")


@pytest.mark.ghsl
class TestConstruction:
    """Constructor validation + static availability checks."""

    def test_output_kind_raster(self, tmp_path):
        """OUTPUT_KIND is raster and the bbox is captured."""
        g = _build(tmp_path, ["GHS_POP"])
        assert g.OUTPUT_KIND == "raster"
        assert g._codes == ["GHS_POP"]

    def test_empty_variables(self, tmp_path):
        """An empty variables list is rejected."""
        with pytest.raises(ValueError, match="non-empty"):
            _build(tmp_path, [])

    def test_bad_api(self, tmp_path):
        """An unknown api mode is rejected."""
        with pytest.raises(ValueError, match="api must be"):
            _build(tmp_path, ["GHS_POP"], api="ftp")

    def test_bad_tiling(self, tmp_path):
        """An unknown tiling mode is rejected."""
        with pytest.raises(ValueError, match="tiling must be"):
            _build(tmp_path, ["GHS_POP"], tiling="halfsies")

    def test_unknown_product(self, tmp_path):
        """An unknown product key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="not a known GHSL product"):
            _build(tmp_path, ["GHS_NOPE"])

    def test_bad_release(self, tmp_path):
        """A release the product lacks is rejected at construction."""
        with pytest.raises(ValueError, match="no release"):
            _build(tmp_path, ["GHS_POP"], release="R9999Z")

    def test_bad_resolution(self, tmp_path):
        """A resolution the product lacks is rejected at construction."""
        with pytest.raises(ValueError, match="no resolution"):
            _build(tmp_path, ["GHS_SMOD"], resolution="100m")

    def test_alias_resolves(self, tmp_path):
        """A friendly alias resolves to the canonical code."""
        assert _build(tmp_path, ["population"])._codes == ["GHS_POP"]


@pytest.mark.ghsl
class TestGridsAndDates:
    """`_create_grid` + `_check_input_dates`."""

    def test_create_grid(self, tmp_path):
        """The bbox is exposed via the SpatialExtent edges."""
        g = _build(tmp_path, ["GHS_POP"])
        assert (g.space.west, g.space.south, g.space.east, g.space.north) == (
            -9.0,
            30.5,
            -8.5,
            31.0,
        )

    def test_check_input_dates(self, tmp_path):
        """The window parses into a TemporalExtent with year-start cadence."""
        g = _build(tmp_path, ["GHS_POP"], start="2000-01-01", end="2010-12-31")
        assert g.time.resolution == "YS"
        assert g.time.start_date.year == 2000


@pytest.mark.ghsl
class TestEpochsFor:
    """Epoch resolution precedence."""

    def test_explicit_epochs(self, tmp_path):
        """An explicit epochs= list is used verbatim (validated)."""
        g = _build(tmp_path, ["GHS_POP"], epochs=[2000, 2020])
        assert g._epochs_for("GHS_POP") == [2000, 2020]

    def test_single_epoch(self, tmp_path):
        """A single epoch= becomes a one-element list."""
        g = _build(tmp_path, ["GHS_POP"], epoch=1990)
        assert g._epochs_for("GHS_POP") == [1990]

    def test_derive_from_window(self, tmp_path):
        """The date window selects the catalog epochs in range."""
        g = _build(tmp_path, ["GHS_POP"], start="2000-01-01", end="2010-12-31")
        assert g._epochs_for("GHS_POP") == [2000, 2005, 2010]

    def test_narrow_window_snaps(self, tmp_path):
        """A sub-step window snaps to the nearest catalog epoch."""
        g = _build(tmp_path, ["GHS_POP"], start="2019-01-01", end="2021-12-31")
        assert g._epochs_for("GHS_POP") == [2020]

    def test_unknown_explicit_epoch(self, tmp_path):
        """An explicit epoch the product lacks raises."""
        g = _build(tmp_path, ["GHS_POP"], epochs=[1973])
        with pytest.raises(ValueError, match="no epoch"):
            g._epochs_for("GHS_POP")


@pytest.mark.ghsl
class TestUrlsForAndSearch:
    """URL routing + the search plan."""

    def test_tiled_product(self, tmp_path):
        """A 100 m product over a land AOI returns per-tile URLs."""
        g = _build(tmp_path, ["GHS_POP"])
        urls = g._urls_for("GHS_POP", 2020)
        assert urls and all("/tiles/" in u for u in urls)

    def test_tiling_global_forces_whole_globe(self, tmp_path):
        """tiling='global' returns the single whole-globe URL."""
        g = _build(tmp_path, ["GHS_POP"], tiling="global")
        urls = g._urls_for("GHS_POP", 2020)
        assert len(urls) == 1 and "/tiles/" not in urls[0]

    def test_coarse_whole_globe(self, tmp_path):
        """A 1 km coarse product resolves to one whole-globe URL."""
        g = _build(tmp_path, ["GHS_SMOD"])
        urls = g._urls_for("GHS_SMOD", 2020)
        assert len(urls) == 1 and "/tiles/" not in urls[0]

    def test_smod_1km_pins_version_v1(self, tmp_path):
        """SMOD 1 km (Mollweide) still resolves to the V1-0 release."""
        g = _build(tmp_path, ["GHS_SMOD"])
        (url,) = g._urls_for("GHS_SMOD", 2020)
        assert "/V1-0/" in url and url.endswith("_V1_0.zip")

    def test_smod_30ss_pins_version_v2(self, tmp_path):
        """SMOD 30ss (WGS84) follows the V2-0 release JRC dropped V1-0 for."""
        g = _build(tmp_path, ["GHS_SMOD"], resolution="30ss")
        (url,) = g._urls_for("GHS_SMOD", 2020)
        assert "/V2-0/" in url and url.endswith("_V2_0.zip")

    def test_ocean_aoi_raises(self, tmp_path):
        """A tiled product over an ocean AOI raises a clear error."""
        g = _build(tmp_path, ["GHS_POP"], lat_lim=[-40, -39.8], lon_lim=[-140, -139.8])
        with pytest.raises(ValueError, match="no GHSL land tiles"):
            g._urls_for("GHS_POP", 2020)

    def test_search_multi_epoch(self, tmp_path):
        """_search emits one raster product per (product, epoch)."""
        g = _build(tmp_path, ["GHS_POP"], start="2000-01-01", end="2010-12-31")
        plan = g._search()
        assert [p.id for p in plan] == [
            "GHS_POP_E2000",
            "GHS_POP_E2005",
            "GHS_POP_E2010",
        ]
        assert plan[0].metadata["kind"] == "raster"

    def test_search_tabular_flag(self, tmp_path):
        """A tabular product is flagged and carries no urls."""
        g = _build(tmp_path, ["GHS_DUC"])
        plan = g._search()
        assert plan[0].metadata["kind"] == "tabular"
        assert "urls" not in plan[0].metadata


@pytest.mark.ghsl
class TestLocaliseAndFetch:
    """The download → mosaic/reproject/crop pipeline (real pyramids crop)."""

    def test_reproject_path_for_metric_source(self, tmp_path, patched_io):
        """A 100 m (54009 source) request reprojects to 4326 with bilinear."""
        g = _build(tmp_path, ["GHS_POP"])
        out = g.download(progress_bar=False)
        assert len(out) == 1 and out[0].exists()
        assert patched_io[-1]["dst_crs"] == 4326
        assert patched_io[-1]["resampling"] == "bilinear"

    def test_native_path_for_wgs84_source(self, tmp_path, patched_io):
        """A 3ss (4326 source) request to 4326 skips reprojection."""
        g = _build(tmp_path, ["GHS_POP"], resolution="3ss")
        g.download(progress_bar=False)
        assert patched_io[-1]["dst_crs"] is None

    def test_categorical_uses_nearest_and_writes_legend(self, tmp_path, patched_io):
        """A categorical product uses NN resampling + writes a legend sidecar."""
        g = _build(tmp_path, ["GHS_SMOD"], resolution="30ss")
        out = g.download(progress_bar=False)
        assert patched_io[-1]["resampling"] == "nearest neighbor"
        assert out[0].with_suffix(".legend.json").exists()

    def test_stac_branch_raises(self, tmp_path):
        """The api='stac' fetch branch raises the documented error."""
        g = _build(tmp_path, ["GHS_POP"], api="stac")
        with pytest.raises(ValueError, match="api='stac' is unavailable"):
            g.download(progress_bar=False)


@pytest.mark.ghsl
class TestAggregate:
    """Multi-epoch reduction + categorical guard."""

    def test_aggregate_mean_across_epochs(self, tmp_path, patched_io):
        """aggregate= reduces the per-epoch stack into one window raster."""
        g = _build(tmp_path, ["GHS_POP"], start="2015-01-01", end="2020-12-31")
        out = g.download(
            progress_bar=False, aggregate=AggregationConfig(freq="100YS", op="mean")
        )
        assert len(out) == 1 and out[0].name.startswith("GHS_POP_mean_")

    def test_aggregate_categorical_rejected(self, tmp_path):
        """aggregate= on a categorical product is rejected up front."""
        g = _build(tmp_path, ["GHS_SMOD"], resolution="30ss")
        with pytest.raises(ValueError, match="cannot aggregate class codes"):
            g.download(
                progress_bar=False, aggregate=AggregationConfig(freq="100YS", op="mean")
            )


@pytest.mark.ghsl
class TestTabularFetch:
    """The DUC / WUP-statistics side-table route."""

    def test_fetch_duc(self, tmp_path, monkeypatch):
        """_fetch_duc discovers the latest version + zip and extracts it."""
        extracted: dict = {}

        def fake_extract(url, dest_dir, **kw):
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            (Path(dest_dir) / "duc.csv").write_text("a,b\n", encoding="utf-8")
            extracted["url"] = url
            return [Path(dest_dir) / "duc.csv"]

        monkeypatch.setattr(backend_mod, "latest_version_dir", lambda url: "V2-0")
        monkeypatch.setattr(
            backend_mod,
            "list_remote_dir",
            lambda url: ["GHS_DUC_MT_GLOBE_R2023A_V2_0.zip"],
        )
        monkeypatch.setattr(backend_mod, "download_and_extract", fake_extract)
        g = _build(tmp_path, ["GHS_DUC"])
        out = g.download(progress_bar=False)
        assert out[0].name == "GHS_DUC" and out[0].is_dir()
        assert extracted["url"].endswith("V2-0/GHS_DUC_MT_GLOBE_R2023A_V2_0.zip")

    def test_fetch_duc_no_zip_raises(self, tmp_path, monkeypatch):
        """_fetch_duc raises when the version dir has no .zip."""
        monkeypatch.setattr(backend_mod, "latest_version_dir", lambda url: "V2-0")
        monkeypatch.setattr(
            backend_mod, "list_remote_dir", lambda url: ["copyright.txt"]
        )
        g = _build(tmp_path, ["GHS_DUC"])
        with pytest.raises(ValueError, match="no .zip table"):
            g.download(progress_bar=False)


@pytest.mark.ghsl
class TestInternals:
    """Small module-level helpers."""

    def test_close_dataset_calls_close(self):
        """close_quietly calls .close when present, swallowing errors."""
        calls = {"n": 0}

        class _D:
            def close(self):
                calls["n"] += 1

        close_quietly(_D())
        close_quietly(object())
        assert calls["n"] == 1

    def test_first_band(self, tmp_path):
        """_first_band returns band 0 for 3-D, the array for 2-D."""
        g = _build(tmp_path, ["GHS_POP"])
        a2 = np.zeros((3, 3))
        a3 = np.stack([np.ones((3, 3)), np.zeros((3, 3))])
        assert g._first_band(a2).shape == (3, 3)
        assert g._first_band(a3).tolist() == np.ones((3, 3)).tolist()

    def test_write_legend_sidecar(self, tmp_path):
        """The legend sidecar serialises the class-code → label map."""
        import json

        g = _build(tmp_path, ["GHS_SMOD"], resolution="30ss")
        target = tmp_path / "x.tif"
        target.write_bytes(b"")
        g._write_legend_sidecar(target, "GHS_SMOD")
        data = json.loads((tmp_path / "x.legend.json").read_text())
        assert data["30"] == "Urban Centre"


@pytest.mark.ghsl
class TestReviewFixes:
    """New branches added by the PR-review fixes (M1/L1/L2/L4/M3/L5)."""

    def test_multi_tile_uses_parallel_download(self, tmp_path, patched_io, monkeypatch):
        """A multi-tile product downloads every tile and mosaics them (L1)."""
        g = _build(tmp_path, ["GHS_POP"])
        monkeypatch.setattr(
            g,
            "_urls_for",
            lambda code, epoch: [
                "https://x/GHS_POP_E2020_GLOBE_R2023A_54009_100_V1_0_R6_C18.zip",
                "https://x/GHS_POP_E2020_GLOBE_R2023A_54009_100_V1_0_R6_C19.zip",
            ],
        )
        out = g.download(progress_bar=False)
        assert len(out) == 1, f"expected one mosaicked output, got {out}"
        assert patched_io[-1]["n"] == 2, "both tiles should reach merge_rasters"

    def test_merge_inherits_the_source_no_data(self, tmp_path, patched_io, monkeypatch):
        """JRC's -200 sentinel reaches merge_rasters, not its 0 default."""
        g = _build(tmp_path, ["GHS_POP"])
        monkeypatch.setattr(
            g,
            "_urls_for",
            lambda code, epoch: [
                "https://x/GHS_POP_E2020_GLOBE_R2023A_54009_100_V1_0_R6_C18.zip",
                "https://x/GHS_POP_E2020_GLOBE_R2023A_54009_100_V1_0_R6_C19.zip",
            ],
        )
        g.download(progress_bar=False)
        assert patched_io[-1]["no_data_value"] == -200.0

    def test_localise_idempotent_skips_remerge(self, tmp_path, patched_io):
        """A repeated identical download reuses the output without re-merging (L4)."""
        _build(tmp_path, ["GHS_POP"]).download(progress_bar=False)
        merges = len(patched_io)
        _build(tmp_path, ["GHS_POP"]).download(progress_bar=False)
        assert len(patched_io) == merges, (
            "second download must not re-run merge_rasters"
        )

    def test_aggregate_honors_out_dir_and_skipna(self, tmp_path, patched_io):
        """aggregate= writes under config.out_dir and respects skipna (L2)."""
        g = _build(tmp_path, ["GHS_POP"], start="2015-01-01", end="2020-12-31")
        odir = tmp_path / "agg"
        out = g.download(
            progress_bar=False,
            aggregate=AggregationConfig(
                freq="100YS", op="mean", out_dir=str(odir), skipna=False
            ),
        )
        assert out and all(p.parent == odir for p in out), (
            f"outputs not under {odir}: {out}"
        )

    def test_aggregate_passes_tabular_through(self, tmp_path, patched_io, monkeypatch):
        """A mixed raster+tabular aggregate keeps the tabular output (M3)."""
        monkeypatch.setattr(backend_mod, "latest_version_dir", lambda url: "V2-0")
        monkeypatch.setattr(
            backend_mod,
            "list_remote_dir",
            lambda url: ["GHS_DUC_MT_GLOBE_R2023A_V2_0.zip"],
        )

        def fake_extract(url, dest_dir, **kw):
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            (Path(dest_dir) / "duc.csv").write_text("a\n", encoding="utf-8")
            return [Path(dest_dir) / "duc.csv"]

        monkeypatch.setattr(backend_mod, "download_and_extract", fake_extract)
        g = _build(
            tmp_path, ["GHS_POP", "GHS_DUC"], start="2015-01-01", end="2020-12-31"
        )
        out = g.download(
            progress_bar=False, aggregate=AggregationConfig(freq="100YS", op="mean")
        )
        names = [p.name for p in out]
        assert "GHS_DUC" in names, f"tabular output dropped: {names}"
        assert any(n.startswith("GHS_POP_mean_") for n in names), (
            f"missing aggregate: {names}"
        )

    def test_aggregate_mismatched_grids_raise(self, tmp_path):
        """Per-epoch grids that differ in shape raise a clear error (M1)."""
        import numpy as np

        g = _build(tmp_path, ["GHS_POP"])
        t1 = make_tiny_tif(
            tmp_path / "e1.tif", epsg=4326, values=np.zeros((4, 4), "float32")
        )
        t2 = make_tiny_tif(
            tmp_path / "e2.tif",
            epsg=4326,
            geo=(-9.0, 0.25, 0.0, 31.0, 0.0, -0.25),
            values=np.zeros((3, 3), "float32"),
        )
        rps = [
            backend_mod.RemoteProduct(
                id=f"GHS_POP_E{e}",
                metadata={
                    "product": "GHS_POP",
                    "epoch": e,
                    "resolution": "100m",
                    "categorical": False,
                    "kind": "raster",
                },
            )
            for e in (2000, 2020)
        ]
        with pytest.raises(ValueError, match="differ in shape"):
            g._aggregate_epochs(
                rps, [t1, t2], AggregationConfig(freq="100YS", op="mean")
            )

    def test_fetch_duc_multiple_zips_picks_sorted_first(self, tmp_path, monkeypatch):
        """Multiple table zips -> the sorted-first is downloaded with a warning (L5)."""
        monkeypatch.setattr(backend_mod, "latest_version_dir", lambda url: "V2-0")
        monkeypatch.setattr(
            backend_mod, "list_remote_dir", lambda url: ["b_V2_0.zip", "a_V2_0.zip"]
        )
        captured: dict = {}

        def fake_extract(url, dest_dir, **kw):
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            captured["url"] = url
            return []

        monkeypatch.setattr(backend_mod, "download_and_extract", fake_extract)
        _build(tmp_path, ["GHS_DUC"]).download(progress_bar=False)
        assert captured["url"].endswith("a_V2_0.zip"), (
            f"expected sorted-first zip, got {captured}"
        )

    def test_close_dataset_swallows_close_error(self):
        """close_quietly never raises when close() fails."""

        class _Boom:
            def close(self):
                raise RuntimeError("boom")

        close_quietly(_Boom())


@pytest.mark.ghsl
class TestLegacyRegionalBackend:
    """Backend wiring for the legacy R2022A nested releases."""

    def test_nested_r2022a_urls(self, tmp_path):
        """A R2022A built-up request builds the nested double-dir URL."""
        g = _build(tmp_path, ["GHS_BUILT_S"], release="R2022A", resolution="1km")
        urls = g._urls_for("GHS_BUILT_S", 2020)
        assert len(urls) == 1
        assert "/GHS_BUILT_S_GLOBE_R2022A/GHS_BUILT_S_GLOBE_R2022A/" in urls[0]

    def test_r2022a_epoch_snaps_within_range(self, tmp_path):
        """R2022A POP over 2000-2020 yields its in-range epochs (no 2025/2030)."""
        g = _build(
            tmp_path,
            ["GHS_POP"],
            release="R2022A",
            start="2000-01-01",
            end="2020-12-31",
        )
        assert g._epochs_for("GHS_POP") == [2000, 2005, 2010, 2015, 2020]


@pytest.mark.ghsl
class TestReleaseResolution:
    """`_release_for` — requested release, single-release fallback, mixed."""

    def test_uses_requested_release_when_present(self, tmp_path):
        """The requested release is used when the product offers it."""
        g = _build(tmp_path, ["GHS_POP"], release="R2022A")
        assert g._release_for("GHS_POP") == "R2022A"

    def test_single_release_fallback(self, tmp_path):
        """A single-release product falls back from the default release."""
        g = _build(tmp_path, ["GHS_LAND"], start="2018-01-01", end="2018-12-31")
        assert g._release_for("GHS_LAND") == "R2022A"

    def test_mixed_release_request(self, tmp_path):
        """One request resolves each product to its own release."""
        g = _build(tmp_path, ["GHS_POP", "GHS_FUA_UCDB2015"])
        assert g._release_for("GHS_POP") == "R2023A"
        assert g._release_for("GHS_FUA_UCDB2015") == "R2019A"

    def test_multi_release_mismatch_raises(self, tmp_path):
        """A product with several releases, none requested, raises."""
        with pytest.raises(ValueError, match="no release"):
            _build(tmp_path, ["GHS_POP"], release="R9999Z")

    def test_explicit_unavailable_release_raises_no_silent_fallback(self, tmp_path):
        """An explicit release a single-release product lacks raises (typo guard)."""
        with pytest.raises(ValueError, match="no release"):
            _build(
                tmp_path,
                ["GHS_LAND"],
                release="R2099Z",
                start="2018-01-01",
                end="2018-12-31",
            )

    def test_auto_prefers_default_release(self, tmp_path):
        """release=None resolves to R2023A when the product offers it."""
        g = _build(tmp_path, ["GHS_POP"])
        assert g._release_for("GHS_POP") == "R2023A"

    def test_resolution_is_memoized(self, tmp_path):
        """_release_for caches its result per code."""
        g = _build(tmp_path, ["GHS_LAND"], start="2018-01-01", end="2018-12-31")
        for _ in range(3):
            g._release_for("GHS_LAND")
        assert g._release_cache == {"GHS_LAND": "R2022A"}

    def test_ambiguous_multi_release_without_default_raises(self, tmp_path):
        """A multi-release product lacking R2023A with no explicit release raises."""
        from earthlens.ghsl.catalog import Availability, Catalog, Product

        fake = Catalog(
            datasets={
                "GHS_X": Product(
                    code="GHS_X",
                    default_resolution="1km",
                    releases={
                        "R2022A": [Availability(epochs=[2020], resolutions=["1km"])],
                        "R2025A": [Availability(epochs=[2020], resolutions=["1km"])],
                    },
                )
            }
        )
        with pytest.raises(ValueError, match="none of them the default"):
            GHSL(
                variables=["GHS_X"],
                catalog=fake,
                start="2020-01-01",
                end="2020-12-31",
                lat_lim=[0, 1],
                lon_lim=[0, 1],
                path=str(tmp_path),
            )


@pytest.mark.ghsl
class TestLegendlessCategorical:
    """A categorical product with no legend (GHS_BUILT_C_VEG) — NN, no sidecar."""

    def test_veg_uses_nn_and_writes_no_sidecar(self, tmp_path, patched_io):
        """VEG reprojects with nearest-neighbour and writes no legend sidecar."""
        g = _build(tmp_path, ["GHS_BUILT_C_VEG"], start="2018-01-01", end="2018-12-31")
        out = g.download(progress_bar=False)
        assert patched_io[-1]["resampling"] == "nearest neighbor"
        assert out and not out[0].with_suffix(".legend.json").exists()
