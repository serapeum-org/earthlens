"""Offline unit tests for the OSM backend's `pbf` protocol branch.

Fakes the module-level `download_extract` / `read_pbf` on `earthlens.osm.backend`
so the branch is exercised end-to-end (region resolution, layer dispatch, bbox
mapping, ODbL warning, `aggregate=` rejection) without a network or the pyrosm /
osmium SDKs.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import box

from earthlens.osm import LicenseWarning, backend, empty_fc


class FakePbf:
    """Records `download_extract` / `read_pbf` calls and serves a preset frame."""

    def __init__(self, frame: gpd.GeoDataFrame | None = None) -> None:
        self.frame = frame
        self.download_args: tuple[Any, ...] | None = None
        self.download_http: Any = None
        self.read_kwargs: dict[str, Any] | None = None

    def download_extract(self, region_path: str, cache_dir: Any, **kwargs: Any) -> Path:
        self.download_args = (region_path, Path(cache_dir))
        self.download_http = kwargs.get("http")
        return Path("/fake/cache") / f"{region_path.replace('/', '_')}.osm.pbf"

    def read_pbf(self, path: Any, **kwargs: Any):
        self.read_kwargs = kwargs
        from earthlens.osm import to_fc

        return to_fc(self.frame) if self.frame is not None else empty_fc()


def _one_building():
    """Build a one-row WGS84 GeoDataFrame of a building polygon."""
    return gpd.GeoDataFrame(
        {"building": ["yes"]}, geometry=[box(0.0, 0.0, 1.0, 1.0)], crs="EPSG:4326"
    )


@pytest.fixture
def fake_pbf(monkeypatch):
    """Patch the backend's `download_extract` / `read_pbf` with a recorder."""
    fake = FakePbf(frame=_one_building())
    monkeypatch.setattr(backend, "download_extract", fake.download_extract)
    monkeypatch.setattr(backend, "read_pbf", fake.read_pbf)
    return fake


def _osm(tmp_path=None, **overrides):
    """Build an `OSM` for a pbf request over the Malta bbox by default."""
    kwargs: dict[str, Any] = {
        "variables": ["pbf:buildings"],
        "lat_lim": [35.8, 36.0],
        "lon_lim": [14.4, 14.6],
        "region": "malta",
        "path": str(tmp_path) if tmp_path is not None else "",
    }
    kwargs.update(overrides)
    return backend.OSM(**kwargs)


class TestPbfConstruction:
    """Constructor validation for the pbf knobs."""

    def test_bad_engine_rejected(self):
        """An unknown engine is rejected at construction."""
        with pytest.raises(ValueError, match="pyrosm.*pyosmium"):
            _osm(engine="bogus")

    def test_defaults(self, fake_pbf):
        """Engine defaults to pyrosm and cache_dir to the user cache."""
        osm = _osm()
        assert osm._engine == "pyrosm"
        assert osm._cache_dir == backend.default_pbf_cache_dir()


class TestPbfSearch:
    """Region requirement and the conditional bbox-area guard."""

    def test_region_required(self):
        """A pbf:* query without region= raises a clear error."""
        with pytest.raises(ValueError, match="needs a Geofabrik region"):
            backend.OSM(
                variables=["pbf:buildings"], lat_lim=[0, 1], lon_lim=[0, 1]
            )._api()

    def test_large_bbox_allowed_for_pbf(self, fake_pbf, tmp_path):
        """The area cap does not apply to a pbf read (whole-Earth is fine)."""
        osm = _osm(tmp_path, lat_lim=[-90, 90], lon_lim=[-180, 180])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            fc = osm.download(progress_bar=False)
        assert len(fc) == 1
        # whole-Earth default maps to no clip
        assert fake_pbf.read_kwargs["bbox"] is None

    def test_large_bbox_still_guarded_for_live(self):
        """A mixed request keeps guarding the bbox for the live protocol."""
        osm = backend.OSM(
            variables=["overpass:buildings"],
            lat_lim=[-90, 90],
            lon_lim=[-180, 180],
        )
        with pytest.raises(ValueError, match="too large"):
            osm._api()


class TestPbfFetch:
    """Region resolution, layer dispatch, bbox mapping, and output contract."""

    def test_region_key_resolved_to_path(self, fake_pbf, tmp_path):
        """A region key resolves to its Geofabrik path for the fetch."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            _osm(tmp_path).download(progress_bar=False)
        assert fake_pbf.download_args[0] == "europe/malta"

    def test_raw_region_path_passthrough(self, fake_pbf, tmp_path):
        """A raw 'continent/region' region is passed through unchanged."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            _osm(tmp_path, region="europe/andorra").download(progress_bar=False)
        assert fake_pbf.download_args[0] == "europe/andorra"

    def test_layer_and_engine_dispatch(self, fake_pbf, tmp_path):
        """The row's pyrosm_method / network_type + engine reach read_pbf."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            _osm(tmp_path, variables=["pbf:roads"], engine="pyosmium").download(
                progress_bar=False
            )
        assert fake_pbf.read_kwargs["pyrosm_method"] == "get_network"
        assert fake_pbf.read_kwargs["network_type"] == "driving"
        assert fake_pbf.read_kwargs["engine"] == "pyosmium"

    def test_bbox_mapped_wsen(self, fake_pbf, tmp_path):
        """A finite request bbox reaches read_pbf as (W, S, E, N)."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            _osm(tmp_path).download(progress_bar=False)
        assert fake_pbf.read_kwargs["bbox"] == (14.4, 35.8, 14.6, 36.0)

    def test_download_client_retries_transport_errors(self, fake_pbf, tmp_path):
        """The fetch client retries dropped-socket/timeout/disk errors."""
        import requests

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            _osm(tmp_path).download(progress_bar=False)
        retry_on = fake_pbf.download_http.retry_on_exceptions
        assert requests.ConnectionError in retry_on
        assert requests.Timeout in retry_on
        assert OSError in retry_on

    def test_odbl_warning_emitted(self, fake_pbf, tmp_path):
        """A successful pbf download warns about the ODbL licence."""
        with pytest.warns(LicenseWarning):
            _osm(tmp_path).download(progress_bar=False)

    def test_aggregate_rejected(self, fake_pbf, tmp_path):
        """A pbf request rejects aggregate= like the other protocols."""
        with pytest.raises(NotImplementedError):
            _osm(tmp_path).download(aggregate=object())
