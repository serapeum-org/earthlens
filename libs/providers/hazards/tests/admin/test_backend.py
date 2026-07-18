"""Unit + integration tests for `earthlens.admin.backend` (no network)."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from earthlens.admin import AdminBoundaries
from earthlens.admin import backend as backend_mod
from earthlens.base import RemoteProduct, SpatialExtent, TemporalExtent

from .conftest import make_fc

pytestmark = pytest.mark.admin


def _make_backend(**overrides: Any) -> AdminBoundaries:
    """Construct an AdminBoundaries backend with test defaults."""
    params: dict[str, Any] = dict(variables=["cgaz:adm0"])
    params.update(overrides)
    return AdminBoundaries(**params)


@pytest.fixture
def fake_read(monkeypatch):
    """Patch read_vector to serve a fresh 2-polygon FC, recording its URLs."""
    calls: list[str] = []

    def _read(url: str):
        calls.append(url)
        return make_fc(2)

    monkeypatch.setattr(backend_mod, "read_vector", _read)
    return calls


@pytest.fixture
def fake_geoboundaries(monkeypatch):
    """Patch geoboundaries_resolve to a deterministic URL, recording its args."""
    calls: list[tuple[str, str]] = []

    def _resolve(iso: str, adm: str, timeout: float = 60.0):
        calls.append((iso, adm))
        return f"http://x/{iso}-{adm}.geojson"

    monkeypatch.setattr(backend_mod, "geoboundaries_resolve", _resolve)
    return calls


class TestConstruction:
    """`__init__` wiring: OUTPUT_KIND, selectors, defaults, validation."""

    def test_output_kind_is_vector(self):
        """AdminBoundaries declares vector output (boundary polygons)."""
        assert AdminBoundaries.OUTPUT_KIND == "vector"

    def test_space_and_time_captured(self):
        """The sentinel bbox and the 'all' temporal sentinel land on the instance."""
        backend = _make_backend()
        assert isinstance(backend.space, SpatialExtent)
        assert isinstance(backend.time, TemporalExtent)
        assert backend.time.resolution == "all"
        assert backend.temporal_resolution == "all"

    def test_default_bbox_is_whole_earth(self):
        """Omitting lat_lim / lon_lim defaults to the whole-Earth sentinel extent."""
        backend = _make_backend()
        assert backend.space.south == -90.0 and backend.space.north == 90.0
        assert backend.space.west == -180.0 and backend.space.east == 180.0

    def test_empty_variables_rejected(self):
        """An empty variables list raises at construction."""
        with pytest.raises(ValueError, match="empty"):
            AdminBoundaries(variables=[])

    def test_invalid_file_format_rejected(self):
        """An unsupported file_format raises at construction."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            _make_backend(file_format="shp")

    def test_mapping_variables_uses_keys(self):
        """A dict variables (the facade's dataset= sugar) uses its keys as ids."""
        backend = AdminBoundaries(variables={"cgaz:adm0": []})
        assert backend._ids == ["cgaz:adm0"]

    def test_country_is_upper_cased(self):
        """A lower-case country code is normalised to upper case."""
        backend = AdminBoundaries(variables=["geoboundaries:adm1"], country="ken")
        assert backend._country == "KEN"

    def test_state_is_zero_padded(self):
        """An integer state FIPS is zero-padded to two digits."""
        backend = AdminBoundaries(variables=["tiger:tract"], state=6)
        assert backend._state == "06"

    def test_non_numeric_state_rejected(self):
        """A state name / abbreviation gives a clear FIPS error, not int() noise."""
        with pytest.raises(ValueError, match="numeric US state FIPS code"):
            AdminBoundaries(variables=["tiger:tract"], state="CA")

    def test_invalid_scale_rejected(self):
        """An out-of-set Natural Earth scale is rejected at construction."""
        with pytest.raises(ValueError, match="scale= must be one of"):
            _make_backend(variables=["natural_earth:countries"], scale="100m")

    def test_unknown_dataset_id_raises(self):
        """An unknown dataset id raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="geoboundaries:adm1"):
            AdminBoundaries(variables=["geoboundaries:adm1x"])

    def test_geoboundaries_requires_country(self):
        """A geoBoundaries dataset without country= raises a clear error."""
        with pytest.raises(ValueError, match="requires a 'country'"):
            AdminBoundaries(variables=["geoboundaries:adm1"])

    def test_tiger_tract_requires_state(self):
        """A per-state TIGER tract without state= raises a clear error."""
        with pytest.raises(ValueError, match="requires a 'state'"):
            AdminBoundaries(variables=["tiger:tract"])

    def test_no_network_on_construction(self, monkeypatch):
        """Constructing a geoBoundaries backend issues no metadata GET."""

        def _boom(*args, **kwargs):
            raise AssertionError("construction must not hit the network")

        monkeypatch.setattr(backend_mod, "geoboundaries_resolve", _boom)
        AdminBoundaries(variables=["geoboundaries:adm1"], country="KEN")


class TestResolveUrl:
    """`_resolve_url` routes each provider to the right source path."""

    def test_geoboundaries_route(self, fake_geoboundaries):
        """geoBoundaries resolves via the two-step API then /vsicurl/-wraps it."""
        backend = AdminBoundaries(variables=["geoboundaries:adm1"], country="KEN")
        url = backend._resolve_url(backend._catalog.get("geoboundaries:adm1"))
        assert url == "/vsicurl/http://x/KEN-ADM1.geojson"
        assert fake_geoboundaries == [("KEN", "ADM1")]

    def test_cgaz_route(self):
        """CGAZ routes to the seamless GeoPackage URL for its ADM level."""
        backend = _make_backend(variables=["cgaz:adm2"])
        url = backend._resolve_url(backend._catalog.get("cgaz:adm2"))
        assert url.endswith("geoBoundariesCGAZ_ADM2.gpkg")

    def test_natural_earth_route_default_scale(self):
        """Natural Earth uses the dataset's default scale when none is given."""
        backend = _make_backend(variables=["natural_earth:countries"])
        url = backend._resolve_url(backend._catalog.get("natural_earth:countries"))
        assert "110m" in url and url.endswith("ne_110m_admin_0_countries.zip")

    def test_natural_earth_route_scale_override(self):
        """An explicit scale= overrides the dataset default in the URL."""
        backend = _make_backend(variables=["natural_earth:countries"], scale="50m")
        url = backend._resolve_url(backend._catalog.get("natural_earth:countries"))
        assert "ne_50m_admin_0_countries.zip" in url

    def test_tiger_route_default_year_nationwide(self):
        """A nationwide TIGER entity uses scope 'us' and the default year."""
        backend = _make_backend(variables=["tiger:state"])
        url = backend._resolve_url(backend._catalog.get("tiger:state"))
        assert url.endswith("cb_2023_us_state_500k.zip")

    def test_tiger_route_year_override(self):
        """An explicit year= overrides the dataset default in the URL."""
        backend = _make_backend(variables=["tiger:county"], year=2022)
        url = backend._resolve_url(backend._catalog.get("tiger:county"))
        assert "GENZ2022" in url and "cb_2022_us_county_500k.zip" in url

    def test_tiger_tract_uses_state_scope(self):
        """A per-state tract writes the zero-padded FIPS as the URL scope."""
        backend = AdminBoundaries(variables=["tiger:tract"], state=6)
        url = backend._resolve_url(backend._catalog.get("tiger:tract"))
        assert url.endswith("cb_2023_06_tract_500k.zip")

    def test_unsupported_provider_raises(self):
        """An unrecognised provider on a row raises a clear ValueError."""
        backend = _make_backend()
        row = types.SimpleNamespace(provider="gadm", id="gadm:adm0")
        with pytest.raises(ValueError, match="unsupported admin provider"):
            backend._resolve_url(row)


class TestSearchFetch:
    """`_search` / `_fetch` plan and read per-dataset collections."""

    def test_search_one_product_per_dataset(self):
        """_search plans one RemoteProduct per requested dataset id."""
        backend = _make_backend(variables=["cgaz:adm0", "cgaz:adm1"])
        products = backend._search()
        assert len(products) == 2
        assert all(isinstance(p, RemoteProduct) for p in products)
        assert products[0].metadata["dataset"].id == "cgaz:adm0"

    def test_fetch_reads_via_read_vector(self, fake_read):
        """_fetch routes each product through read_vector and returns its FCs."""
        backend = _make_backend(variables=["cgaz:adm0"])
        results = backend._fetch(backend._search())
        assert len(results) == 1 and len(results[0]) == 2
        assert fake_read[0].endswith("geoBoundariesCGAZ_ADM0.gpkg")

    def test_api_composes_search_fetch(self, fake_read):
        """_api composes search + fetch into the per-dataset collection list."""
        backend = _make_backend(variables=["cgaz:adm0"])
        results = backend._api()
        assert len(results) == 1


class TestDownload:
    """`download` returns the polygons and optionally writes one file."""

    def test_returns_feature_collection(self, fake_read):
        """A download returns the in-memory FeatureCollection in EPSG:4326."""
        backend = _make_backend(variables=["cgaz:adm0"])
        fc = backend.download(progress_bar=False)
        assert len(fc) == 2
        assert fc.crs.to_epsg() == 4326

    def test_writes_file_when_path_set(self, fake_read, tmp_path: Path):
        """With a path set, the boundaries are written to one vector file."""
        backend = _make_backend(variables=["cgaz:adm0"], path=str(tmp_path))
        backend.download(progress_bar=False)
        assert (tmp_path / "admin_cgaz_adm0.gpkg").is_file()

    def test_geojson_format(self, fake_read, tmp_path: Path):
        """file_format='geojson' writes a .geojson file."""
        backend = _make_backend(
            variables=["cgaz:adm0"], path=str(tmp_path), file_format="geojson"
        )
        backend.download(progress_bar=False)
        assert (tmp_path / "admin_cgaz_adm0.geojson").is_file()

    def test_filename_embeds_selector_no_clobber(
        self, fake_read, fake_geoboundaries, tmp_path: Path
    ):
        """Two countries for one dataset write to distinct selector-stamped files."""
        AdminBoundaries(
            variables=["geoboundaries:adm1"], country="KEN", path=str(tmp_path)
        ).download(progress_bar=False)
        AdminBoundaries(
            variables=["geoboundaries:adm1"], country="UGA", path=str(tmp_path)
        ).download(progress_bar=False)
        assert (tmp_path / "admin_geoboundaries_adm1_KEN.gpkg").is_file()
        assert (tmp_path / "admin_geoboundaries_adm1_UGA.gpkg").is_file()

    def test_no_write_without_path(self, fake_read):
        """With no path, the collection is returned but nothing is written."""
        backend = _make_backend(variables=["cgaz:adm0"])
        assert backend._should_write is False
        fc = backend.download(progress_bar=False)
        assert len(fc) == 2

    def test_multi_dataset_concat(self, fake_read, tmp_path: Path):
        """Several datasets concatenate into one combined FeatureCollection."""
        backend = _make_backend(
            variables=["cgaz:adm0", "cgaz:adm1"], path=str(tmp_path)
        )
        fc = backend.download(progress_bar=False)
        assert len(fc) == 4
        assert fc.crs.to_epsg() == 4326

    def test_empty_result_writes_nothing(self, monkeypatch, tmp_path: Path):
        """An empty fetch returns an empty FC and writes no file."""
        monkeypatch.setattr(
            backend_mod, "read_vector", lambda url: backend_mod.empty_fc()
        )
        backend = _make_backend(variables=["cgaz:adm0"], path=str(tmp_path))
        fc = backend.download(progress_bar=False)
        assert len(fc) == 0
        assert list(tmp_path.glob("*.gpkg")) == []

    def test_aggregate_rejected(self):
        """A non-None aggregate is rejected (vector output)."""
        backend = _make_backend(variables=["cgaz:adm0"])
        with pytest.raises(NotImplementedError, match="vector"):
            backend.download(aggregate=object())

    def test_combine_empty_returns_empty_fc(self):
        """_combine([]) yields the schema-only empty FeatureCollection."""
        fc = AdminBoundaries._combine([])
        assert len(fc) == 0


def test_no_bare_gpd_read_file_in_source():
    """The admin source never reads a file with a bare geopandas.read_file."""
    src = Path(backend_mod.__file__).parent
    for path in src.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "gpd.read_file(" not in text
        assert "geopandas.read_file(" not in text
