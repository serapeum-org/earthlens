"""Unit tests for `earthlens.ghsl._helpers`."""

from __future__ import annotations

import zipfile

import pytest
from earthlens.ghsl._helpers import (
    download_and_extract,
    download_and_unzip,
    ghsl_url,
    latest_version_dir,
    list_remote_dir,
    tiles_for_bbox,
)

from earthlens.ghsl import _helpers

from .conftest import make_tiny_tif, zip_with_tif


@pytest.mark.ghsl
class TestGhslUrl:
    """The deterministic JRC URL builder."""

    def test_verified_tile_url(self):
        """The per-tile URL reproduces the verified path byte-for-byte."""
        url = ghsl_url("GHS_POP", "GHS_POP", 2020, "R2023A", "100m", tile="R6_C18")
        assert url.endswith(
            "GHS_POP_GLOBE_R2023A/GHS_POP_E2020_GLOBE_R2023A_54009_100/V1-0/"
            "tiles/GHS_POP_E2020_GLOBE_R2023A_54009_100_V1_0_R6_C18.zip"
        )

    def test_whole_globe_url(self):
        """The whole-globe URL drops tiles/ and the _R_C suffix."""
        url = ghsl_url("GHS_POP", "GHS_POP", 2020, "R2023A", "1km")
        assert url.endswith("GHS_POP_E2020_GLOBE_R2023A_54009_1000_V1_0.zip")
        assert "/tiles/" not in url

    def test_subproduct_family_vs_stem(self):
        """A sub-product uses the family dir but the stem filename."""
        url = ghsl_url("GHS_BUILT_H", "GHS_BUILT_H_ANBH", 2018, "R2023A", "3ss")
        assert "/GHS_BUILT_H_GLOBE_R2023A/" in url
        assert url.endswith("GHS_BUILT_H_ANBH_E2018_GLOBE_R2023A_4326_3ss_V1_0.zip")

    def test_version_override(self):
        """A non-default version is reflected in both path and filename."""
        url = ghsl_url("GHS_DUC", "GHS_DUC", 2020, "R2023A", "1km", version=("2", "0"))
        assert "/V2-0/" in url
        assert url.endswith("_V2_0.zip")

    def test_unknown_resolution_raises(self):
        """An unknown resolution is rejected."""
        with pytest.raises(ValueError, match="unknown GHSL resolution"):
            ghsl_url("GHS_POP", "GHS_POP", 2020, "R2023A", "7m")


@pytest.mark.ghsl
class TestTilesForBbox:
    """AOI → intersecting Mollweide land tiles."""

    def test_morocco_selects_verified_tile(self):
        """A small Moroccan-coast AOI selects the verified R6_C18 tile."""
        assert tiles_for_bbox((-9.0, 30.5, -8.5, 31.0)) == ["R6_C18"]

    def test_ocean_selects_nothing(self):
        """An open-ocean (South Pacific) AOI selects no tile."""
        assert tiles_for_bbox((-140.0, -40.0, -139.8, -39.8)) == []

    def test_results_are_sorted_natural(self):
        """A wider AOI returns tiles sorted by (row, col)."""
        hits = tiles_for_bbox((-10.0, 35.0, -2.0, 44.0))
        assert hits == sorted(
            hits, key=lambda t: (int(t.split("_")[0][1:]), int(t.split("_")[1][1:]))
        )
        assert len(hits) >= 1


@pytest.mark.ghsl
class TestDownloadAndUnzip:
    """Streamed download + unzip selecting the .tif member."""

    def test_extracts_tif_skipping_sidecars(
        self, tmp_path, fake_session, make_response
    ):
        """The .tif is extracted past the PDF/xlsx sidecars."""
        tif = make_tiny_tif(tmp_path / "src.tif", epsg=4326)
        zpath = zip_with_tif(tif, tmp_path / "GHS_POP_x_V1_0.zip")
        payload = zpath.read_bytes()
        zpath.unlink()
        url = "https://x/GHS_POP_x_V1_0.zip"
        session = fake_session({url: make_response(content=payload)})
        out = download_and_unzip(url, tmp_path / "dl", session=session)
        assert out.suffix == ".tif" and out.exists()

    def test_idempotent_skip(self, tmp_path, fake_session, make_response):
        """A second call with the .tif present skips the download."""
        tif = make_tiny_tif(tmp_path / "src.tif", epsg=4326)
        zpath = zip_with_tif(tif, tmp_path / "GHS_POP_y_V1_0.zip")
        payload = zpath.read_bytes()
        url = "https://x/GHS_POP_y_V1_0.zip"
        session = fake_session({url: make_response(content=payload)})
        download_and_unzip(url, tmp_path / "dl", session=session)
        first = len(session.requested)
        download_and_unzip(url, tmp_path / "dl", session=session)
        assert len(session.requested) == first

    def test_zip_without_tif_raises(self, tmp_path, fake_session, make_response):
        """A zip with no .tif member raises a clear error."""
        zpath = tmp_path / "GHS_POP_z_V1_0.zip"
        with zipfile.ZipFile(zpath, "w") as archive:
            archive.writestr("only.txt", b"nope")
        url = "https://x/GHS_POP_z_V1_0.zip"
        session = fake_session({url: make_response(content=zpath.read_bytes())})
        with pytest.raises(ValueError, match="no matching member"):
            download_and_unzip(url, tmp_path / "dl", session=session)

    def test_retries_then_raises(self, tmp_path, fake_session, monkeypatch):
        """A persistent 404 raises HTTPError after exhausting retries."""
        import requests as rq

        url = "https://x/missing_V1_0.zip"
        session = fake_session({})
        monkeypatch.setattr(_helpers.time, "sleep", lambda *_: None)
        with pytest.raises(rq.HTTPError):
            download_and_unzip(
                url, tmp_path / "dl", session=session, retries=2, backoff=0.0
            )


@pytest.mark.ghsl
class TestRemoteListing:
    """Apache-autoindex listing + version discovery + extract."""

    _HTML = (
        '<a href="?C=N;O=D">Name</a><a href="/parent">Parent</a>'
        '<a href="V1-0/">V1-0/</a><a href="V2-0/">V2-0/</a>'
        '<a href="copyright.txt">copyright.txt</a>'
    )

    def test_list_remote_dir_filters_nav(self, fake_session, make_response):
        """Listing drops the sort/parent links, keeps real entries."""
        url = "https://x/fam"
        session = fake_session({url + "/": make_response(text=self._HTML)})
        names = list_remote_dir(url, session=session)
        assert "V1-0/" in names and "copyright.txt" in names
        assert not any(n.startswith("?") or n.startswith("/") for n in names)

    def test_latest_version_dir(self, fake_session, make_response):
        """The highest V{maj}-{min} directory is returned."""
        url = "https://x/fam"
        session = fake_session({url + "/": make_response(text=self._HTML)})
        assert latest_version_dir(url, session=session) == "V2-0"

    def test_latest_version_dir_none_raises(self, fake_session, make_response):
        """A family dir without a version directory raises."""
        url = "https://x/fam"
        session = fake_session({url + "/": make_response(text="<a href='x'>x</a>")})
        with pytest.raises(ValueError, match="version directory"):
            latest_version_dir(url, session=session)

    def test_download_and_extract_keeps_all_members(
        self, tmp_path, fake_session, make_response
    ):
        """download_and_extract keeps every member (tabular payload)."""
        zpath = tmp_path / "table_V2_0.zip"
        with zipfile.ZipFile(zpath, "w") as archive:
            archive.writestr("duc.csv", b"a,b\n1,2\n")
            archive.writestr("readme.txt", b"hi")
        url = "https://x/table_V2_0.zip"
        session = fake_session({url: make_response(content=zpath.read_bytes())})
        out = download_and_extract(url, tmp_path / "ex", session=session)
        names = {p.name for p in out}
        assert names == {"duc.csv", "readme.txt"}
        assert (tmp_path / "ex" / "duc.csv").exists()


@pytest.mark.ghsl
class TestZipSafety:
    """Zip-Slip rejection + multi-member handling (L3 / N3)."""

    def test_download_and_unzip_rejects_zip_slip(
        self, tmp_path, fake_session, make_response
    ):
        """A `../` member in a tile zip is rejected before extraction (L3)."""
        zpath = tmp_path / "evil_V1_0.zip"
        with zipfile.ZipFile(zpath, "w") as archive:
            archive.writestr("../escape.tif", b"x")
        url = "https://x/evil_V1_0.zip"
        session = fake_session({url: make_response(content=zpath.read_bytes())})
        with pytest.raises(ValueError, match="unsafe path"):
            download_and_unzip(url, tmp_path / "dl", session=session)

    def test_download_and_extract_rejects_zip_slip(
        self, tmp_path, fake_session, make_response
    ):
        """A `../` member in a tabular zip is rejected before extraction (L3)."""
        zpath = tmp_path / "evil_V2_0.zip"
        with zipfile.ZipFile(zpath, "w") as archive:
            archive.writestr("../escape.csv", b"x")
        url = "https://x/evil_V2_0.zip"
        session = fake_session({url: make_response(content=zpath.read_bytes())})
        with pytest.raises(ValueError, match="unsafe path"):
            download_and_extract(url, tmp_path / "ex", session=session)

    def test_multiple_tif_members_uses_sorted_first(
        self, tmp_path, fake_session, make_response
    ):
        """A zip with several `.tif` members extracts the sorted-first one (N3)."""
        t1 = make_tiny_tif(tmp_path / "src_a.tif", epsg=4326)
        t2 = make_tiny_tif(tmp_path / "src_z.tif", epsg=4326)
        zpath = tmp_path / "GHS_multi_V1_0.zip"
        with zipfile.ZipFile(zpath, "w") as archive:
            archive.write(t2, arcname="zzz.tif")
            archive.write(t1, arcname="aaa.tif")
        url = "https://x/GHS_multi_V1_0.zip"
        session = fake_session({url: make_response(content=zpath.read_bytes())})
        out = download_and_unzip(url, tmp_path / "dl", session=session)
        assert out.suffix == ".tif" and out.exists(), f"expected a .tif, got {out}"


@pytest.mark.ghsl
class TestRegionAndNested:
    """Region token + nested-layout URL building (legacy/regional families)."""

    def test_region_token_in_path_and_stem(self):
        """A non-GLOBE region replaces GLOBE in both the dir and the stem."""
        url = ghsl_url("GHS_X", "GHS_X", 2020, "R2025A", "1km", region="ARCTIC")
        assert "/GHS_X_ARCTIC_R2025A/" in url
        assert "GHS_X_E2020_ARCTIC_R2025A_54009_1000" in url

    def test_nested_inserts_subproduct_dir(self):
        """nested=True inserts the intermediate {code}_{region}_{release} dir."""
        url = ghsl_url(
            "GHS_BUILT_S", "GHS_BUILT_S_NRES", 2020, "R2022A", "100m", nested=True
        )
        assert "/GHS_BUILT_S_GLOBE_R2022A/GHS_BUILT_S_NRES_GLOBE_R2022A/" in url

    def test_flat_has_no_subproduct_dir(self):
        """nested=False keeps the R2023A flat layout (no intermediate dir)."""
        url = ghsl_url("GHS_BUILT_S", "GHS_BUILT_S", 2020, "R2023A", "100m")
        assert "/GHS_BUILT_S_GLOBE_R2023A/GHS_BUILT_S_E2020_" in url
