"""Unit tests for the DEM backend, with a stubbed unsigned boto3 client."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.dem import DEM

pytestmark = [pytest.mark.dem, pytest.mark.unit]


def _install_fake_client(backend: DEM, fake) -> None:
    """Force the S3Auth to hand back the fake client without touching the network."""
    backend._auth._client = fake


class TestSearch:
    """`_search` plans one candidate product per tile the bbox covers."""

    def test_single_tile_plan(self, tmp_path: Path):
        """A sub-degree bbox plans one COG product."""
        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        products = src._search()
        assert len(products) == 1
        product = products[0]
        assert product.href == (
            "Copernicus_DSM_COG_10_N30_00_E031_00_DEM/"
            "Copernicus_DSM_COG_10_N30_00_E031_00_DEM.tif"
        )
        assert product.metadata == {
            "bucket": "copernicus-dem-30m",
            "dataset": "cop-dem-glo-30",
            "tile_lat": 30,
            "tile_lon": 31,
        }

    def test_glo90_uses_30_token(self, tmp_path: Path):
        """GLO-90 encodes the `30` resolution token in the tile name."""
        src = DEM(
            dataset="cop-dem-glo-90",
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        product = src._search()[0]
        assert "Copernicus_DSM_COG_30_" in product.href
        assert product.metadata["bucket"] == "copernicus-dem-90m"

    def test_unknown_dataset_raises(self, tmp_path: Path):
        """A bad `dataset=` key surfaces the catalog's did-you-mean."""
        with pytest.raises(ValueError, match="not in the DEM catalog"):
            DEM(
                dataset="cop-dem-glo-3",
                variables=[],
                lat_lim=[0.4, 0.6],
                lon_lim=[0.4, 0.6],
                path=tmp_path,
            )


class TestFetch:
    """`_fetch` downloads present tiles and logs ocean gaps."""

    def test_downloads_present_tile(self, tmp_path: Path, make_fake_client):
        """A single-tile bbox with a present key returns one local path."""
        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        products = src._search()
        client = make_fake_client([p.href for p in products])
        _install_fake_client(src, client)
        written = src._fetch(products)
        assert len(written) == 1
        assert written[0].name.endswith("_N30_00_E031_00_DEM.tif")
        assert written[0].exists()
        assert client.head_calls == [(products[0].metadata["bucket"], products[0].href)]

    def test_missing_tile_logged_and_skipped(
        self, tmp_path: Path, make_fake_client, caplog: pytest.LogCaptureFixture
    ):
        """An absent (ocean) tile is warned about but does not fail."""
        src = DEM(
            variables=[],
            lat_lim=[0.4, 0.6],
            lon_lim=[-150.4, -149.6],
            path=tmp_path,
        )
        products = src._search()
        assert products, "search should still plan a candidate for the ocean tile"
        client = make_fake_client(present=[])  # nothing present
        _install_fake_client(src, client)
        # loguru pipes into stderr by default; ask it to propagate to caplog.
        from loguru import logger

        handler_id = logger.add(caplog.handler, format="{message}")
        try:
            written = src._fetch(products)
        finally:
            logger.remove(handler_id)
        assert written == []
        assert client.download_calls == []
        assert any("tile absent, skipping" in message for message in caplog.messages)

    def test_multi_tile_partial_coverage(self, tmp_path: Path, make_fake_client):
        """A ragged coastline yields only the land tiles."""
        src = DEM(
            variables=[],
            lat_lim=[0.5, 0.5],
            lon_lim=[5.5, 7.5],
            path=tmp_path,
        )
        products = src._search()
        assert len(products) == 3
        # Say only the first two tiles are present.
        present = [products[0].href, products[1].href]
        client = make_fake_client(present)
        _install_fake_client(src, client)
        written = src._fetch(products)
        assert len(written) == 2

    def test_idempotent_second_call(self, tmp_path: Path, make_fake_client):
        """A second `_fetch` after a completed download re-uses the file."""
        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        products = src._search()
        client = make_fake_client([p.href for p in products])
        _install_fake_client(src, client)
        first = src._fetch(products)
        second = src._fetch(products)
        assert first == second
        # Second call must not download again.
        assert len(client.download_calls) == 1


class TestAggregateRejected:
    """A DEM request cannot be reduced across time — it has no time axis."""

    def test_download_rejects_aggregate(self, tmp_path: Path):
        """`download(aggregate=<anything>)` raises `NotImplementedError`."""
        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        with pytest.raises(NotImplementedError, match="time-invariant"):
            src.download(aggregate=object())


class TestApiComposition:
    """`_api` composes `_search` + `_fetch` via the shared C3 helper."""

    def test_api_calls_search_then_fetch(self, tmp_path: Path, make_fake_client):
        """`_api()` returns the same list `_search` + `_fetch` would."""
        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        client = make_fake_client([p.href for p in src._search()])
        _install_fake_client(src, client)
        assert len(src._api()) == 1

    def test_download_returns_list_path(self, tmp_path: Path, make_fake_client):
        """`download(progress_bar=False)` returns the fetched tile paths."""
        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        client = make_fake_client([p.href for p in src._search()])
        _install_fake_client(src, client)
        written = src.download(progress_bar=False)
        assert len(written) == 1
        assert isinstance(written[0], Path)


class TestErrorPaths:
    """Non-404 S3 errors propagate — only ocean 404s are silenced."""

    def test_head_object_other_error_propagates(
        self, tmp_path: Path, make_fake_client
    ):
        """A 500 / AccessDenied on head_object surfaces to the caller."""
        from botocore.exceptions import ClientError

        class BadClient:
            def head_object(self, **_kwargs):
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                    "HeadObject",
                )

            def download_file(self, **_kwargs):  # pragma: no cover - not reached
                raise AssertionError("download must not run when head fails")

        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        products = src._search()
        _install_fake_client(src, BadClient())
        with pytest.raises(ClientError):
            src._fetch(products)

    def test_download_error_cleans_partfile(self, tmp_path: Path):
        """A download failure removes the `.part` before propagating."""

        class FlakyClient:
            def head_object(self, **_kwargs):
                return {"ContentLength": 128}

            def download_file(self, Bucket, Key, Filename, ExtraArgs=None):
                # Simulate a transport failure that leaves a stub .part behind.
                Path(Filename).write_bytes(b"partial")
                raise RuntimeError("transport failure")

        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        products = src._search()
        _install_fake_client(src, FlakyClient())
        with pytest.raises(RuntimeError, match="transport failure"):
            src._fetch(products)
        # The .part file must be cleaned up so a retry starts fresh.
        assert not any(p.name.endswith(".part") for p in tmp_path.iterdir())


class TestClientGuardrail:
    """The `_client()` guardrail flags a not-initialised backend."""

    def test_client_guardrail_when_auth_missing(self, tmp_path: Path):
        """A backend whose `_auth` is None reports the misuse."""
        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        src._auth = None
        with pytest.raises(RuntimeError, match="not initialised"):
            src._client()


class TestNoDecodeImports:
    """`G5` — no rasterio/gdal/xarray/osgeo import anywhere in `earthlens.dem`."""

    def test_source_tree_has_no_decode_imports(self):
        """Grep the shipped package source for banned imports."""
        import earthlens.dem

        root = Path(earthlens.dem.__file__).parent
        banned = ("rasterio", "gdal", "osgeo", "xarray")
        offending: list[tuple[str, str]] = []
        for python_file in root.rglob("*.py"):
            source = python_file.read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if not stripped.startswith("import ") and not stripped.startswith(
                    "from "
                ):
                    continue
                for token in banned:
                    if token in stripped.split():
                        offending.append((python_file.name, stripped))
        assert offending == [], (
            f"earthlens.dem must not import decode libraries: {offending}"
        )
