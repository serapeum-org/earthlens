"""Lock-in for the M2 pandas_freq load-time validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.chc import Catalog
from earthlens.chc.catalog import clear_catalog_cache

pytestmark = [pytest.mark.chc]


def _dataset_block(key: str, pandas_freq: str) -> str:
    """Render a minimal-valid dataset body with a chosen pandas_freq."""
    return (
        f"  {key}:\n"
        "    ftp_bases: {tif: 'pub/foo/'}\n"
        "    file_patterns: {tif: 'foo.tif'}\n"
        "    region: global\n"
        "    temporal_resolution: daily\n"
        f"    pandas_freq: {pandas_freq!r}\n"
        "    spatial_resolution: [0.05]\n"
        "    formats: [tif]\n"
        "    start_date: '2020-01-01'\n"
        "    variables:\n"
        "      precipitation:\n"
        "        description: synthetic\n"
        "        units: mm/day\n"
        "        types: flux\n"
    )


def _write_catalog(tmp_path: Path, dataset_block: str) -> Path:
    """Write a single-file catalog containing the given dataset body."""
    catalog_yaml = tmp_path / "catalog.yaml"
    catalog_yaml.write_text(
        "available_datasets:\n  - synth\n"
        "regions:\n"
        "  global:\n"
        "    lat_boundaries: [-50, 50]\n"
        "    lon_boundaries: [-180, 180]\n"
        "datasets:\n"
        + dataset_block,
        encoding="utf-8",
    )
    return catalog_yaml


@pytest.fixture(scope="module")
def bundled_catalog() -> Catalog:
    """Bundled catalog, loaded once per module."""
    return Catalog()


def _pandas_rejects(freq: str) -> bool:
    """Whether the installed pandas treats `freq` as an invalid offset alias."""
    import pandas as pd

    try:
        pd.tseries.frequencies.to_offset(freq)
        return False
    except (ValueError, TypeError):
        return True


class TestPandasFreqValidation:
    """`_build_chc_dataset` validates every `pandas_freq` via pandas.to_offset."""

    def test_bundled_catalog_freqs_parse_cleanly(self, bundled_catalog: Catalog):
        """Every dataset's pandas_freq parses without raising."""
        import pandas as pd
        bad: list[tuple[str, str]] = []
        for key, ds in bundled_catalog.datasets.items():
            try:
                pd.tseries.frequencies.to_offset(ds.pandas_freq)
            except (ValueError, TypeError) as exc:
                bad.append((key, f"{ds.pandas_freq!r} -> {exc}"))
        assert not bad, bad

    @pytest.mark.parametrize(
        "freq",
        ["daly", "not-a-freq", "12X"],
    )
    def test_typos_raise(self, tmp_path: Path, freq: str):
        """Each malformed alias is rejected during Catalog.load."""
        block = _dataset_block("synth", pandas_freq=freq)
        catalog_yaml = _write_catalog(tmp_path, block)
        clear_catalog_cache()
        with pytest.raises(ValueError, match=r"pandas_freq") as exc:
            Catalog.load(catalog_path=catalog_yaml)
        assert freq in str(exc.value) or "to_offset" in str(exc.value).lower() or "synth" in str(exc.value)

    @pytest.mark.parametrize(
        "freq",
        ["AS", "H"],
    )
    def test_deprecated_aliases_raise_once_pandas_removes_them(
        self, tmp_path: Path, freq: str
    ):
        """Deprecated aliases are rejected on pandas versions that removed them.

        pandas <3 still accepts `AS` / `H` (only emitting a FutureWarning), so
        the load does not raise there; on pandas >=3 the aliases are gone and
        `to_offset` raises, which the catalog surfaces as a `pandas_freq` error.
        """
        if not _pandas_rejects(freq):
            pytest.skip(f"installed pandas still accepts the deprecated alias {freq!r}")
        block = _dataset_block("synth", pandas_freq=freq)
        catalog_yaml = _write_catalog(tmp_path, block)
        clear_catalog_cache()
        with pytest.raises(ValueError, match=r"pandas_freq"):
            Catalog.load(catalog_path=catalog_yaml)

    @pytest.mark.parametrize(
        "freq",
        ["D", "MS", "YS", "10D", "5D", "2MS", "QS", "6h", "h", "min"],
    )
    def test_valid_aliases_load_clean(self, tmp_path: Path, freq: str):
        """Standard pandas offset aliases pass the validation."""
        block = _dataset_block("synth", pandas_freq=freq)
        catalog_yaml = _write_catalog(tmp_path, block)
        clear_catalog_cache()
        cat = Catalog.load(catalog_path=catalog_yaml)
        assert cat.datasets["synth"].pandas_freq == freq
