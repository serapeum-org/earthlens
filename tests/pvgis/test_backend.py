"""Unit tests for the PVGIS backend (`earthlens.pvgis.backend`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from earthlens.pvgis import PVGIS

from .conftest import FakeResponse

pytestmark = pytest.mark.pvgis


def _backend(tmp_path: Path, **overrides: Any) -> PVGIS:
    """Construct a single-point seriescalc PVGIS backend with overrides."""
    params: dict[str, Any] = dict(
        start="2020-01-01",
        end="2020-12-31",
        variables=["seriescalc"],
        point=(45.0, 8.0),
        path=str(tmp_path),
    )
    params.update(overrides)
    return PVGIS(**params)


class TestConstruction:
    """Tests for `PVGIS.__init__` validation and location handling."""

    def test_variables_dict_raises_type_error(self, tmp_path):
        """A mapping `variables` raises TypeError."""
        with pytest.raises(TypeError, match="single-element list"):
            _backend(tmp_path, variables={"seriescalc": []})

    def test_bad_output_format_raises(self, tmp_path):
        """An unrecognised output_format raises ValueError."""
        with pytest.raises(ValueError, match="output_format must be one of"):
            _backend(tmp_path, output_format="zarr")

    def test_point_overrides_bbox(self, tmp_path):
        """point= wins, collapsing the request to that single coordinate."""
        backend = _backend(tmp_path, lat_lim=[40.0, 50.0], lon_lim=[0.0, 10.0])
        assert backend.space.south == 45.0 and backend.space.north == 45.0
        assert backend.space.west == 8.0 and backend.space.east == 8.0

    def test_missing_location_raises(self, tmp_path):
        """Neither point= nor a bbox raises ValueError."""
        with pytest.raises(ValueError, match="needs a location"):
            _backend(tmp_path, point=None)

    def test_unknown_product_raises(self, tmp_path):
        """An unknown product id raises via the catalog during init."""
        with pytest.raises(ValueError, match="PVGIS product catalog"):
            _backend(tmp_path, variables=["nope"])

    def test_empty_variables_defaults_seriescalc(self, tmp_path):
        """An empty variables list defaults to the seriescalc product."""
        assert _backend(tmp_path, variables=[])._product.tool == "seriescalc"


class TestDownloadSinglePoint:
    """Tests for a single-point `download`."""

    def test_returns_tagged_frame(self, tmp_path, bind_session, seriescalc_payload):
        """A single point returns the hourly rows tagged with lat/lon/product."""
        session = bind_session(FakeResponse(seriescalc_payload))
        df = _backend(tmp_path).download(progress_bar=False)
        assert len(session.calls) == 1, f"expected 1 GET, got {len(session.calls)}"
        n = len(seriescalc_payload["outputs"]["hourly"])
        assert len(df) == n, f"expected {n} rows, got {len(df)}"
        assert {"lat", "lon", "product"}.issubset(df.columns), list(df.columns)
        assert (df["product"] == "seriescalc").all(), "product tag wrong"

    def test_seriescalc_url_carries_year_window(
        self, tmp_path, bind_session, seriescalc_payload
    ):
        """seriescalc derives startyear/endyear from the request window."""
        session = bind_session(FakeResponse(seriescalc_payload))
        _backend(tmp_path).download(progress_bar=False)
        assert "startyear=2020" in session.calls[0], session.calls[0]
        assert "endyear=2020" in session.calls[0], session.calls[0]

    def test_knobs_forwarded_to_url(self, tmp_path, bind_session, seriescalc_payload):
        """PV knobs ride through to the query string; None knobs are dropped."""
        session = bind_session(FakeResponse(seriescalc_payload))
        _backend(tmp_path, pvcalculation=1, peakpower=1, loss=None).download(
            progress_bar=False
        )
        assert "pvcalculation=1" in session.calls[0], session.calls[0]
        assert "loss=" not in session.calls[0], session.calls[0]

    def test_writes_csv(self, tmp_path, bind_session, seriescalc_payload):
        """The download writes a CSV table to root_dir."""
        bind_session(FakeResponse(seriescalc_payload))
        _backend(tmp_path).download(progress_bar=False)
        assert (tmp_path / "pvgis_seriescalc.csv").exists(), "CSV not written"

    def test_writes_parquet(self, tmp_path, bind_session, seriescalc_payload):
        """The download writes Parquet when requested (needs pyarrow)."""
        pytest.importorskip("pyarrow")
        bind_session(FakeResponse(seriescalc_payload))
        _backend(tmp_path, output_format="parquet").download(progress_bar=False)
        assert (tmp_path / "pvgis_seriescalc.parquet").exists(), "Parquet not written"

    def test_logs_citation_on_success(
        self, tmp_path, bind_session, seriescalc_payload
    ):
        """A successful download logs the JRC citation once."""
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="INFO")
        try:
            bind_session(FakeResponse(seriescalc_payload))
            _backend(tmp_path).download(progress_bar=False)
        finally:
            logger.remove(sink_id)
        assert sum("PVGIS (c) European Union" in m for m in messages) == 1, messages


class TestDownloadTmy:
    """Tests for the TMY routing."""

    def test_tmy_routing(self, tmp_path, bind_session, tmy_payload):
        """A tmy request parses the TMY shape and omits the year window."""
        session = bind_session(FakeResponse(tmy_payload))
        df = _backend(tmp_path, variables=["tmy"]).download(progress_bar=False)
        assert "RH" in df.columns, list(df.columns)
        assert "startyear" not in session.calls[0], session.calls[0]
        assert (df["product"] == "tmy").all(), "product tag wrong"


class TestDownloadBbox:
    """Tests for a bbox sampled to a point grid."""

    def test_one_get_per_grid_point(self, tmp_path, bind_session, seriescalc_payload):
        """A 2x2 bbox at 1-degree spacing issues one GET per grid point."""
        session = bind_session(FakeResponse(seriescalc_payload))
        df = _backend(
            tmp_path,
            point=None,
            lat_lim=[45.0, 47.0],
            lon_lim=[8.0, 10.0],
            spacing_deg=1.0,
        ).download(progress_bar=False)
        assert len(session.calls) == 9, f"expected 9 GETs, got {len(session.calls)}"
        assert len(df.groupby(["lat", "lon"])) == 9, "expected 9 distinct points"

    def test_over_cap_raises(self, tmp_path, bind_session, seriescalc_payload):
        """A grid larger than max_points raises before any fetch."""
        bind_session(FakeResponse(seriescalc_payload))
        backend = _backend(
            tmp_path,
            point=None,
            lat_lim=[45.0, 47.0],
            lon_lim=[8.0, 10.0],
            spacing_deg=0.1,
        )
        with pytest.raises(ValueError, match="max_points"):
            backend.download(progress_bar=False)

    def test_warn_past_soft_threshold(
        self, tmp_path, bind_session, seriescalc_payload
    ):
        """A grid above the soft threshold logs a warning but still fetches."""
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING")
        try:
            bind_session(FakeResponse(seriescalc_payload))
            _backend(
                tmp_path,
                point=None,
                lat_lim=[45.0, 46.0],
                lon_lim=[8.0, 9.0],
                spacing_deg=0.1,
            ).download(progress_bar=False)
        finally:
            logger.remove(sink_id)
        assert any("keyless GETs" in m for m in messages), messages


class TestCoverageAndAggregate:
    """Tests for the coverage policy and the aggregate guard."""

    def test_aggregate_rejected(self, tmp_path):
        """A non-None aggregate raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="tabular"):
            _backend(tmp_path).download(aggregate=object())

    def test_single_point_out_of_coverage_raises(self, tmp_path, bind_session):
        """A single out-of-coverage point raises ValueError naming the coord."""
        bind_session(FakeResponse({"message": "Location over the sea"}, 400))
        with pytest.raises(ValueError, match=r"lat=45.0, lon=8.0"):
            _backend(tmp_path).download(progress_bar=False)

    def test_single_point_error_text_fallback(self, tmp_path, bind_session):
        """A non-JSON error body falls back to the response text."""
        bind_session(FakeResponse(None, 400, text="boom"))
        with pytest.raises(ValueError, match="boom"):
            _backend(tmp_path).download(progress_bar=False)

    def test_bbox_skips_out_of_coverage(self, tmp_path, bind_session):
        """An out-of-coverage point in a bbox is skipped, yielding an empty frame."""
        bind_session(FakeResponse({"message": "sea"}, 400))
        df = _backend(
            tmp_path,
            point=None,
            lat_lim=[85.0, 86.0],
            lon_lim=[0.0, 1.0],
            spacing_deg=1.0,
        ).download(progress_bar=False)
        assert df.empty, "all points skipped -> empty frame"
        assert {"lat", "lon", "product"}.issubset(df.columns), list(df.columns)
