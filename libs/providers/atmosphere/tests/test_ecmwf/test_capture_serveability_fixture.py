"""Unit tests for the operator script `tools/ecmwf/capture_serveability_fixture.py`.

The script is not part of the shipped `earthlens.ecmwf` package (it lives under
`tools/`), so it is loaded here by file path. Its only logic — `capture` — is
exercised fully offline: `Catalog` and `_ecmwf_constraints` are monkeypatched on
the loaded module, so no network or live store is touched.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _load_capture() -> ModuleType:
    """Import the standalone `tools/ecmwf/capture_serveability_fixture.py` by path.

    Returns:
        ModuleType: The loaded module.

    Raises:
        FileNotFoundError: When the script cannot be found above this file.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tools" / "ecmwf" / "capture_serveability_fixture.py"
        if candidate.exists():
            spec = importlib.util.spec_from_file_location(
                "ecmwf_capture_serveability_fixture", candidate
            )
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError(
        "tools/ecmwf/capture_serveability_fixture.py not found above the test file"
    )


capture_mod = _load_capture()


def _row(units: str = "K", unhydratable=None) -> SimpleNamespace:
    """Build a catalog variable row the real `_promises_data` can judge."""
    return SimpleNamespace(units=units, unhydratable=unhydratable)


def _dataset(**variables) -> SimpleNamespace:
    """Build a dataset exposing a `variables` slug->row mapping."""
    return SimpleNamespace(variables=dict(variables))


def _patch_catalog(monkeypatch, datasets) -> None:
    """Point the tool's `Catalog()` at a fake exposing `datasets`."""
    monkeypatch.setattr(
        capture_mod, "Catalog", lambda: SimpleNamespace(datasets=datasets)
    )


def _patch_constraints(monkeypatch, responder) -> None:
    """Replace the tool's `_ecmwf_constraints` with a callable fake.

    `capture` calls it as `_ecmwf_constraints(name, strict=True)`, so the fake
    absorbs `strict` and the test responders need only take `name`.
    """
    monkeypatch.setattr(
        capture_mod,
        "_ecmwf_constraints",
        lambda name, strict=False: responder(name),
    )


def _read_snapshot(path: Path) -> dict:
    """Round-trip the written gzip+JSON snapshot back into a dict."""
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read())


class TestCapture:
    """Tests for `capture`, the sole function in the operator script."""

    def test_normal_dataset_records_canonicalised_blocks(self, monkeypatch, tmp_path):
        """A promising dataset's blocks are recorded with list values sorted and scalars left untouched."""
        _patch_catalog(monkeypatch, {"normal": _dataset(t2m=_row())})
        block = {"variable": ["b", "a", "c"], "data_format": "netcdf"}
        _patch_constraints(monkeypatch, lambda name: [dict(block)])

        out = tmp_path / "nested" / "serveability.json.gz"
        capture_mod.capture(out)

        snapshot = _read_snapshot(out)
        assert snapshot == {
            "normal": [{"variable": ["a", "b", "c"], "data_format": "netcdf"}]
        }, f"blocks not canonicalised as expected: {snapshot}"

    def test_parent_directories_are_created(self, monkeypatch, tmp_path):
        """A missing parent directory of `out` is created before the snapshot is written."""
        _patch_catalog(monkeypatch, {"normal": _dataset(t2m=_row())})
        _patch_constraints(monkeypatch, lambda name: [{"variable": ["x"]}])

        out = tmp_path / "a" / "b" / "snap.json.gz"
        capture_mod.capture(out)

        assert out.exists(), "capture did not create the output file and its parents"

    def test_dataset_with_no_promising_row_is_skipped(self, monkeypatch, tmp_path):
        """A dataset whose rows are all placeholders is skipped and never fetched."""
        _patch_catalog(monkeypatch, {"placeholder": _dataset(x=_row(units="unknown"))})
        fetched = []
        _patch_constraints(
            monkeypatch, lambda name: fetched.append(name) or [{"variable": ["x"]}]
        )

        out = tmp_path / "snap.json.gz"
        capture_mod.capture(out)

        snapshot = _read_snapshot(out)
        assert snapshot == {}, f"a non-promising dataset was captured: {snapshot}"
        assert fetched == [], f"a skipped dataset was still fetched: {fetched}"

    def test_a_failed_fetch_is_recorded_as_unreachable(self, monkeypatch, tmp_path):
        """A dataset whose fetch raises is recorded as null (unreachable), distinct from an empty store."""
        _patch_catalog(monkeypatch, {"broken": _dataset(t2m=_row())})

        def boom(name):
            raise RuntimeError("store unreachable")

        _patch_constraints(monkeypatch, boom)

        out = tmp_path / "snap.json.gz"
        capture_mod.capture(out)

        snapshot = _read_snapshot(out)
        assert snapshot == {"broken": None}, (
            f"a failed fetch was not recorded as unreachable (null): {snapshot}"
        )

    def test_a_none_fetch_result_becomes_empty_blocks(self, monkeypatch, tmp_path):
        """A fetch returning `None` is coerced to an empty block list by the `or []` guard."""
        _patch_catalog(monkeypatch, {"empty": _dataset(t2m=_row())})
        _patch_constraints(monkeypatch, lambda name: None)

        out = tmp_path / "snap.json.gz"
        capture_mod.capture(out)

        assert _read_snapshot(out) == {"empty": []}, "None was not coerced to []"

    def test_mixed_datasets_capture_only_the_promising_ones(
        self, monkeypatch, tmp_path
    ):
        """Across a mix of skipped, normal and partly-placeholder datasets, only the promising ones are captured."""
        datasets = {
            "skip": _dataset(x=_row(units="unknown")),
            "normal": _dataset(t2m=_row()),
            "mixed": _dataset(ph=_row(units="unknown"), real=_row()),
        }
        _patch_catalog(monkeypatch, datasets)
        _patch_constraints(monkeypatch, lambda name: [{"variable": [name]}])

        out = tmp_path / "snap.json.gz"
        capture_mod.capture(out)

        snapshot = _read_snapshot(out)
        assert set(snapshot) == {"normal", "mixed"}, (
            f"the wrong datasets were captured: {sorted(snapshot)}"
        )
        assert snapshot["mixed"] == [{"variable": ["mixed"]}]

    def test_two_runs_are_byte_identical(self, monkeypatch, tmp_path):
        """Two captures of the same inputs produce byte-identical gzip output."""
        _patch_catalog(monkeypatch, {"normal": _dataset(t2m=_row())})
        _patch_constraints(
            monkeypatch, lambda name: [{"variable": ["b", "a"], "product_type": ["f"]}]
        )

        first = tmp_path / "run_a" / "serveability_constraints.json.gz"
        second = tmp_path / "run_b" / "serveability_constraints.json.gz"
        capture_mod.capture(first)
        capture_mod.capture(second)

        assert first.read_bytes() == second.read_bytes(), (
            "two captures of the same inputs produced different bytes"
        )
