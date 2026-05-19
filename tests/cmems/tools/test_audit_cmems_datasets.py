"""Tests for tools/cmems/audit_cmems_datasets.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import audit_cmems_datasets as acd  # noqa: E402 (sys.path injection in conftest)
from tests.cmems.tools.conftest import (
    FakeCatalogue,
    FakeCmemsModule,
    FakeProduct,
    FakeVariable,
    make_dataset,
)


@pytest.fixture
def temp_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bundle a minimal 2-row catalog and point Catalog.load at it."""
    path = tmp_path / "cmems_data_catalog.yaml"
    path.write_text(
        "available_products:\n"
        "  - PROD_A\n"
        "datasets:\n"
        "  ds-covered:\n"
        "    product: PROD_A\n"
        "    title: covered\n"
        "    cadence: daily\n"
        "    domain: global\n"
        "    temporal:\n"
        "      start: null\n"
        "      end: null\n"
        "    variables:\n"
        "      thetao:\n"
        "        units: degrees_C\n"
        "        long_name: Sea water potential temperature\n"
        "  ds-partial:\n"
        "    product: PROD_A\n"
        "    title: partial\n"
        "    cadence: daily\n"
        "    domain: global\n"
        "    temporal:\n"
        "      start: null\n"
        "      end: null\n"
        "    variables:\n"
        "      thetao:\n"
        "        units: degrees_C\n"
        "        long_name: Sea water potential temperature\n"
        "      missing_var:\n"
        "        units: '1'\n"
        "        long_name: ''\n"
        "  ds-missing:\n"
        "    product: PROD_A\n"
        "    title: missing\n"
        "    cadence: daily\n"
        "    domain: global\n"
        "    temporal:\n"
        "      start: null\n"
        "      end: null\n"
        "    variables:\n"
        "      x:\n"
        "        units: '1'\n"
        "        long_name: ''\n"
        "  ds-renamed-old:\n"
        "    product: PROD_A\n"
        "    title: renamed\n"
        "    cadence: daily\n"
        "    domain: global\n"
        "    temporal:\n"
        "      start: null\n"
        "      end: null\n"
        "    variables:\n"
        "      y:\n"
        "        units: '1'\n"
        "        long_name: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("earthlens.cmems.catalog.CATALOG_PATH", path)
    from earthlens.cmems.catalog import clear_catalog_cache

    clear_catalog_cache()
    return path


@pytest.fixture
def populated_cm(fake_cm_module: FakeCmemsModule) -> FakeCmemsModule:
    """Wire up a fake toolbox response covering all four audit statuses."""
    covered_ds = make_dataset(
        "ds-covered",
        [FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature")],
    )
    partial_ds = make_dataset(
        "ds-partial",
        [FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature")],
    )
    renamed_to = make_dataset(
        "ds-renamed-new",
        [FakeVariable("y", "1", "y_std")],
    )
    fake_cm_module.describe_by_dataset["ds-covered"] = FakeCatalogue(
        [FakeProduct("PROD_A", [covered_ds])]
    )
    fake_cm_module.describe_by_dataset["ds-partial"] = FakeCatalogue(
        [FakeProduct("PROD_A", [partial_ds])]
    )
    fake_cm_module.describe_by_dataset["ds-renamed-old"] = FakeCatalogue(
        [FakeProduct("PROD_A", [renamed_to])]
    )
    fake_cm_module.describe_raises["ds-missing"] = fake_cm_module.DatasetNotFound(
        "no such id"
    )
    return fake_cm_module


class TestDescribe:
    """`_describe` wrapper."""

    def test_success_returns_response(
        self, fake_cm_module: FakeCmemsModule
    ) -> None:
        """A normal response is returned with `error=None`."""
        ds = make_dataset("ds-1", [FakeVariable("v", "1", "v_std")])
        fake_cm_module.describe_by_dataset["ds-1"] = FakeCatalogue(
            [FakeProduct("P", [ds])]
        )
        resp, err = acd._describe("ds-1")
        assert resp is not None
        assert err is None

    def test_dataset_not_found_captured(
        self, fake_cm_module: FakeCmemsModule
    ) -> None:
        """`DatasetNotFound` lands as `(None, 'DatasetNotFound: ...')`."""
        fake_cm_module.describe_raises["ds-x"] = fake_cm_module.DatasetNotFound(
            "nope"
        )
        resp, err = acd._describe("ds-x")
        assert resp is None
        assert err is not None and "DatasetNotFound" in err

    def test_generic_exception_captured(
        self, fake_cm_module: FakeCmemsModule
    ) -> None:
        """Any other exception lands as `(None, '<Type>: <message>')`."""

        def boom(**kwargs: object) -> None:
            raise RuntimeError("network down")

        fake_cm_module.describe = boom  # type: ignore[assignment]
        resp, err = acd._describe("ds-x")
        assert resp is None
        assert err is not None and "RuntimeError" in err and "network down" in err


class TestLiveDataset:
    """`_live_dataset` resolver."""

    def test_exact_match(self) -> None:
        """Returns the dataset whose id matches the request."""
        ds_a = make_dataset("a", [FakeVariable("v", "1", "vs")])
        ds_b = make_dataset("b", [FakeVariable("v", "1", "vs")])
        resp = FakeCatalogue([FakeProduct("P", [ds_a, ds_b])])
        assert acd._live_dataset(resp, "b") is ds_b

    def test_redirect_returns_first_dataset(self) -> None:
        """When no exact id match, returns the first dataset (the redirect target)."""
        ds = make_dataset("other-id", [FakeVariable("v", "1", "vs")])
        resp = FakeCatalogue([FakeProduct("P", [ds])])
        result = acd._live_dataset(resp, "requested-id")
        assert result is ds

    def test_empty_response(self) -> None:
        """A product-less response returns None."""
        resp = FakeCatalogue([])
        assert acd._live_dataset(resp, "anything") is None


class TestClassify:
    """End-to-end classification across the four audit statuses."""

    def test_full_matrix(self, populated_cm: FakeCmemsModule) -> None:
        """One dataset of each status class lands in the expected bucket."""
        curated = {
            "ds-covered": ["thetao"],
            "ds-partial": ["thetao", "missing_var"],
            "ds-missing": ["x"],
            "ds-renamed-old": ["y"],
        }
        rows = acd.classify(curated)
        by_id = {r["dataset_id"]: r for r in rows}
        assert by_id["ds-covered"]["status"] == "covered"
        assert by_id["ds-covered"]["missing_variables"] == []
        assert by_id["ds-partial"]["status"] == "partial"
        assert by_id["ds-partial"]["missing_variables"] == ["missing_var"]
        assert by_id["ds-missing"]["status"] == "missing"
        assert by_id["ds-renamed-old"]["status"] == "renamed"
        assert by_id["ds-renamed-old"]["live_id"] == "ds-renamed-new"


class TestEmitMarkdown:
    """Markdown table output."""

    def test_table_header(self) -> None:
        """First two lines are always the header + separator."""
        rows = [
            {
                "dataset_id": "ds-1",
                "status": "covered",
                "live_id": "ds-1",
                "missing_variables": [],
                "error": None,
            }
        ]
        out = acd.emit_markdown(rows)
        lines = out.splitlines()
        assert lines[0].startswith("| Dataset ID")
        assert lines[1].startswith("|---")
        assert "ds-1" in out
        assert "covered" in out

    def test_details_per_status(self) -> None:
        """Each status produces a recognisable detail string."""
        rows = [
            {
                "dataset_id": "a",
                "status": "covered",
                "live_id": "a",
                "missing_variables": [],
                "error": None,
            },
            {
                "dataset_id": "b",
                "status": "partial",
                "live_id": "b",
                "missing_variables": ["x", "y"],
                "error": None,
            },
            {
                "dataset_id": "c",
                "status": "renamed",
                "live_id": "c-new",
                "missing_variables": [],
                "error": None,
            },
            {
                "dataset_id": "d",
                "status": "missing",
                "live_id": None,
                "missing_variables": [],
                "error": "DatasetNotFound",
            },
        ]
        out = acd.emit_markdown(rows)
        assert "OK" in out
        assert "missing variables: x, y" in out
        assert "renamed to `c-new`" in out
        assert "DatasetNotFound" in out


class TestEmitJson:
    """JSON output."""

    def test_sorts_by_status(self) -> None:
        """`covered` rows precede `partial`/`renamed`/`missing` ones."""
        rows = [
            {
                "dataset_id": "z-missing",
                "status": "missing",
                "live_id": None,
                "missing_variables": [],
                "error": None,
            },
            {
                "dataset_id": "a-covered",
                "status": "covered",
                "live_id": "a-covered",
                "missing_variables": [],
                "error": None,
            },
        ]
        parsed = json.loads(acd.emit_json(rows))
        assert parsed[0]["status"] == "covered"
        assert parsed[1]["status"] == "missing"


class TestMain:
    """CLI dispatch."""

    def test_help(self) -> None:
        """`--help` exits 0."""
        with pytest.raises(SystemExit) as exc_info:
            acd.main(["--help"])
        assert exc_info.value.code == 0

    def test_default_markdown_run(
        self,
        populated_cm: FakeCmemsModule,
        temp_catalog: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Default format is markdown, exit 0 even on drift."""
        rc = acd.main(["--catalog", str(temp_catalog)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Dataset ID" in out
        assert "ds-covered" in out

    def test_strict_exits_one_on_drift(
        self,
        populated_cm: FakeCmemsModule,
        temp_catalog: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`--strict` exits 1 when any non-covered row exists."""
        rc = acd.main(["--strict", "--catalog", str(temp_catalog)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--strict" in err

    def test_json_format(
        self,
        populated_cm: FakeCmemsModule,
        temp_catalog: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`--format=json` outputs parseable JSON."""
        rc = acd.main(
            ["--format", "json", "--catalog", str(temp_catalog)]
        )
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert all("dataset_id" in r for r in parsed)
