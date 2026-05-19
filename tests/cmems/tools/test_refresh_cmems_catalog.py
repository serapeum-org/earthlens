"""Tests for tools/cmems/refresh_cmems_catalog.py."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import refresh_cmems_catalog as rcc  # noqa: E402 (sys.path injection in conftest)
from tests.cmems.tools.conftest import (
    FakeCatalogue,
    FakeCmemsModule,
    FakeProduct,
    FakeVariable,
    make_dataset,
)


@pytest.fixture
def temp_catalog(tmp_path: Path) -> Path:
    """A minimal valid `cmems_data_catalog.yaml` under tmp_path."""
    path = tmp_path / "cmems_data_catalog.yaml"
    path.write_text(
        "available_products:\n"
        "  - OLD_A\n"
        "  - OLD_B\n"
        "\n"
        "datasets:\n"
        "  existing_ds:\n"
        "    product: PROD_X\n"
        "    title: existing dataset\n"
        "    cadence: daily\n"
        "    domain: global\n"
        "    temporal:\n"
        "      start: 2020-01-01\n"
        "      end: null\n"
        "    variables:\n"
        "      thetao:\n"
        "        units: degrees_C\n"
        "        long_name: Sea water potential temperature\n",
        encoding="utf-8",
    )
    return path


def _make_args(**overrides: object) -> object:
    """Build an argparse.Namespace stand-in with refresh defaults."""
    import argparse

    ns = argparse.Namespace(
        catalog=None,
        dry_run=False,
        with_datasets=None,
        dataset_ids=None,
        func=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class TestCmdRefresh:
    """End-to-end behaviour of `_cmd_refresh`."""

    def test_dry_run_does_not_touch_catalog(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`--dry-run` prints the block but leaves the catalog file alone."""
        fake_cm_module.describe_response = FakeCatalogue(
            [FakeProduct("LIVE_A", []), FakeProduct("LIVE_B", [])]
        )
        before = temp_catalog.read_text(encoding="utf-8")
        args = _make_args(catalog=temp_catalog, dry_run=True)
        rc = rcc._cmd_refresh(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "LIVE_A" in out
        assert "LIVE_B" in out
        assert temp_catalog.read_text(encoding="utf-8") == before

    def test_write_mode_rewrites_available_products(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
    ) -> None:
        """Non-dry write replaces `available_products:` and leaves `datasets:` intact."""
        fake_cm_module.describe_response = FakeCatalogue(
            [FakeProduct("LIVE_A", []), FakeProduct("LIVE_B", [])]
        )
        args = _make_args(catalog=temp_catalog, dry_run=False)
        rc = rcc._cmd_refresh(args)
        assert rc == 0
        text = temp_catalog.read_text(encoding="utf-8")
        assert "OLD_A" not in text
        assert "LIVE_A" in text and "LIVE_B" in text
        assert "existing_ds:" in text

    def test_describe_failure_surfaces_as_nonzero(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A toolbox-level exception during describe -> exit code 1."""

        def boom(**kwargs: object) -> None:
            raise RuntimeError("toolbox down")

        fake_cm_module.describe = boom  # type: ignore[assignment]
        args = _make_args(catalog=temp_catalog, dry_run=False)
        rc = rcc._cmd_refresh(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "toolbox down" in err

    def test_with_datasets_emits_stanzas(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
        capsys: pytest.CaptureFixture[str],
        fake_product: FakeProduct,
    ) -> None:
        """`--with-datasets PROD` walks the product and prints stanzas to stdout."""
        fake_cm_module.describe_response = FakeCatalogue([fake_product])
        fake_cm_module.describe_by_product[fake_product.product_id] = FakeCatalogue(
            [fake_product]
        )
        args = _make_args(
            catalog=temp_catalog,
            dry_run=True,
            with_datasets=[fake_product.product_id],
        )
        rc = rcc._cmd_refresh(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "# ---- paste under" in out
        assert fake_product.datasets[0].dataset_id in out
        assert "title: GLORYS12 daily mean" in out

    def test_with_datasets_continues_on_per_product_error(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
        capsys: pytest.CaptureFixture[str],
        fake_product: FakeProduct,
    ) -> None:
        """A bad product id logs and moves on; the good one still emits."""
        fake_cm_module.describe_response = FakeCatalogue([fake_product])
        fake_cm_module.describe_by_product[fake_product.product_id] = FakeCatalogue(
            [fake_product]
        )
        fake_cm_module.describe_raises["BAD_PROD"] = RuntimeError("nope")
        args = _make_args(
            catalog=temp_catalog,
            dry_run=True,
            with_datasets=["BAD_PROD", fake_product.product_id],
        )
        rc = rcc._cmd_refresh(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "BAD_PROD" in captured.err
        assert fake_product.datasets[0].dataset_id in captured.out


class TestCmdAddIds:
    """`add-ids <dataset_id>` end-to-end."""

    def test_skips_already_curated(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An id already under `datasets:` is reported as skipped and not re-fetched."""
        monkeypatch.setattr(
            "earthlens.cmems.catalog.CATALOG_PATH", temp_catalog
        )
        from earthlens.cmems.catalog import clear_catalog_cache

        clear_catalog_cache()
        args = _make_args(catalog=temp_catalog, dataset_ids=["existing_ds"])
        rc = rcc._cmd_add_ids(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "already-curated" in out
        assert "nothing to add" in out
        assert fake_cm_module.describe_calls == []

    def test_appends_fresh_stanza(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A new dataset id is appended and re-parsed cleanly."""
        monkeypatch.setattr(
            "earthlens.cmems.catalog.CATALOG_PATH", temp_catalog
        )
        from earthlens.cmems.catalog import clear_catalog_cache

        clear_catalog_cache()
        new_ds = make_dataset(
            "fresh_ds_P1D-m",
            [FakeVariable("chl", "mg m-3", "mass_concentration_of_chlorophyll")],
            dataset_name="Fresh dataset",
        )
        fake_cm_module.describe_by_dataset["fresh_ds_P1D-m"] = FakeCatalogue(
            [FakeProduct("FRESH_PROD", [new_ds])]
        )
        args = _make_args(catalog=temp_catalog, dataset_ids=["fresh_ds_P1D-m"])
        rc = rcc._cmd_add_ids(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "appended 1 stanzas" in out
        text = temp_catalog.read_text(encoding="utf-8")
        assert "fresh_ds_P1D-m:" in text
        assert "existing_ds:" in text

    def test_describe_failure_recorded_as_stderr_skip(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A `DatasetNotFound` is logged and the run exits 1 when nothing appended."""
        monkeypatch.setattr(
            "earthlens.cmems.catalog.CATALOG_PATH", temp_catalog
        )
        from earthlens.cmems.catalog import clear_catalog_cache

        clear_catalog_cache()
        args = _make_args(catalog=temp_catalog, dataset_ids=["does_not_exist"])
        rc = rcc._cmd_add_ids(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "does_not_exist" in err


class TestCmdCompact:
    """`compact` subcommand: stdin -> stdout normaliser."""

    def test_passes_through_clean_input(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clean canonical stanzas round-trip without losing content."""
        raw = "  ds-a:\n    product: P\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        args = _make_args()
        rc = rcc._cmd_compact(args)
        assert rc == 0
        assert "ds-a:" in capsys.readouterr().out

    def test_normalises_messy_input(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CRLF and scratch markers in input -> clean LF output."""
        raw = "# ---- paste ---\r\n# product: X\r\n  ds-a:\r\n    product: P\r\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        rc = rcc._cmd_compact(_make_args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "\r" not in out
        assert "# ---- paste" not in out
        assert "  ds-a:" in out


class TestMain:
    """CLI dispatch."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--help` exits 0 with a usage banner."""
        with pytest.raises(SystemExit) as exc_info:
            rcc.main(["--help"])
        assert exc_info.value.code == 0

    def test_refresh_dispatch(
        self,
        fake_cm_module: FakeCmemsModule,
        temp_catalog: Path,
    ) -> None:
        """The `refresh` subcommand routes to `_cmd_refresh`."""
        fake_cm_module.describe_response = FakeCatalogue(
            [FakeProduct("LIVE_A", [])]
        )
        rc = rcc.main(["refresh", "--dry-run", "--catalog", str(temp_catalog)])
        assert rc == 0

    def test_compact_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`compact` subcommand reads stdin and prints to stdout."""
        monkeypatch.setattr("sys.stdin", io.StringIO("  ds:\n    product: P\n"))
        rc = rcc.main(["compact"])
        assert rc == 0
        assert "ds:" in capsys.readouterr().out
