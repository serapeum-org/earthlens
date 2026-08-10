"""Unit tests for the FLODIS backend (no network — canned CSV fixtures)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from earthlens.flodis import FLODIS, ZenodoRecord
from earthlens.flodis import backend as backend_module

#: A leading unnamed index column (as FLODIS ships) plus the columns the backend
#: reads. Row years span the record so the date filter can be exercised.
DAMAGES_CSV = """\
,ISO3,year,disasterno,total_deaths,total_damages_(000_USD),GFD_matches
0,MOZ,2000.0,2000-0001,700,50000,2
1,MOZ,2007.0,2007-0002,29,12000,1
2,MOZ,2013.0,2013-0003,113,30000,3
3,BGD,2004.0,2004-0004,730,80000,4
"""

DISPLACEMENT_CSV = """\
,ISO3,year,displacements,GID_1,GID_2,num_provinces
0,MOZ,2000.0,500,MOZ.1_1,MOZ.1.1_1,1
1,MOZ,2013.0,1200,MOZ.2_1,MOZ.2.3_1,1
2,BGD,2007.0,3000,BGD.1_1,BGD.1.2_1,1
"""


class _FakeClient:
    """A stand-in HttpClient whose download writes canned bytes to the target."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    def download(self, url: str, local: Path, **kwargs: Any) -> None:
        """Record the call and write the canned CSV to `local`."""
        self.calls.append({"url": url, "local": local, **kwargs})
        Path(local).write_text(self._payload, encoding="utf-8")


def _seed(backend: FLODIS, csv_text: str) -> Path:
    """Write `csv_text` into the backend's pristine-download cache."""
    path = backend._source_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(csv_text, encoding="utf-8")
    return path


def _make(tmp_path: Path, **kwargs: Any) -> FLODIS:
    """Build a FLODIS instance rooted at a temp path."""
    kwargs.setdefault("path", str(tmp_path))
    return FLODIS(**kwargs)


class TestAsList:
    """Tests for the _as_list helper."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, []), ("MOZ", ["MOZ"]), (["A", "B"], ["A", "B"])],
    )
    def test_forms(self, value: Any, expected: list[str]) -> None:
        """None, a bare string and a list each normalise to a list."""
        assert backend_module._as_list(value) == expected


class TestNormalizeIso3:
    """Tests for the _normalize_iso3 helper."""

    def test_none_is_empty(self) -> None:
        """A None country keeps every country (empty set)."""
        assert backend_module._normalize_iso3(None) == set()

    def test_upper_cased_set(self) -> None:
        """Codes are upper-cased and de-duplicated."""
        assert backend_module._normalize_iso3(["moz", "MOZ", "bgd"]) == {"MOZ", "BGD"}

    @pytest.mark.parametrize("bad", ["MO", "MOZA", "M0Z", "12"])
    def test_malformed_raises(self, bad: str) -> None:
        """A code that is not three ASCII letters is rejected."""
        with pytest.raises(ValueError, match="3-letter ISO3"):
            backend_module._normalize_iso3(bad)


class TestNormalizeGid:
    """Tests for the _normalize_gid helper."""

    def test_empty_for_none(self) -> None:
        """A None gid keeps every region (empty set)."""
        row = backend_module.Catalog().dataset("displacement")
        assert backend_module._normalize_gid(None, "displacement", row) == set()

    def test_valid_on_displacement(self) -> None:
        """A gid is accepted for the GADM-keyed displacement table."""
        row = backend_module.Catalog().dataset("displacement")
        assert backend_module._normalize_gid("moz.1_1", "displacement", row) == {
            "MOZ.1_1"
        }

    def test_rejected_on_damages(self) -> None:
        """A gid is rejected for the non-GADM damages table."""
        row = backend_module.Catalog().dataset("damages")
        with pytest.raises(ValueError, match="GADM-keyed"):
            backend_module._normalize_gid("MOZ.1_1", "damages", row)


class TestInit:
    """Tests for FLODIS construction and validation."""

    def test_defaults_to_damages(self, tmp_path: Path) -> None:
        """The default table is damages and the output kind is tabular."""
        backend = _make(tmp_path)
        assert backend._dataset_name == "damages"
        assert backend.OUTPUT_KIND == "tabular"

    def test_unknown_dataset_hints(self, tmp_path: Path) -> None:
        """An unknown table name raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'damages'"):
            _make(tmp_path, dataset="damage")

    def test_variables_rejected(self, tmp_path: Path) -> None:
        """A non-empty variables argument is rejected (no variable axis)."""
        with pytest.raises(ValueError, match="no variable axis"):
            _make(tmp_path, variables=["total_deaths"])

    def test_empty_variables_allowed(self, tmp_path: Path) -> None:
        """An empty variables argument is accepted (the facade passes None/[])."""
        assert _make(tmp_path, variables=[])._dataset_name == "damages"

    def test_country_normalised(self, tmp_path: Path) -> None:
        """The country argument is normalised to an upper-cased set."""
        assert _make(tmp_path, country="moz")._country == {"MOZ"}

    def test_malformed_record_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A catalog whose record failed to load is reported clearly."""

        class _NoRecordCatalog:
            record = None

        monkeypatch.setattr(backend_module, "Catalog", _NoRecordCatalog)
        with pytest.raises(ValueError, match="failed to load its 'record:'"):
            _make(tmp_path)


class TestDates:
    """Tests for the temporal-extent parsing and year range."""

    def test_no_window_is_none(self, tmp_path: Path) -> None:
        """Omitting the window yields a None-dated extent."""
        assert _make(tmp_path)._year_range == (None, None)

    def test_window_year_range(self, tmp_path: Path) -> None:
        """A start/end window resolves to inclusive year bounds."""
        backend = _make(tmp_path, start="2005", end="2018", fmt="%Y")
        assert backend._year_range == (2005, 2018)

    def test_start_after_end_raises(self, tmp_path: Path) -> None:
        """A start later than the end is rejected."""
        with pytest.raises(ValueError):
            _make(tmp_path, start="2018", end="2000", fmt="%Y")


class TestLoadTable:
    """Tests for downloading / reading the selected table."""

    def test_reads_cached_file(self, tmp_path: Path) -> None:
        """A cached CSV is read without any download, index column dropped."""
        backend = _make(tmp_path)
        _seed(backend, DAMAGES_CSV)
        table = backend._load_table()
        assert len(table) == 4
        assert not any(col.startswith("Unnamed") for col in table.columns)
        assert "disasterno" in table.columns

    def test_downloads_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing CSV is fetched via the client with the magic guard."""
        backend = _make(tmp_path)
        client = _FakeClient(DAMAGES_CSV)
        monkeypatch.setattr(backend, "_client", lambda: client)
        table = backend._load_table()
        assert len(table) == 4
        assert client.calls[0]["expect_magic"] == backend_module._CSV_MAGIC


class TestClient:
    """Tests for the pooled HTTP client."""

    def test_client_is_reused(self, tmp_path: Path) -> None:
        """The client is built once and reused on later calls."""
        backend = _make(tmp_path)
        assert backend._client() is backend._client()


class TestFilterTable:
    """Tests for the country / gid / date filters."""

    def test_country_filter(self, tmp_path: Path) -> None:
        """A country restriction keeps only that ISO3's rows."""
        backend = _make(tmp_path, country="MOZ")
        _seed(backend, DAMAGES_CSV)
        table = backend._filter_table(backend._load_table())
        assert set(table["ISO3"]) == {"MOZ"}
        assert len(table) == 3

    def test_year_bounds(self, tmp_path: Path) -> None:
        """A start/end window keeps only rows within the year bounds."""
        backend = _make(tmp_path, start="2005", end="2018", fmt="%Y")
        _seed(backend, DAMAGES_CSV)
        table = backend._filter_table(backend._load_table())
        assert sorted(table["year"]) == [2007.0, 2013.0]

    def test_open_start_only(self, tmp_path: Path) -> None:
        """An end-only window keeps rows up to and including that year."""
        backend = _make(tmp_path, end="2004", fmt="%Y")
        _seed(backend, DAMAGES_CSV)
        table = backend._filter_table(backend._load_table())
        assert sorted(table["year"]) == [2000.0, 2004.0]

    def test_gid_matches_gid1_or_gid2(self, tmp_path: Path) -> None:
        """A gid restriction matches either the GID_1 or GID_2 column."""
        backend = _make(tmp_path, dataset="displacement", gid=["MOZ.1_1", "MOZ.2.3_1"])
        _seed(backend, DISPLACEMENT_CSV)
        table = backend._filter_table(backend._load_table())
        assert sorted(table["displacements"]) == [500, 1200]

    def test_no_filter_keeps_all(self, tmp_path: Path) -> None:
        """An unfiltered request keeps every row."""
        backend = _make(tmp_path)
        _seed(backend, DAMAGES_CSV)
        assert len(backend._filter_table(backend._load_table())) == 4

    def test_gid_skips_absent_column(self, tmp_path: Path) -> None:
        """A gid filter tolerates a table that lacks one of the GID columns."""
        backend = _make(tmp_path, dataset="displacement", gid=["MOZ.1_1"])
        _seed(backend, ",ISO3,year,displacements,GID_1\n0,MOZ,2000.0,500,MOZ.1_1\n")
        table = backend._filter_table(backend._load_table())
        assert list(table["displacements"]) == [500]


class TestSearchFetchApi:
    """Tests for the search / fetch / api composition."""

    def test_search_pins_one_product(self, tmp_path: Path) -> None:
        """_search pins one product carrying the record id and file name."""
        backend = _make(tmp_path, dataset="displacement")
        products = backend._search()
        assert len(products) == 1
        assert products[0].id == "flodis:displacement"
        assert products[0].metadata["file"] == "FLODIS_displacement.csv"

    def test_fetch_returns_filtered_frame(self, tmp_path: Path) -> None:
        """_fetch downloads and filters into a single DataFrame."""
        backend = _make(tmp_path, country="BGD")
        _seed(backend, DAMAGES_CSV)
        (frame,) = backend._fetch(backend._search())
        assert list(frame["ISO3"]) == ["BGD"]


class TestDownload:
    """Tests for the public download entry point."""

    def test_returns_and_writes_csv(self, tmp_path: Path) -> None:
        """download returns the frame and writes a CSV with no index column."""
        backend = _make(tmp_path, country="MOZ")
        _seed(backend, DAMAGES_CSV)
        frame = backend.download(progress_bar=False)
        assert len(frame) == 3
        written = list(backend.root_dir.glob("flodis_damages-*.csv"))
        assert written, "a filtered CSV should be written"
        reloaded = pd.read_csv(written[0])
        assert not any(col.startswith("Unnamed") for col in reloaded.columns)

    def test_unfiltered_plain_name(self, tmp_path: Path) -> None:
        """An unfiltered request writes the plain flodis_<table>.csv name."""
        backend = _make(tmp_path)
        _seed(backend, DAMAGES_CSV)
        backend.download(progress_bar=False)
        assert (backend.root_dir / "flodis_damages.csv").exists()

    def test_output_does_not_case_collide_with_source_cache(
        self, tmp_path: Path
    ) -> None:
        """The written CSV never shares a case-insensitive path with the raw cache."""
        backend = _make(tmp_path, dataset="displacement")
        source = os.path.normcase(str(backend._source_path()))
        output = os.path.normcase(
            str(backend.root_dir / (backend._result_stem() + ".csv"))
        )
        assert source != output, (
            "raw cache and output must not collide on Windows/macOS"
        )

    def test_repeated_unfiltered_displacement_download(self, tmp_path: Path) -> None:
        """Downloading the displacement table twice keeps the schema intact.

        Regression for the case-insensitive collision that overwrote the raw
        cache with the index-stripped output and dropped `ISO3` on re-read.
        """
        first = _make(tmp_path, dataset="displacement")
        _seed(first, DISPLACEMENT_CSV)
        first.download(progress_bar=False)

        second = _make(tmp_path, dataset="displacement")
        reread = second.download(progress_bar=False)
        assert "ISO3" in reread.columns
        assert not any(col.startswith("Unnamed") for col in reread.columns)
        assert len(reread) == 3


class TestResultStem:
    """Tests for the output-file stem builder."""

    def test_plain_when_unfiltered(self, tmp_path: Path) -> None:
        """An unfiltered request yields the plain stem."""
        assert _make(tmp_path)._result_stem() == "flodis_damages"

    def test_digest_when_filtered(self, tmp_path: Path) -> None:
        """A filtered request appends a stable digest to the stem."""
        stem = _make(
            tmp_path, country="MOZ", start="2000", end="2018", fmt="%Y"
        )._result_stem()
        assert (
            stem.startswith("flodis_damages-")
            and len(stem) == len("flodis_damages-") + 8
        )


class TestLogCitation:
    """Tests for the citation logging."""

    def test_logs_when_attribution_present(self, tmp_path: Path) -> None:
        """A record with attribution emits its citation line."""
        backend = _make(tmp_path)
        messages: list[str] = []
        sink_id = backend_module.logger.add(messages.append, format="{message}")
        try:
            backend._log_citation()
        finally:
            backend_module.logger.remove(sink_id)
        assert any("FLODIS source citation" in message for message in messages)

    def test_silent_when_attribution_empty(self, tmp_path: Path) -> None:
        """A record with no attribution emits no citation line."""
        backend = _make(tmp_path)
        backend._record = ZenodoRecord(record=1, attribution="")
        messages: list[str] = []
        sink_id = backend_module.logger.add(messages.append, format="{message}")
        try:
            backend._log_citation()
        finally:
            backend_module.logger.remove(sink_id)
        assert not any("FLODIS source citation" in message for message in messages)
