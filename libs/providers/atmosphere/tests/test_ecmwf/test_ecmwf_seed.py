"""Unit tests for `earthlens.ecmwf._seed` (form.json fetch mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from earthlens.cli.stanza import StanzaResult
from earthlens.ecmwf import _seed as seed_mod
from earthlens.ecmwf._seed import bulk_seed_uncurated

pytestmark = pytest.mark.cli


def _ok_result(info, dataset_id, **kw):
    """Return an `ok` StanzaResult for `dataset_id` (a fake emit_stanza)."""
    return StanzaResult(
        provider="ecmwf",
        key=dataset_id,
        upstream_id=dataset_id,
        status="ok",
        row={
            "endpoint": "cds",
            "request_kind": "form",
            "variables": {
                "2m-temperature": {
                    "cds_variable": "2m_temperature",
                    "nc_variable": "2m_temperature",
                    "units": "unknown",
                }
            },
        },
    )


def _patch_catalog(monkeypatch, tmp_path, available, datasets):
    """Redirect the ecmwf catalog at a fake `(available, datasets)` + tmp shard dir."""
    import earthlens.ecmwf as ecmwf
    import earthlens.ecmwf.catalog as ecmwf_catalog

    fake = SimpleNamespace(available_datasets=list(available), datasets=dict(datasets))
    monkeypatch.setattr(ecmwf, "Catalog", lambda: fake)
    monkeypatch.setattr(ecmwf_catalog, "CATALOG_PATH", tmp_path)
    monkeypatch.setattr(ecmwf_catalog, "clear_catalog_cache", lambda: None)


class TestBulkSeedUncurated:
    """Tests for the catalog-wide seed driver (form.json + catalog mocked)."""

    def test_seeds_uncurated_into_categorised_shard(self, tmp_path, monkeypatch):
        """Uncurated = available - datasets; each row lands in its family shard."""
        _patch_catalog(
            monkeypatch,
            tmp_path,
            available=[
                "reanalysis-era5-single-levels",  # curated -> excluded
                "reanalysis-era5-complete",  # uncurated -> era5.yaml
                "cams-global-reanalysis-eac4",  # uncurated -> ads.yaml
            ],
            datasets={"reanalysis-era5-single-levels": object()},
        )
        monkeypatch.setattr(seed_mod, "emit_stanza", _ok_result)

        summary = bulk_seed_uncurated()
        assert summary["candidates"] == 2, "uncurated = available - datasets"
        assert summary["seeded"] == 2
        assert summary["skipped"] == 0
        era5 = yaml.safe_load((tmp_path / "era5.yaml").read_text())["datasets"]
        ads = yaml.safe_load((tmp_path / "ads.yaml").read_text())["datasets"]
        assert "reanalysis-era5-complete" in era5, "era5 id routed to era5.yaml"
        assert "cams-global-reanalysis-eac4" in ads, "cams id routed to ads.yaml"

    def test_non_ok_emit_is_skipped_and_recorded(self, tmp_path, monkeypatch):
        """A dataset whose form fetch fails is skipped and recorded in `failed`."""
        _patch_catalog(
            monkeypatch,
            tmp_path,
            available=["reanalysis-era5-complete", "cams-broken"],
            datasets={},
        )

        def fake_emit(info, dataset_id, **kw):
            if dataset_id == "cams-broken":
                return StanzaResult("ecmwf", dataset_id, dataset_id, "error", "boom")
            return _ok_result(info, dataset_id)

        monkeypatch.setattr(seed_mod, "emit_stanza", fake_emit)

        summary = bulk_seed_uncurated()
        assert summary["seeded"] == 1
        assert summary["skipped"] == 1
        assert summary["failed"] == [("cams-broken", "boom")]

    def test_duplicate_key_is_skipped(self, tmp_path, monkeypatch):
        """A key already curated in its shard (re-run) is skipped, not duplicated."""
        (tmp_path / "era5.yaml").write_text(
            "datasets:\n  reanalysis-era5-complete:\n    endpoint: cds\n",
            encoding="utf-8",
        )
        _patch_catalog(
            monkeypatch,
            tmp_path,
            available=["reanalysis-era5-complete"],
            datasets={},
        )
        monkeypatch.setattr(seed_mod, "emit_stanza", _ok_result)

        summary = bulk_seed_uncurated()
        assert summary["seeded"] == 0
        assert summary["skipped"] == 1

    def test_limit_caps_candidates(self, tmp_path, monkeypatch):
        """A --limit truncates the uncurated worklist."""
        _patch_catalog(
            monkeypatch,
            tmp_path,
            available=["cams-a", "cams-b", "cams-c"],
            datasets={},
        )
        monkeypatch.setattr(seed_mod, "emit_stanza", _ok_result)

        summary = bulk_seed_uncurated(limit=2)
        assert summary["candidates"] == 2, "limit applied to the worklist"
        assert summary["seeded"] == 2
