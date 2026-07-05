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

    def test_multi_tile_partial_coverage(
        self, tmp_path: Path, make_fake_client, caplog: pytest.LogCaptureFixture
    ):
        """A ragged coastline yields exactly the land tiles; ocean tiles are logged."""
        src = DEM(
            variables=[],
            lat_lim=[0.5, 0.5],
            lon_lim=[5.5, 7.5],
            path=tmp_path,
        )
        products = src._search()
        assert len(products) == 3
        # Say only the first two tiles are present — the third surfaces
        # as a real anonymous 404 ClientError, matching the live path.
        present = [products[0].href, products[1].href]
        client = make_fake_client(present)
        _install_fake_client(src, client)
        from loguru import logger

        handler_id = logger.add(caplog.handler, format="{message}")
        try:
            written = src._fetch(products)
        finally:
            logger.remove(handler_id)
        assert len(written) == 2
        # The written list is the two land tiles, in bbox row-major order.
        assert [p.name for p in written] == [
            Path(products[0].href).name,
            Path(products[1].href).name,
        ]
        # And the ocean tile's absence surfaces as a WARNING naming its S3 URI.
        expected_uri = f"s3://{products[2].metadata['bucket']}/{products[2].href}"
        assert any(expected_uri in message for message in caplog.messages)

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
        # Second call must not re-HEAD or re-download an existing tile.
        assert len(client.download_calls) == 1
        assert len(client.head_calls) == 1


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

    def test_not_found_substring_no_longer_swallows_endpoint_error(
        self, tmp_path: Path
    ):
        """A network error carrying `Not Found` in its message must propagate.

        Copernicus DEM's ocean-tile behaviour is `Error.Code == "404"`;
        classifier must NOT match on a `"Not Found"` substring, or a DNS
        `"Host Not Found"` / `"Endpoint Not Found"` outage would be
        silently reported as a missing ocean tile.
        """
        from botocore.exceptions import ClientError

        class NoisyClient:
            def head_object(self, **_kwargs):
                raise ClientError(
                    {
                        "Error": {
                            "Code": "EndpointConnectionError",
                            "Message": "Could not connect to the endpoint URL "
                            "(address Not Found)",
                        }
                    },
                    "HeadObject",
                )

            def download_file(self, **_kwargs):  # pragma: no cover - not reached
                raise AssertionError("download must not run")

        src = DEM(
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        _install_fake_client(src, NoisyClient())
        with pytest.raises(ClientError):
            src._fetch(src._search())

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
    """`G5` — no rasterio/gdal/xarray/osgeo/cfgrib import anywhere in `earthlens.dem`."""

    def test_source_tree_has_no_decode_imports(self):
        """AST-walk the shipped package source for any banned decode import.

        Uses `ast.parse` so `from rasterio.merge import Merger`,
        `import rasterio.io`, `from osgeo.gdal import Translate`, and
        multi-line `from … import (…)` continuations are all caught — a
        naive `line.split()` grep misses them. Also flags dynamic
        `importlib.import_module("rasterio")` / `__import__("rasterio")`
        calls whose argument is a string literal.
        """
        import ast
        import earthlens.dem

        root = Path(earthlens.dem.__file__).parent
        banned = frozenset({"rasterio", "gdal", "osgeo", "xarray", "cfgrib"})
        offending: list[tuple[str, int, str]] = []
        for python_file in root.rglob("*.py"):
            source = python_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(python_file))
            relative = python_file.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        head = alias.name.split(".", 1)[0]
                        if head in banned:
                            offending.append((relative, node.lineno, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    head = (node.module or "").split(".", 1)[0]
                    if head in banned:
                        offending.append(
                            (relative, node.lineno, f"from {node.module}")
                        )
                elif isinstance(node, ast.Call):
                    target = None
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "import_module"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        target = node.args[0].value
                    elif (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "__import__"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        target = node.args[0].value
                    if target and target.split(".", 1)[0] in banned:
                        offending.append(
                            (relative, node.lineno, f"dynamic:{target}")
                        )
        assert offending == [], (
            f"earthlens.dem must not import decode libraries: {offending}"
        )
