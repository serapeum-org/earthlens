"""Live end-to-end tests for the Humanitarian Data Exchange backend.

Hits the real CKAN catalogue at `data.humdata.org`, which is public, so
these tests are gated only behind the `e2e` pytest marker plus network
availability — no credentials are needed. A default `pytest` invocation
skips them.

Run with:

    pixi run -e dev pytest -m "e2e and hdx" tests/hdx
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens
from earthlens.hdx import HDX

pytestmark = [pytest.mark.e2e, pytest.mark.hdx]


def _hdx_reachable() -> bool:
    """Return whether `data.humdata.org` answers on HTTPS."""
    try:
        socket.create_connection(("data.humdata.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


requires_network = pytest.mark.skipif(
    not _hdx_reachable(), reason="data.humdata.org is unreachable (offline)"
)


@requires_network
class TestHdxLiveDownload:
    """Live HDX reads/downloads (public — no credentials needed)."""

    def test_download_curated_dataset(self, tmp_path: Path):
        """A curated tabular dataset downloads a real file to disk."""
        paths = EarthLens(
            data_source="hdx",
            variables={"wfp-topline-figures": []},
            path=str(tmp_path),
        ).download(progress_bar=False)
        assert paths, "expected at least one downloaded resource"
        assert all(Path(p).exists() for p in paths), "downloaded files must exist"
        assert all(Path(p).suffix.lower() == ".csv" for p in paths), "CSV expected"

    def test_escape_hatch_arbitrary_id(self, tmp_path: Path):
        """The hdx_id= escape hatch downloads an arbitrary dataset's resource."""
        paths = HDX(
            hdx_id="wfp-topline-figures",
            resource="*.csv",
            path=str(tmp_path),
        ).download(progress_bar=False)
        assert paths and Path(paths[0]).exists()

    def test_search_resolves_curated_vector_dataset(self, tmp_path: Path):
        """A curated vector dataset resolves to GeoPackage resources."""
        backend = HDX(variables={"kontur-population": []}, path=str(tmp_path))
        products = backend._search()
        assert products, "kontur-population should expose resources"
        assert any(p.metadata["format"].lower() == "geopackage" for p in products), (
            "expected a Geopackage resource per the catalog"
        )

    def test_stage_site_constructs_without_error(self, tmp_path: Path):
        """hdx_site='stage' is accepted and constructs without raising (L4).

        Verifies the `hdx_site='stage'` path: the backend constructs (which
        runs `_initialize` → the guarded `Configuration.read()/create()`) and
        a search does not raise a *configuration* error. Two caveats keep this
        intentionally light: HDX `Configuration` is a process-global singleton,
        so when this runs after a prod test in the same process the existing
        config is reused; and staging is not guaranteed to mirror prod, so a
        `ValueError` (dataset not found) from the search is tolerated.
        """
        backend = HDX(
            variables={"wfp-topline-figures": []},
            hdx_site="stage",
            path=str(tmp_path),
        )
        assert backend._hdx_site == "stage"
        try:
            backend._search()
        except ValueError:
            # stage may not host this dataset — acceptable; config path worked.
            pass
