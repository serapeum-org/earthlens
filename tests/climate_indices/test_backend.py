"""Tests for the ClimateIndices backend with faked HTTP responses."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import requests

from earthlens.climate_indices import ClimateIndices, backend

DATA = Path(__file__).parent / "data"

#: Map an index file's basename to its captured fixture.
_FIXTURES = {
    "oni.data": DATA / "psl" / "oni.data",
    "nao.data": DATA / "psl" / "nao.data",
    "soi.data": DATA / "psl" / "soi.data",
    "iamo_ersst.dat": DATA / "climexp" / "iamo_ersst.dat",
    "inao.dat": DATA / "climexp" / "inao.dat",
}


class _FakeResponse:
    """A minimal stand-in for `requests.Response`."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """Raise `HTTPError` for a >=400 status, mirroring requests."""
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _fake_get(url: str, timeout: float | None = None) -> _FakeResponse:
    """Return the captured fixture body for a known index URL, else a 404."""
    name = url.rsplit("/", 1)[-1]
    if name in _FIXTURES:
        return _FakeResponse(_FIXTURES[name].read_text())
    return _FakeResponse("Not found", status_code=404)


def _always_404(url: str, timeout: float | None = None) -> _FakeResponse:
    """Return a 404 for every URL."""
    return _FakeResponse("Not found", status_code=404)


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the backend's `requests.get` to the captured fixtures."""
    monkeypatch.setattr(backend.requests, "get", _fake_get)


def test_multi_index_concat_distinguished_by_index(fake_http, tmp_path: Path) -> None:
    """Two indices come back in one frame, split by the index column."""
    df = ClimateIndices(
        start="2000-01-01",
        end="2010-12-31",
        variables=["oni", "amo"],
        path=tmp_path,
    ).download()
    assert list(df.columns) == ["date", "index", "value", "source"]
    assert set(df["index"].unique()) == {"oni", "amo"}
    assert set(df["source"].unique()) == {"noaa-psl", "knmi-climexp"}
    assert (df["date"].dt.day == 1).all()


def test_window_filter_drops_out_of_range_rows(fake_http, tmp_path: Path) -> None:
    """The window filter keeps only rows inside [start, end]."""
    df = ClimateIndices(
        start="1990-01-01",
        end="1999-12-31",
        variables=["oni"],
        path=tmp_path,
    ).download()
    assert df["date"].min() >= pd.Timestamp("1990-01-01")
    assert df["date"].max() <= pd.Timestamp("1999-12-31")
    assert len(df) == 12 * 10


def test_sentinel_maps_to_nan(fake_http, tmp_path: Path) -> None:
    """Sentinel-valued months inside the window become NaN, not raw values."""
    df = ClimateIndices(
        start="1948-01-01",
        end="1950-12-31",
        variables=["nao"],
        path=tmp_path,
    ).download()
    # NAO 1948 is entirely sentinel-valued upstream.
    assert df["value"].isna().any()
    assert not (df["value"] == -99.9).any()


def test_bbox_is_ignored(fake_http, tmp_path: Path) -> None:
    """Passing a bbox yields the same result as omitting it (G4)."""
    common = dict(start="2000-01-01", end="2005-12-31", variables=["oni"])
    without = ClimateIndices(path=tmp_path / "a", **common).download()
    with_bbox = ClimateIndices(
        path=tmp_path / "b",
        lat_lim=[10.0, 20.0],
        lon_lim=[30.0, 40.0],
        **common,
    ).download()
    pd.testing.assert_frame_equal(without, with_bbox)


def test_aggregate_is_rejected(fake_http, tmp_path: Path) -> None:
    """A non-None aggregate raises NotImplementedError (G1)."""
    source = ClimateIndices(
        start="2000-01-01", end="2001-12-31", variables=["oni"], path=tmp_path
    )
    with pytest.raises(NotImplementedError, match="aggregate"):
        source.download(aggregate=object())


def test_empty_variables_raises() -> None:
    """An empty variables list raises with the available indices (G5)."""
    with pytest.raises(ValueError, match="available indices"):
        ClimateIndices(start="2000-01-01", end="2001-12-31", variables=[])


def test_dict_variables_raises_typeerror() -> None:
    """A mapping variables raises TypeError (G5 — flat list only)."""
    with pytest.raises(TypeError, match="not a mapping"):
        ClimateIndices(
            start="2000-01-01", end="2001-12-31", variables={"oni": []}
        )


def test_unknown_index_did_you_mean(fake_http, tmp_path: Path) -> None:
    """An unknown id surfaces a did-you-mean ValueError at fetch time."""
    source = ClimateIndices(
        start="2000-01-01", end="2001-12-31", variables=["noo"], path=tmp_path
    )
    with pytest.raises(ValueError, match="Did you mean 'nao'"):
        source.download()


def test_fetch_failure_names_index_and_url(monkeypatch, tmp_path: Path) -> None:
    """A 404 raises a ValueError naming the index and URL (G8)."""
    monkeypatch.setattr(backend.requests, "get", _always_404)
    source = ClimateIndices(
        start="2000-01-01", end="2001-12-31", variables=["oni"], path=tmp_path
    )
    with pytest.raises(ValueError, match="oni"):
        source.download()


def test_window_with_no_data_returns_empty_canonical(fake_http, tmp_path: Path) -> None:
    """A window before the series start contributes zero rows, not a crash (G7)."""
    df = ClimateIndices(
        start="1800-01-01",
        end="1801-12-31",
        variables=["oni"],
        path=tmp_path,
    ).download()
    assert list(df.columns) == ["date", "index", "value", "source"]
    assert len(df) == 0


def test_writes_csv_table(fake_http, tmp_path: Path) -> None:
    """download() writes the table to a CSV under the output path."""
    ClimateIndices(
        start="2000-01-01", end="2001-12-31", variables=["oni"], path=tmp_path
    ).download()
    written = list(tmp_path.glob("climate_indices_*.csv"))
    assert len(written) == 1


def test_citation_logged_once(fake_http, tmp_path: Path) -> None:
    """Each source's citation is logged once on download (G6)."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO")
    try:
        ClimateIndices(
            start="2000-01-01",
            end="2001-12-31",
            variables=["oni", "amo"],
            path=tmp_path,
        ).download()
    finally:
        logger.remove(sink_id)
    citation_lines = [m for m in messages if "source citation" in m]
    assert len(citation_lines) == 2


def test_no_xarray_in_subpackage() -> None:
    """The shipped subpackage imports no xarray (G3)."""
    src = Path(backend.__file__).parent
    for py in src.glob("*.py"):
        text = py.read_text()
        assert "import xarray" not in text, py.name
        assert "xr." not in text, py.name
