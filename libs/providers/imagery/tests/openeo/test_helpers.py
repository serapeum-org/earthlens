"""Unit tests for `earthlens.openeo._helpers` (pure mapping helpers)."""

from __future__ import annotations

import sys

import pytest

from earthlens.openeo import _helpers
from earthlens.openeo._helpers import (
    DEFAULT_ENDPOINT,
    import_openeo,
    period_for,
    reducer_for,
    resolve_endpoint,
)


@pytest.mark.openeo
class TestResolveEndpoint:
    """`resolve_endpoint` handles aliases, URLs, None, and bad input."""

    def test_named_alias(self):
        """A named alias maps to its API root URL."""
        assert resolve_endpoint("cdse") == "https://openeo.dataspace.copernicus.eu"
        assert resolve_endpoint("openeo-platform") == "https://openeo.cloud"

    def test_none_is_default(self):
        """None falls back to the CDSE-core default."""
        assert resolve_endpoint(None) == DEFAULT_ENDPOINT

    def test_full_url_passthrough(self):
        """A full http(s) URL is returned unchanged."""
        assert resolve_endpoint("https://x.org/openeo") == "https://x.org/openeo"

    def test_unknown_alias_raises(self):
        """An unknown bare alias is rejected with a helpful message."""
        with pytest.raises(ValueError, match="unknown openEO endpoint"):
            resolve_endpoint("not-an-endpoint")


@pytest.mark.openeo
class TestPeriodFor:
    """`period_for` maps pandas freq aliases to openEO calendar periods."""

    @pytest.mark.parametrize(
        "freq,expected",
        [
            ("D", "day"),
            ("1MS", "month"),
            ("MS", "month"),
            ("10D", "dekad"),
            ("7D", "day"),
            ("YS", "year"),
            ("QS", "season"),
            ("h", "hour"),
        ],
    )
    def test_known_freqs(self, freq, expected):
        """Each supported pandas alias maps to the right calendar period."""
        assert period_for(freq) == expected

    def test_unknown_freq_raises(self):
        """A freq with no calendar equivalent raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="no calendar period"):
            period_for("3B")


@pytest.mark.openeo
class TestReducerFor:
    """`reducer_for` maps aggregator ops + raw reducer names."""

    def test_auto_is_mean(self):
        """`auto` resolves to mean."""
        assert reducer_for("auto") == "mean"

    def test_std_maps_to_sd(self):
        """The aggregator's std maps to openEO's sd."""
        assert reducer_for("std") == "sd"

    def test_raw_reducer_passthrough(self):
        """A raw openEO reducer name (not an aggregator op) is accepted verbatim."""
        assert reducer_for("median") == "median"
        assert reducer_for("first") == "first"

    def test_unknown_op_raises(self):
        """An unknown op raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="not supported"):
            reducer_for("bogus")


@pytest.mark.openeo
class TestImportOpeneo:
    """`import_openeo` returns the SDK or a friendly ImportError."""

    def test_returns_module(self):
        """When installed, the openeo module is returned."""
        assert import_openeo().__name__ == "openeo"

    def test_missing_extra_raises_friendly(self, monkeypatch: pytest.MonkeyPatch):
        """A missing openeo surfaces as ImportError naming earthlens[openeo]."""
        monkeypatch.setitem(sys.modules, "openeo", None)
        with pytest.raises(ImportError, match=r"earthlens\[openeo\]"):
            import_openeo()

    def test_module_constants_present(self):
        """The output-format and reducer tables expose the expected keys."""
        assert set(_helpers.OUTPUT_FORMATS) == {"GTiff", "netCDF"}
        assert "sd" in _helpers.OPENEO_REDUCERS
