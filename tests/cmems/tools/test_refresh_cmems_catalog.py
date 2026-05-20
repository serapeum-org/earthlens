"""Tests for tools/cmems/refresh_cmems_catalog.py (multi-file layout)."""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest
import yaml

import refresh_cmems_catalog as rcc  # noqa: E402 (sys.path injection in conftest)
from tests.cmems.tools.conftest import (
    FakeCatalogue,
    FakeCmemsModule,
    FakeProduct,
    FakeVariable,
    make_dataset,
)


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    """A minimal valid `catalog/` directory: _index.yaml + one domain file."""
    cat = tmp_path / "catalog"
    cat.mkdir()
    (cat / "_index.yaml").write_text(
        "# index header\n"
        "available_datasets:\n"
        "  - existing_ds\n"
        "  - OLD_EXTRA_ds\n",
        encoding="utf-8",
    )
    (cat / "global-physics.yaml").write_text(
        "datasets:\n"
        "  existing_ds:\n"
        "    product: GLOBAL_MULTIYEAR_PHY_001_030\n"
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
    return cat


def _make_args(**overrides: object) -> argparse.Namespace:
    """Build an argparse.Namespace stand-in with all known refresh/add fields."""
    ns = argparse.Namespace(
        index=None,
        catalog_dir=None,
        dry_run=False,
        with_datasets=None,
        dataset_ids=None,
        func=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _product_with_datasets(product_id: str, *dataset_ids: str) -> FakeProduct:
    """A product carrying one trivial dataset per id (one variable each)."""
    datasets = [
        make_dataset(ds_id, [FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature")])
        for ds_id in dataset_ids
    ]
    return FakeProduct(product_id, datasets, title=product_id)


class TestCmdRefresh:
    """End-to-end behaviour of `_cmd_refresh` (rewrites _index.yaml)."""

    def test_dry_run_does_not_touch_index(
        self,
        fake_cm_module: FakeCmemsModule,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`--dry-run` prints the dataset-id block but leaves _index.yaml alone."""
        fake_cm_module.describe_response = FakeCatalogue(
            [_product_with_datasets("PROD_A", "live_ds_1", "live_ds_2")]
        )
        index = catalog_dir / "_index.yaml"
        before = index.read_text(encoding="utf-8")
        args = _make_args(index=index, dry_run=True)
        rc = rcc._cmd_refresh(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "available_datasets:" in out
        assert "live_ds_1" in out and "live_ds_2" in out
        assert index.read_text(encoding="utf-8") == before

    def test_write_mode_rewrites_available_datasets(
        self,
        fake_cm_module: FakeCmemsModule,
        catalog_dir: Path,
    ) -> None:
        """Non-dry write replaces available_datasets: with the live dataset ids."""
        fake_cm_module.describe_response = FakeCatalogue(
            [_product_with_datasets("PROD_A", "live_ds_1", "live_ds_2")]
        )
        index = catalog_dir / "_index.yaml"
        args = _make_args(index=index, dry_run=False)
        rc = rcc._cmd_refresh(args)
        assert rc == 0
        parsed = yaml.safe_load(index.read_text(encoding="utf-8"))
        assert parsed["available_datasets"] == ["live_ds_1", "live_ds_2"]
        assert "# index header" in index.read_text(encoding="utf-8")

    def test_describe_failure_surfaces_as_nonzero(
        self,
        fake_cm_module: FakeCmemsModule,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A toolbox-level exception during describe -> exit code 1."""

        def boom(**kwargs: object) -> None:
            raise RuntimeError("toolbox down")

        fake_cm_module.describe = boom  # type: ignore[assignment]
        args = _make_args(index=catalog_dir / "_index.yaml", dry_run=False)
        rc = rcc._cmd_refresh(args)
        assert rc == 1
        assert "toolbox down" in capsys.readouterr().err

    def test_with_datasets_emits_stanzas(
        self,
        fake_cm_module: FakeCmemsModule,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
        fake_product: FakeProduct,
    ) -> None:
        """`--with-datasets PROD` walks the product and prints stanzas to stdout."""
        fake_cm_module.describe_response = FakeCatalogue([fake_product])
        fake_cm_module.describe_by_product[fake_product.product_id] = FakeCatalogue(
            [fake_product]
        )
        args = _make_args(
            index=catalog_dir / "_index.yaml",
            dry_run=True,
            with_datasets=[fake_product.product_id],
        )
        rc = rcc._cmd_refresh(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "# ---- paste under" in out
        assert fake_product.datasets[0].dataset_id in out


class TestCmdAddIds:
    """`add-ids <dataset_id>` end-to-end against the catalog directory."""

    def test_skips_already_curated(
        self,
        fake_cm_module: FakeCmemsModule,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An id already curated is reported as skipped and not re-fetched."""
        args = _make_args(catalog_dir=catalog_dir, dataset_ids=["existing_ds"])
        rc = rcc._cmd_add_ids(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "already-curated" in out
        assert "nothing to add" in out
        assert fake_cm_module.describe_calls == []

    def test_appends_fresh_stanza_to_routed_file(
        self,
        fake_cm_module: FakeCmemsModule,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A new dataset lands in its routed per-domain file + _index.yaml."""
        new_ds = make_dataset(
            "fresh_med_ds_P1D-m",
            [FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature")],
            dataset_name="Fresh Med dataset",
        )
        fake_cm_module.describe_by_dataset["fresh_med_ds_P1D-m"] = FakeCatalogue(
            [FakeProduct("MEDSEA_MULTIYEAR_PHY_006_004", [new_ds])]
        )
        args = _make_args(
            catalog_dir=catalog_dir, dataset_ids=["fresh_med_ds_P1D-m"]
        )
        rc = rcc._cmd_add_ids(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "appended 1 stanzas" in out
        # Routed to mediterranean.yaml (created fresh).
        med = (catalog_dir / "mediterranean.yaml").read_text(encoding="utf-8")
        assert "fresh_med_ds_P1D-m:" in med
        # And added to the index so curated ⊆ available holds.
        index = yaml.safe_load((catalog_dir / "_index.yaml").read_text(encoding="utf-8"))
        assert "fresh_med_ds_P1D-m" in index["available_datasets"]

    def test_describe_failure_recorded_as_stderr_skip(
        self,
        fake_cm_module: FakeCmemsModule,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A `DatasetNotFound` is logged and the run exits 1 when nothing appended."""
        args = _make_args(catalog_dir=catalog_dir, dataset_ids=["does_not_exist"])
        rc = rcc._cmd_add_ids(args)
        assert rc == 1
        assert "does_not_exist" in capsys.readouterr().err


class TestCmdCompact:
    """`compact` subcommand: stdin -> stdout normaliser."""

    def test_passes_through_clean_input(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clean canonical stanzas round-trip without losing content."""
        monkeypatch.setattr("sys.stdin", io.StringIO("  ds-a:\n    product: P\n"))
        rc = rcc._cmd_compact(_make_args())
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

    def test_help_exits_zero(self) -> None:
        """`--help` exits 0 with a usage banner."""
        with pytest.raises(SystemExit) as exc_info:
            rcc.main(["--help"])
        assert exc_info.value.code == 0

    def test_refresh_dispatch(
        self,
        fake_cm_module: FakeCmemsModule,
        catalog_dir: Path,
    ) -> None:
        """The `refresh` subcommand routes to `_cmd_refresh`."""
        fake_cm_module.describe_response = FakeCatalogue(
            [_product_with_datasets("PROD_A", "live_ds_1")]
        )
        rc = rcc.main(
            ["refresh", "--dry-run", "--index", str(catalog_dir / "_index.yaml")]
        )
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
