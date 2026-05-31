"""Unit + integration tests for the HDX backend (faked SDK, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.hdx import HDX
from earthlens.hdx.backend import _as_filter_list

from .conftest import FakeHdx, FakeResource

pytestmark = pytest.mark.hdx


class TestAsFilterList:
    """Tests for the _as_filter_list normaliser."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, []),
            ("", []),
            ("*.csv", ["*.csv"]),
            (["*.csv", "shp"], ["*.csv", "shp"]),
            (["", "csv", ""], ["csv"]),
        ],
    )
    def test_normalise(self, value, expected):
        """A string / list / None argument normalises to a filter list."""
        assert _as_filter_list(value) == expected


class TestInit:
    """Tests for HDX construction and target resolution."""

    def test_resolves_catalog_key(self, fake_hdx: FakeHdx, tmp_path):
        """A curated key resolves to its hdx_id and default resource filter."""
        backend = HDX(variables={"kontur-population": []}, path=tmp_path)
        assert backend._targets == [("kontur-population-dataset", ["*.gpkg.gz"])]

    def test_per_call_filter_overrides_catalog_default(self, fake_hdx, tmp_path):
        """An explicit per-key filter list overrides the catalog default."""
        backend = HDX(variables={"kontur-population": ["*.csv"]}, path=tmp_path)
        assert backend._targets == [("kontur-population-dataset", ["*.csv"])]

    def test_default_bbox_is_global(self, fake_hdx: FakeHdx, tmp_path):
        """Omitted bbox defaults to the whole globe (ignored by the query)."""
        backend = HDX(variables={"kontur-population": []}, path=tmp_path)
        assert backend.space.west == -180.0 and backend.space.east == 180.0

    def test_output_kind_is_mixed(self, fake_hdx: FakeHdx, tmp_path):
        """OUTPUT_KIND is the fixed value 'mixed'."""
        backend = HDX(variables={"kontur-population": []}, path=tmp_path)
        assert backend.OUTPUT_KIND == "mixed"

    def test_escape_hatch_bypasses_catalog(self, fake_hdx: FakeHdx, tmp_path):
        """hdx_id= bypasses the catalog and carries its resource filter."""
        backend = HDX(hdx_id="arbitrary-id", resource="*.tif", path=tmp_path)
        assert backend._targets == [("arbitrary-id", ["*.tif"])]

    def test_escape_hatch_without_resource(self, fake_hdx: FakeHdx, tmp_path):
        """hdx_id= with no resource filter targets every resource."""
        backend = HDX(hdx_id="arbitrary-id", path=tmp_path)
        assert backend._targets == [("arbitrary-id", [])]

    def test_requires_variables_or_hdx_id(self, fake_hdx: FakeHdx, tmp_path):
        """Neither variables nor hdx_id raises ValueError."""
        with pytest.raises(ValueError, match="non-empty `variables`"):
            HDX(path=tmp_path)

    def test_unknown_key_raises(self, fake_hdx: FakeHdx, tmp_path):
        """An unknown catalog key raises ValueError (did-you-mean)."""
        with pytest.raises(ValueError, match="HDX catalog"):
            HDX(variables={"not-a-key": []}, path=tmp_path)

    def test_raw_hdx_id_resolves_via_index(self, fake_hdx: FakeHdx, tmp_path):
        """A raw HDX id (long-tail) is accepted through `variables=`."""
        backend = HDX(variables={"kontur-population-dataset": []}, path=tmp_path)
        assert backend._targets == [("kontur-population-dataset", [])]

    def test_check_input_dates_resolution_all(self, fake_hdx: FakeHdx, tmp_path):
        """The temporal resolution is the 'all' sentinel."""
        backend = HDX(variables={"kontur-population": []}, path=tmp_path)
        assert backend.time.resolution == "all"

    def test_invalid_date_range_raises(self, fake_hdx: FakeHdx, tmp_path):
        """An end-before-start window is rejected by the date parser."""
        with pytest.raises(ValueError):
            HDX(
                variables={"kontur-population": []},
                start="2024-12-31",
                end="2024-01-01",
                path=tmp_path,
            )


class TestSearch:
    """Tests for HDX._search (resolve + filter resources)."""

    def test_resolves_and_filters(self, fake_hdx: FakeHdx, tmp_path):
        """Only resources matching the filter become products."""
        fake_hdx.add_dataset(
            "multi",
            [
                FakeResource("a.gpkg", "Geopackage"),
                FakeResource("b.csv", "CSV"),
            ],
        )
        backend = HDX(hdx_id="multi", resource="*.gpkg", path=tmp_path)
        products = backend._search()
        assert [p.metadata["name"] for p in products] == ["a.gpkg"]
        assert products[0].id == "multi::a.gpkg"
        assert products[0].metadata["format"] == "Geopackage"

    def test_empty_filter_keeps_all(self, fake_hdx: FakeHdx, tmp_path):
        """An empty filter list downloads every resource."""
        fake_hdx.add_dataset(
            "multi",
            [FakeResource("a.gpkg", "Geopackage"), FakeResource("b.csv", "CSV")],
        )
        backend = HDX(hdx_id="multi", path=tmp_path)
        assert len(backend._search()) == 2

    def test_missing_dataset_raises(self, fake_hdx: FakeHdx, tmp_path):
        """A dataset id absent from HDX raises a clear ValueError."""
        backend = HDX(hdx_id="ghost", path=tmp_path)
        with pytest.raises(ValueError, match="not found"):
            backend._search()

    def test_records_href_from_resource_url(self, fake_hdx: FakeHdx, tmp_path):
        """Each product's href is the resource URL."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV", url="http://h/a")])
        backend = HDX(hdx_id="d", path=tmp_path)
        assert backend._search()[0].href == "http://h/a"

    def test_missing_extra_in_search_raises(
        self, fake_hdx: FakeHdx, tmp_path, monkeypatch
    ):
        """A missing SDK at search time surfaces a friendly ImportError."""
        import sys

        backend = HDX(hdx_id="d", path=tmp_path)
        for name in ("hdx", "hdx.data", "hdx.data.dataset"):
            monkeypatch.setitem(sys.modules, name, None)
        with pytest.raises(ImportError, match=r"earthlens\[hdx\]"):
            backend._search()


class TestFetch:
    """Tests for HDX._fetch (download resources to disk)."""

    def test_downloads_into_per_dataset_subdir(self, fake_hdx: FakeHdx, tmp_path):
        """Resources are downloaded into a per-dataset subdir of root_dir."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        backend = HDX(hdx_id="d", path=tmp_path)
        paths = backend._fetch(backend._search())
        assert paths == [Path(tmp_path) / "d" / "a.csv"]
        assert paths[0].exists()

    def test_same_named_resources_do_not_collide(self, fake_hdx: FakeHdx, tmp_path):
        """Two datasets with an identically-named resource keep separate files."""
        from earthlens.base import RemoteProduct

        backend = HDX(hdx_id="seed", path=tmp_path)
        products = [
            RemoteProduct(
                id=f"{hdx_id}::data.csv",
                metadata={
                    "resource": FakeResource("data.csv", "CSV"),
                    "hdx_id": hdx_id,
                    "name": "data.csv",
                    "format": "CSV",
                },
            )
            for hdx_id in ("ds-a", "ds-b")
        ]
        paths = backend._fetch(products)
        assert sorted(str(p.relative_to(tmp_path)) for p in paths) == [
            str(Path("ds-a") / "data.csv"),
            str(Path("ds-b") / "data.csv"),
        ]
        assert len({str(p) for p in paths}) == 2 and all(p.exists() for p in paths)

    def test_empty_products_returns_empty(self, fake_hdx: FakeHdx, tmp_path):
        """Fetching no products returns an empty list."""
        backend = HDX(hdx_id="d", path=tmp_path)
        assert backend._fetch([]) == []

    def test_traversal_hdx_id_stays_within_root(self, fake_hdx: FakeHdx, tmp_path):
        """A traversal-style hdx_id is reduced to its final path segment."""
        from earthlens.base import RemoteProduct

        backend = HDX(hdx_id="seed", path=tmp_path)
        product = RemoteProduct(
            id="x::a.csv",
            metadata={
                "resource": FakeResource("a.csv", "CSV"),
                "hdx_id": "../escape",
                "name": "a.csv",
                "format": "CSV",
            },
        )
        (path,) = backend._fetch([product])
        assert backend.root_dir in path.parents
        assert path == backend.root_dir / "escape" / "a.csv"


class TestDownload:
    """Tests for HDX.download (the facade-facing entry point)."""

    def test_download_composes_search_fetch(self, fake_hdx: FakeHdx, tmp_path):
        """download resolves, filters, and downloads end-to-end."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        backend = HDX(hdx_id="d", path=tmp_path)
        paths = backend.download()
        assert [p.name for p in paths] == ["a.csv"]

    def test_download_rejects_aggregate(self, fake_hdx: FakeHdx, tmp_path):
        """A non-None aggregate= is rejected despite the mixed-forwarding facade."""
        backend = HDX(variables={"kontur-population": []}, path=tmp_path)
        with pytest.raises(NotImplementedError, match="aggregate="):
            backend.download(aggregate=object())

    def test_download_empty_search_short_circuits(self, fake_hdx: FakeHdx, tmp_path):
        """A dataset with no matching resource downloads nothing."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        backend = HDX(hdx_id="d", resource="*.gpkg", path=tmp_path)
        assert backend.download() == []

    def test_progress_bar_is_a_no_op(self, fake_hdx: FakeHdx, tmp_path):
        """progress_bar is accepted for parity and does not change the result."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        backend = HDX(hdx_id="d", path=tmp_path)
        assert [p.name for p in backend.download(progress_bar=False)] == ["a.csv"]
        assert not hasattr(backend, "_show_progress")

    def test_api_via_search_fetch(self, fake_hdx: FakeHdx, tmp_path):
        """_api composes the search/fetch split."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        backend = HDX(hdx_id="d", path=tmp_path)
        assert [p.name for p in backend._api()] == ["a.csv"]

    def test_read_maps_paths_through_read_resource(
        self, fake_hdx: FakeHdx, tmp_path, monkeypatch
    ):
        """read=True reads each downloaded path via pyramids.read_resource."""
        calls = []

        def fake_read_resource(path, fmt=None):
            calls.append((Path(path).name, fmt))
            return f"obj:{Path(path).name}:{fmt}"

        monkeypatch.setattr("pyramids.read_resource", fake_read_resource, raising=False)
        fake_hdx.add_dataset(
            "d", [FakeResource("a.gpkg", "Geopackage"), FakeResource("b.csv", "CSV")]
        )
        backend = HDX(hdx_id="d", path=tmp_path)
        result = backend.download(read=True)
        assert result == ["obj:a.gpkg:Geopackage", "obj:b.csv:CSV"]
        assert calls == [("a.gpkg", "Geopackage"), ("b.csv", "CSV")]

    def test_read_empty_search_returns_empty(
        self, fake_hdx: FakeHdx, tmp_path, monkeypatch
    ):
        """read=True with no matching resource returns an empty list."""
        monkeypatch.setattr(
            "pyramids.read_resource", lambda path, fmt=None: None, raising=False
        )
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        backend = HDX(hdx_id="d", resource="*.gpkg", path=tmp_path)
        assert backend.download(read=True) == []

    def test_read_without_reader_raises_upgrade_error(
        self, fake_hdx: FakeHdx, tmp_path, monkeypatch
    ):
        """read=True with a pyramids lacking read_resource raises an upgrade hint."""
        import sys
        import types

        monkeypatch.setitem(sys.modules, "pyramids", types.ModuleType("pyramids"))
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        backend = HDX(hdx_id="d", path=tmp_path)
        with pytest.raises(NotImplementedError, match=r"pyramids-gis >= 0\.27\.0"):
            backend.download(read=True)
