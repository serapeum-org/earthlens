"""Tests for `ASF._fetch` and the `download()` facade."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from earthlens.asf import ASF
from earthlens.base import RemoteProduct


def _build_capturing_results_cls(captured_fileNames: list[list[str]]) -> type:
    """Subclass the conftest fake to record per-call fileNames into a shared list.

    Builds a `_FakeResults` subclass whose `download` appends the
    fileNames it was handed onto `captured_fileNames` and then delegates
    to the base implementation (which writes the tiny per-product
    files). Hoisted out of the test body so it does not violate the
    "no nested function defs" convention.
    """
    from .conftest import _FakeResults

    class _CapturingResults(_FakeResults):
        def download(
            self,
            path: str,
            session: Any = None,
            processes: int = 1,
            fileType: Any = None,
        ) -> None:
            captured_fileNames.append(
                [product.properties["fileName"] for product in self]
            )
            super().download(path, session, processes, fileType)

    return _CapturingResults


def _fake_remote_product(fake_asf_search_module, scene: str) -> RemoteProduct:
    """Build a `RemoteProduct` whose metadata mimics a real `_search` row."""
    from .conftest import _FakeProduct

    product = _FakeProduct(sceneName=scene)
    return RemoteProduct(
        id=scene,
        href=product.properties["url"],
        metadata={
            "product": product,
            "fileName": product.properties["fileName"],
            "perpendicularBaseline": 0.0,
            "temporalBaseline": 0,
        },
    )


@pytest.mark.asf
@pytest.mark.unit
def test_fetch_authenticates_once_and_downloads(
    fake_asf_search, fake_earthdata_auth, tmp_path: Path
) -> None:
    """`_fetch` calls `auth.session()` once and downloads every product."""
    from .conftest import _FakeProduct

    fake_asf_search.search_results = [
        _FakeProduct(sceneName="S1A_AAA_SLC"),
        _FakeProduct(sceneName="S1A_BBB_SLC"),
    ]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    remotes = backend._search()
    paths = backend._fetch(remotes)
    assert all(p.exists() for p in paths)
    assert {p.name for p in paths} == {"S1A_AAA_SLC.zip", "S1A_BBB_SLC.zip"}


@pytest.mark.asf
@pytest.mark.unit
def test_fetch_is_idempotent_skips_existing_files(
    fake_asf_search, fake_earthdata_auth, tmp_path: Path
) -> None:
    """A product already on disk is not re-downloaded."""
    from .conftest import _FakeProduct

    fake_asf_search.search_results = [
        _FakeProduct(sceneName="S1A_PRESENT"),
        _FakeProduct(sceneName="S1A_MISSING"),
    ]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    remotes = backend._search()
    # Pre-create the "present" file.
    (tmp_path / "S1A_PRESENT.zip").write_bytes(b"already-here")
    # Capture the fileNames actually passed to the SDK's download.
    captured: list[list[str]] = []
    fake_asf_search.ASFSearchResults = _build_capturing_results_cls(captured)
    paths = backend._fetch(remotes)
    assert captured == [["S1A_MISSING.zip"]]
    # Returned paths include both, in original order.
    assert paths == [
        tmp_path / "S1A_PRESENT.zip",
        tmp_path / "S1A_MISSING.zip",
    ]
    # The "present" file was not overwritten.
    assert (tmp_path / "S1A_PRESENT.zip").read_bytes() == b"already-here"


@pytest.mark.asf
@pytest.mark.unit
def test_fetch_skips_download_when_all_files_present(
    fake_asf_search, fake_earthdata_auth, tmp_path: Path
) -> None:
    """Every product already on disk → no download call at all."""
    from .conftest import _FakeProduct

    fake_asf_search.search_results = [_FakeProduct(sceneName="S1A_HAVE")]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    remotes = backend._search()
    (tmp_path / "S1A_HAVE.zip").write_bytes(b"have")
    backend._fetch(remotes)
    # ASFAuth.configure must not run if nothing was downloaded.
    assert backend._auth.is_authenticated() is False


@pytest.mark.asf
@pytest.mark.unit
def test_download_passes_aggregate_none_through(
    fake_asf_search, fake_earthdata_auth, tmp_path: Path
) -> None:
    """A plain `download()` returns the path list."""
    from .conftest import _FakeProduct

    fake_asf_search.search_results = [_FakeProduct(sceneName="S1A_X")]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    paths = backend.download()
    assert paths == [tmp_path / "S1A_X.zip"]


@pytest.mark.asf
@pytest.mark.unit
def test_download_aggregate_raises_notimplementederror(tmp_path: Path) -> None:
    """`aggregate=` is unsupported; the error message names InSAR tooling."""
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    with pytest.raises(NotImplementedError, match="InSAR"):
        backend.download(aggregate=object())


@pytest.mark.asf
@pytest.mark.unit
def test_empty_search_short_circuits_download(
    fake_asf_search, fake_earthdata_auth, tmp_path: Path
) -> None:
    """A search that matches nothing returns an empty list (no auth call)."""
    fake_asf_search.search_results = []
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    assert backend.download() == []
    assert backend._auth.is_authenticated() is False


@pytest.mark.asf
@pytest.mark.unit
def test_fetch_raises_clear_error_on_missing_filename(
    fake_asf_search, fake_earthdata_auth, tmp_path: Path
) -> None:
    """A product without a resolvable `fileName` raises `ValueError`, not `TypeError`."""
    from .conftest import _FakeProduct

    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    broken_product = _FakeProduct(sceneName="S1A_BROKEN_SLC")
    broken_product.properties["fileName"] = None
    broken_remote = RemoteProduct(
        id="S1A_BROKEN_SLC",
        metadata={"product": broken_product, "fileName": None},
    )
    with pytest.raises(ValueError, match="resolvable fileName"):
        backend._fetch([broken_remote])
    # The auth wrapper must not have been configured — failing this fast
    # means we never paid for an EDL login just to discover the product
    # was malformed.
    assert backend._auth.is_authenticated() is False
