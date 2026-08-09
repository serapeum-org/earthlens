"""Unit tests for the NSI backend (offline, faked transport)."""

from __future__ import annotations

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.nsi import NSI

from .conftest import EMPTY_GEOJSON, _FakeSession, make_nfip_records

pytestmark = pytest.mark.nsi

BOX = {"lat_lim": [29.95, 29.96], "lon_lim": [-90.07, -90.06]}


@pytest.mark.unit
class TestConstruction:
    """Constructor validation and per-instance OUTPUT_KIND."""

    def test_output_kind_structures_vector(self, tmp_path) -> None:
        """A structures instance is vector."""
        b = NSI(source="structures", fips="22071012700", path=tmp_path)
        assert b.OUTPUT_KIND == "vector"

    def test_output_kind_nfhl_vector(self, tmp_path) -> None:
        """An nfhl instance is vector."""
        assert NSI(source="nfhl", path=tmp_path, **BOX).OUTPUT_KIND == "vector"

    def test_output_kind_nfip_tabular(self, tmp_path) -> None:
        """An nfip instance is tabular."""
        assert (
            NSI(source="nfip", county="22071", path=tmp_path).OUTPUT_KIND == "tabular"
        )

    def test_unknown_source_raises(self, tmp_path) -> None:
        """An unknown source key is rejected by the catalog."""
        with pytest.raises(ValueError):
            NSI(source="buildings", fips="22071", path=tmp_path)

    def test_bad_output_format_raises(self, tmp_path) -> None:
        """An unrecognised output_format is rejected."""
        with pytest.raises(ValueError):
            NSI(source="nfip", county="22071", output_format="xlsx", path=tmp_path)


@pytest.mark.unit
class TestBoundGuard:
    """The required-bound guard (`G3`)."""

    def test_structures_without_bound_raises(self, tmp_path) -> None:
        """structures with neither fips nor box is refused."""
        with pytest.raises(ValueError, match="fips"):
            NSI(source="structures", path=tmp_path)

    def test_nfhl_without_box_raises(self, tmp_path) -> None:
        """nfhl without a box is refused."""
        with pytest.raises(ValueError, match="box"):
            NSI(source="nfhl", path=tmp_path)

    def test_nfip_without_filter_raises(self, tmp_path) -> None:
        """nfip with no attribute selector is refused."""
        with pytest.raises(ValueError, match="state|county|year"):
            NSI(source="nfip", path=tmp_path)

    def test_malformed_box_raises_at_construction(self, tmp_path) -> None:
        """An inverted box is rejected at construction, not deep in download()."""
        with pytest.raises(ValueError, match="min <= max"):
            NSI(
                source="nfhl",
                lat_lim=[30.0, 29.0],
                lon_lim=[-90.1, -90.0],
                path=tmp_path,
            )

    def test_malformed_fips_raises(self, tmp_path) -> None:
        """A fips that is not a 2/5/11/15-digit code is rejected."""
        with pytest.raises(ValueError, match="2/5/11/15-digit"):
            NSI(source="structures", fips="220710", path=tmp_path)

    def test_nfip_box_is_ignored(self, tmp_path) -> None:
        """A (valid) box passed to nfip is ignored, not a spatial filter."""
        client = _FakeSession(nfip_records=make_nfip_records(2))
        df = NSI(
            source="nfip",
            county="22071",
            lat_lim=[29.9, 30.0],
            lon_lim=[-90.1, -90.0],
            session=client,
            path=tmp_path,
        ).download()
        assert len(df) == 2

    def test_structures_fips_and_box_uses_fips(self, fake_session, tmp_path) -> None:
        """Given both fips and a box, structures uses fips (GET), ignoring the box."""
        fc = NSI(
            source="structures",
            fips="22071012700",
            lat_lim=[29.9, 30.0],
            lon_lim=[-90.1, -90.0],
            session=fake_session,
            path=tmp_path,
        ).download()
        assert len(fc) == 2
        assert fake_session.calls[-1]["method"] == "GET"


@pytest.mark.unit
class TestStructures:
    """The NSI structures source."""

    def test_fips_uses_get_with_fips_param(self, fake_session, tmp_path) -> None:
        """A fips request GETs the endpoint with a `fips` query param."""
        fc = NSI(
            source="structures",
            fips="22071012700",
            session=fake_session,
            path=tmp_path,
        ).download()
        assert isinstance(fc, FeatureCollection)
        assert len(fc) == 2
        assert fake_session.calls[-1] == {
            "method": "GET",
            "url": "https://nsi.sec.usace.army.mil/nsiapi/structures",
            "params": {"fips": "22071012700"},
        }

    def test_box_uses_post_polygon_body(self, fake_session, tmp_path) -> None:
        """A box request POSTs a GeoJSON polygon body (the ?bbox= repair)."""
        fc = NSI(
            source="structures", session=fake_session, path=tmp_path, **BOX
        ).download()
        assert isinstance(fc, FeatureCollection)
        post = fake_session.calls[-1]
        assert post["method"] == "POST"
        assert post["json"]["features"][0]["geometry"]["type"] == "Polygon"

    def test_state_fips_warns_but_returns(self, fake_session, tmp_path) -> None:
        """A 2-digit (whole-state) FIPS warns but still returns the features."""
        fc = NSI(
            source="structures", fips="22", session=fake_session, path=tmp_path
        ).download()
        assert isinstance(fc, FeatureCollection)
        assert len(fc) == 2

    def test_foreign_box_returns_empty(self, tmp_path) -> None:
        """A non-US box returns an empty collection, not an error (`G4`)."""
        client = _FakeSession(structures=EMPTY_GEOJSON)
        fc = NSI(
            source="structures",
            lat_lim=[30.0, 30.1],
            lon_lim=[31.2, 31.3],
            session=client,
            path=tmp_path,
        ).download()
        assert isinstance(fc, FeatureCollection)
        assert len(fc) == 0


@pytest.mark.unit
class TestNfhl:
    """The FEMA NFHL source (canned fixture; live blocked in this env)."""

    def test_query_url_and_envelope(self, fake_session, tmp_path) -> None:
        """nfhl GETs the layer's /query with an esri envelope."""
        fc = NSI(source="nfhl", session=fake_session, path=tmp_path, **BOX).download()
        assert isinstance(fc, FeatureCollection)
        assert len(fc) == 2
        call = fake_session.calls[-1]
        assert call["url"].endswith("/MapServer/28/query")
        assert call["params"]["geometryType"] == "esriGeometryEnvelope"


@pytest.mark.unit
class TestNfip:
    """The FEMA NFIP claims source."""

    def test_download_progress_bar_is_a_noop(self, tmp_path) -> None:
        """`progress_bar=` is accepted and does not change the result."""
        client = _FakeSession(nfip_records=make_nfip_records(3))
        df = NSI(source="nfip", county="22071", session=client, path=tmp_path).download(
            progress_bar=False
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3

    def test_download_returns_friendly_dataframe(self, tmp_path) -> None:
        """nfip returns a DataFrame with the friendly column names."""
        client = _FakeSession(nfip_records=make_nfip_records(5))
        df = NSI(
            source="nfip", county="22071", session=client, path=tmp_path
        ).download()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 5
        assert "building_paid" in df.columns
        assert "rated_flood_zone" in df.columns

    def test_pagination_and_cap(self, tmp_path) -> None:
        """nfip pages the endpoint and honours max_records."""
        client = _FakeSession(nfip_records=make_nfip_records(100))
        df = NSI(
            source="nfip",
            county="22071",
            max_records=25,
            session=client,
            path=tmp_path,
        ).download()
        assert len(df) == 25

    def test_writes_csv(self, tmp_path) -> None:
        """A tabular result is written to root_dir as CSV."""
        client = _FakeSession(nfip_records=make_nfip_records(3))
        NSI(source="nfip", county="22071", session=client, path=tmp_path).download()
        assert (tmp_path / "nsi_nfip.csv").exists()

    def test_writes_parquet(self, tmp_path) -> None:
        """`output_format='parquet'` writes a parquet file."""
        pytest.importorskip("pyarrow")
        client = _FakeSession(nfip_records=make_nfip_records(3))
        NSI(
            source="nfip",
            county="22071",
            output_format="parquet",
            session=client,
            path=tmp_path,
        ).download()
        assert (tmp_path / "nsi_nfip.parquet").exists()

    def test_empty_result_writes_schema_only(self, tmp_path) -> None:
        """No matching claims still writes an empty schema-only table."""
        client = _FakeSession(nfip_records=[])
        df = NSI(source="nfip", year=1900, session=client, path=tmp_path).download()
        assert df.empty
        assert (tmp_path / "nsi_nfip.csv").exists()

    def test_large_uncapped_pull_still_returns(self, tmp_path) -> None:
        """A large uncapped match count warns but still returns the fetched rows."""
        client = _FakeSession(nfip_records=make_nfip_records(3), nfip_total=60_000)
        df = NSI(source="nfip", state="LA", session=client, path=tmp_path).download()
        assert len(df) == 3


@pytest.mark.unit
class TestAggregate:
    """`aggregate=` rejection."""

    def test_download_rejects_aggregate(self, fake_session, tmp_path) -> None:
        """Passing aggregate= is refused for the record-shaped backend."""
        b = NSI(
            source="structures",
            fips="22071012700",
            session=fake_session,
            path=tmp_path,
        )
        with pytest.raises(NotImplementedError):
            b.download(aggregate=object())
