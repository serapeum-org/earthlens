"""Lock-in for the two new `Catalog.health()` checks added by C5."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.chc import Catalog
from earthlens.chc.catalog import clear_catalog_cache

pytestmark = [pytest.mark.chc]


def _dataset_block(key: str) -> str:
    """Render a minimal-valid `datasets.<key>:` YAML body for synthetic tests."""
    return (
        f"  {key}:\n"
        "    ftp_bases: {tif: 'pub/foo/'}\n"
        "    file_patterns: {tif: 'foo.tif'}\n"
        "    region: global\n"
        "    temporal_resolution: daily\n"
        "    pandas_freq: D\n"
        "    spatial_resolution: [0.05]\n"
        "    formats: [tif]\n"
        "    start_date: '2020-01-01'\n"
        "    variables:\n"
        "      precipitation:\n"
        "        description: synthetic\n"
        "        units: mm/day\n"
        "        types: flux\n"
    )


def _write_catalog(
    tmp_path: Path,
    available: list[str],
    dataset_keys: list[str],
) -> Path:
    """Write a single-file catalog with controlled `available_datasets` / `datasets` sets."""
    catalog_yaml = tmp_path / "catalog.yaml"
    body = "available_datasets:\n"
    for key in available:
        body += f"  - {key}\n"
    body += (
        "regions:\n"
        "  global:\n"
        "    lat_boundaries: [-50, 50]\n"
        "    lon_boundaries: [-180, 180]\n"
        "datasets:\n"
    )
    for key in dataset_keys:
        body += _dataset_block(key)
    catalog_yaml.write_text(body, encoding="utf-8")
    return catalog_yaml


@pytest.fixture(scope="module")
def bundled_catalog() -> Catalog:
    """Bundled catalog, loaded once per module."""
    return Catalog()


class TestHealthIndexConsistency:
    """`Catalog.health()` reports drift between `available_datasets` and `datasets`."""

    def test_bundled_catalog_has_no_drift(self, bundled_catalog: Catalog):
        """The shipped catalog matches `available_datasets` 1:1 to `datasets`."""
        report = bundled_catalog.health()
        assert report["index_missing_in_datasets"] == []
        assert report["datasets_missing_in_index"] == []

    def test_index_missing_in_datasets_lists_only_index_orphans(self, tmp_path: Path):
        """A key in available_datasets but not in datasets surfaces only on the index side."""
        catalog_yaml = _write_catalog(
            tmp_path,
            available=["alpha", "beta"],
            dataset_keys=["alpha"],
        )
        clear_catalog_cache()
        cat = Catalog.load(catalog_path=catalog_yaml)
        report = cat.health()
        assert report["index_missing_in_datasets"] == ["beta"]
        assert report["datasets_missing_in_index"] == []

    def test_datasets_missing_in_index_lists_only_dataset_orphans(self, tmp_path: Path):
        """A key in datasets but not in available_datasets surfaces only on the dataset side."""
        catalog_yaml = _write_catalog(
            tmp_path,
            available=["alpha"],
            dataset_keys=["alpha", "beta"],
        )
        clear_catalog_cache()
        cat = Catalog.load(catalog_path=catalog_yaml)
        report = cat.health()
        assert report["index_missing_in_datasets"] == []
        assert report["datasets_missing_in_index"] == ["beta"]

    def test_both_drifts_at_once_are_reported_separately(self, tmp_path: Path):
        """Keys missing on either side appear on the correct list, never the other."""
        catalog_yaml = _write_catalog(
            tmp_path,
            available=["alpha", "only-in-index"],
            dataset_keys=["alpha", "only-in-datasets"],
        )
        clear_catalog_cache()
        cat = Catalog.load(catalog_path=catalog_yaml)
        report = cat.health()
        assert report["index_missing_in_datasets"] == ["only-in-index"]
        assert report["datasets_missing_in_index"] == ["only-in-datasets"]

    def test_perfectly_aligned_catalog_reports_no_drift(self, tmp_path: Path):
        """Two-entry catalog with equal sets reports clean on both new keys."""
        catalog_yaml = _write_catalog(
            tmp_path,
            available=["alpha", "beta"],
            dataset_keys=["alpha", "beta"],
        )
        clear_catalog_cache()
        cat = Catalog.load(catalog_path=catalog_yaml)
        report = cat.health()
        assert report["index_missing_in_datasets"] == []
        assert report["datasets_missing_in_index"] == []


class TestFtpPathMigration:
    """The shipped catalog uses CHC's reorganised FTP layout (#1386, #1387).

    CHC retired the `pub/org/chc/products/...` tree in favour of a
    `products/...` root. #1386 migrated every product except the
    CHIRPS-GEFS forecast rows, whose v12->v3 remap was a semantic change
    deferred to #1387; #1387 then replaced those rows with the v3
    products under `products/CHIRPS-GEFS/v3/`. No shipped `ftp_base`
    references the retired prefix any more.

    These checks are a prefix/rooting lock-in, not a reachability check:
    they catch a revert to the retired prefix or a stray leading slash,
    but not a `products/`-rooted path mistyped within the tree. Live
    resolution is covered by the e2e `test_all_ftp_bases_resolve_live`.
    """

    #: The retired FTP root that no shipped path may use any more.
    _RETIRED_PREFIX = "pub/org/chc/"
    #: The v3 tree every CHIRPS-GEFS row must now live under (#1387).
    _GEFS_V3_ROOT = "products/CHIRPS-GEFS/v3/"

    def test_no_dataset_uses_the_retired_prefix(self, bundled_catalog: Catalog):
        """No shipped ftp_base may still carry the retired `pub/org/chc/` prefix."""
        offenders = sorted(
            key
            for key, ds in bundled_catalog.datasets.items()
            for path in ds.ftp_bases.values()
            if path.startswith(self._RETIRED_PREFIX)
        )
        assert offenders == [], (
            f"datasets still on the retired {self._RETIRED_PREFIX!r} prefix: {offenders}"
        )

    def test_every_ftp_base_is_rooted_at_products(self, bundled_catalog: Catalog):
        """Every ftp_base starts at `products/` with no leading slash."""
        bad = sorted(
            (key, path)
            for key, ds in bundled_catalog.datasets.items()
            for path in ds.ftp_bases.values()
            if path.startswith("/") or not path.startswith("products/")
        )
        assert bad == [], f"ftp_bases not cleanly rooted at 'products/': {bad}"

    def test_gefs_rows_are_migrated_to_the_v3_tree(self, bundled_catalog: Catalog):
        """Every CHIRPS-GEFS row is now a v3 product under the v3 FTP tree (#1387)."""
        gefs = {
            key: list(ds.ftp_bases.values())
            for key, ds in bundled_catalog.datasets.items()
            if key.startswith("chirps-gefs")
        }
        assert gefs, "no CHIRPS-GEFS rows present in the bundled catalog"
        stale_keys = sorted(k for k in gefs if not k.startswith("chirps-gefs-v3-"))
        assert stale_keys == [], f"non-v3 CHIRPS-GEFS keys remain: {stale_keys}"
        off_tree = sorted(
            key
            for key, paths in gefs.items()
            if not all(p.startswith(self._GEFS_V3_ROOT) for p in paths)
        )
        assert off_tree == [], (
            f"CHIRPS-GEFS rows not under {self._GEFS_V3_ROOT!r}: {off_tree}"
        )


@pytest.mark.e2e
def test_all_ftp_bases_resolve_live(bundled_catalog: Catalog):
    """Every shipped ftp_base resolves on the live CHC FTP."""
    from ftplib import FTP

    from earthlens.chc import CHIRPS

    unreachable: list[str] = []
    ftp = FTP(CHIRPS.api_url, timeout=60)  # noqa: S321 - public anonymous data FTP
    try:
        ftp.login()
        for key, ds in bundled_catalog.datasets.items():
            for path in ds.ftp_bases.values():
                try:
                    ftp.cwd("/" + path.strip("/"))
                except Exception as exc:  # noqa: BLE001 - collect, assert once at end
                    unreachable.append(f"{key}: {path}: {exc}")
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001 - best-effort close
            pass
    assert unreachable == [], (
        f"ftp_bases that did not resolve on the live FTP: {unreachable}"
    )
