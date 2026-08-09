"""Unit tests for the NSI OpenFEMA query/parse helpers."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.nsi._helpers import (
    nfip_count,
    odata_filter,
    paginate_nfip,
    records_to_frame,
)

from .conftest import _FakeSession, make_client, make_nfip_records

pytestmark = pytest.mark.nsi

ENDPOINT = "https://www.fema.gov/api/open/v3/NfipClaims"
FIELD_MAP = {
    "claim_id": "id",
    "paid": "amountPaidOnBuildingClaim",
    "zone": "floodZoneCurrent",
}


@pytest.mark.unit
class TestOdataFilter:
    """`odata_filter` clause building."""

    def test_none_when_no_selector(self) -> None:
        """No selector yields no filter."""
        assert odata_filter() is None

    def test_string_and_numeric_clauses(self) -> None:
        """Strings are quoted; the year is bare; clauses join with `and`."""
        assert (
            odata_filter(state="LA", county="22071", year=2005)
            == "state eq 'LA' and countyCode eq '22071' and yearOfLoss eq 2005"
        )

    def test_flood_event_quote_escaped(self) -> None:
        """An embedded single quote is doubled per OData."""
        assert odata_filter(flood_event="O'Brien") == "floodEvent eq 'O''Brien'"


@pytest.mark.unit
class TestPagination:
    """`paginate_nfip` paging and capping."""

    def test_collects_all_pages(self) -> None:
        """Paging walks `$skip` until a short page ends it."""
        session = _FakeSession(nfip_records=make_nfip_records(25))
        rows = paginate_nfip(
            make_client(session),
            ENDPOINT,
            "NfipClaims",
            filter_str="x eq 1",
            page_size=10,
        )
        assert len(rows) == 25
        skips = [c["params"]["$skip"] for c in session.calls]
        assert skips == [0, 10, 20]

    def test_max_records_caps_total(self) -> None:
        """`max_records` stops paging early and caps the last `$top`."""
        session = _FakeSession(nfip_records=make_nfip_records(100))
        rows = paginate_nfip(
            make_client(session),
            ENDPOINT,
            "NfipClaims",
            filter_str=None,
            page_size=10,
            max_records=15,
        )
        assert len(rows) == 15
        assert session.calls[-1]["params"]["$top"] == 5

    def test_select_forwarded(self) -> None:
        """A `$select` projection reaches the query params."""
        session = _FakeSession(nfip_records=make_nfip_records(3))
        paginate_nfip(
            make_client(session),
            ENDPOINT,
            "NfipClaims",
            filter_str=None,
            page_size=10,
            select="id,yearOfLoss",
        )
        assert session.calls[0]["params"]["$select"] == "id,yearOfLoss"


@pytest.mark.unit
class TestNfipCount:
    """`nfip_count` reads the inline count."""

    def test_reads_metadata_count(self) -> None:
        """The total comes from `metadata.count` under `$inlinecount`."""
        session = _FakeSession(nfip_records=make_nfip_records(2), nfip_total=127250)
        assert nfip_count(make_client(session), ENDPOINT, filter_str="x eq 1") == 127250
        assert session.calls[0]["params"]["$inlinecount"] == "allpages"


@pytest.mark.unit
class TestRecordsToFrame:
    """`records_to_frame` mapping and empty handling."""

    def test_no_map_returns_raw_frame(self) -> None:
        """Without a field map every provider column survives."""
        frame = records_to_frame(make_nfip_records(2))
        assert isinstance(frame, pd.DataFrame)
        assert "amountPaidOnBuildingClaim" in frame.columns

    def test_map_renames_and_orders(self) -> None:
        """With a field map the friendly names appear in map order."""
        frame = records_to_frame(make_nfip_records(2), FIELD_MAP)
        assert list(frame.columns) == ["claim_id", "paid", "zone"]
        assert len(frame) == 2

    def test_empty_records_yield_schema_only_frame(self) -> None:
        """No records yields an empty frame with the friendly columns."""
        frame = records_to_frame([], FIELD_MAP)
        assert list(frame.columns) == ["claim_id", "paid", "zone"]
        assert frame.empty
