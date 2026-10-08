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
    """Replace the tool's `_ecmwf_constraints` with a callable fake."""
    monkeypatch.setattr(capture_mod, "_ecmwf_constraints", responder)


def _read_snapshot(path: Path) -> dict:
    """Round-trip the written gzip+JSON snapshot back into a dict."""
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read())


class TestCapture:
    """Tests for `capture`, the sole function in the operator script."""

    def test_normal_dataset_records_canonicalised_blocks(self, monkeypatch, tmp_path):
        """A promising dataset's blocks are recorded with list values sorted.

        Test scenario:
            One dataset has a filled row, so it is audited; its single block
            mixes an unsorted list selector with a scalar one. The written
            snapshot must sort the list and leave the scalar untouched.
        """
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
        """A non-existent parent of `out` is created before the write.

        Test scenario:
            `out` points two levels below `tmp_path`; `capture` must `mkdir`
            the parents rather than failing, and the file must exist after.
        """
        _patch_catalog(monkeypatch, {"normal": _dataset(t2m=_row())})
        _patch_constraints(monkeypatch, lambda name: [{"variable": ["x"]}])

        out = tmp_path / "a" / "b" / "snap.json.gz"
        capture_mod.capture(out)

        assert out.exists(), "capture did not create the output file and its parents"

    def test_dataset_with_no_promising_row_is_skipped(self, monkeypatch, tmp_path):
        """A dataset whose every row is a placeholder is not audited at all.

        Test scenario:
            The only row carries `units='unknown'`, so `_promises_data` is
            False for it; the dataset must be absent from the snapshot and no
            constraints fetch must be issued for it.
        """
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

    def test_a_failed_fetch_is_recorded_as_empty_blocks(self, monkeypatch, tmp_path):
        """A dataset whose fetch raises is recorded as `[]`, not dropped.

        Test scenario:
            `_ecmwf_constraints` raises for the one audited dataset; `capture`
            must swallow the exception and store an empty block list so the
            offline audit treats it as nothing to judge.
        """
        _patch_catalog(monkeypatch, {"broken": _dataset(t2m=_row())})

        def boom(name):
            raise RuntimeError("store unreachable")

        _patch_constraints(monkeypatch, boom)

        out = tmp_path / "snap.json.gz"
        capture_mod.capture(out)

        snapshot = _read_snapshot(out)
        assert snapshot == {"broken": []}, (
            f"a failed fetch was not recorded as []: {snapshot}"
        )

    def test_a_none_fetch_result_becomes_empty_blocks(self, monkeypatch, tmp_path):
        """A fetch returning `None` is coerced to `[]` by the `or []` guard.

        Test scenario:
            `_ecmwf_constraints` returns None (no constraints document); the
            `blocks = ... or []` branch must store an empty list for the row.
        """
        _patch_catalog(monkeypatch, {"empty": _dataset(t2m=_row())})
        _patch_constraints(monkeypatch, lambda name: None)

        out = tmp_path / "snap.json.gz"
        capture_mod.capture(out)

        assert _read_snapshot(out) == {"empty": []}, "None was not coerced to []"

    def test_mixed_datasets_capture_only_the_promising_ones(
        self, monkeypatch, tmp_path
    ):
        """Across a mix, skipped, failing and normal datasets land correctly.

        Test scenario:
            Three datasets — a placeholder (skipped), a normal one (recorded),
            and a dataset with one placeholder plus one filled row (audited) —
            exercise both sides of the `any(_promises_data(...))` branch in one
            run.
        """
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
        """mtime=0 plus canonicalisation make an unchanged store write identical bytes.

        Test scenario:
            Re-capturing the same catalog and constraints to the same filename
            (two runs of the refresh, modelled as the same basename in two
            directories so both survive) must yield byte-for-byte identical
            gzip output, so an unchanged refresh shows git nothing to commit.
            The basename is kept equal because gzip records it in the header.
        """
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
