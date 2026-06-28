"""Unit + integration tests for the NREL backend (`earthlens.nrel.backend`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.nrel import NREL
from earthlens.nrel.auth import AuthenticationError

from .conftest import FakeResponse

pytestmark = pytest.mark.nrel


def _nrel(nsrdb_csv: str, bind_session, **kwargs) -> tuple[NREL, object]:
    """Build an NREL backend bound to a fake session replaying the NSRDB CSV."""
    session = bind_session(FakeResponse(text=nsrdb_csv))
    defaults = dict(
        start="2020-01-01",
        end="2020-01-01",
        variables=["ghi", "dni"],
        point=(39.74, -105.18),
        api_key="DUMMYKEY",
        email="me@example.com",
    )
    defaults.update(kwargs)
    return NREL(**defaults), session


class TestSinglePoint:
    """Tests for a single-point download."""

    def test_single_point_returns_tagged_frame(self, nsrdb_csv, bind_session, tmp_path):
        """A single point yields the data rows tagged with lat/lon/year/product."""
        backend, session = _nrel(nsrdb_csv, bind_session, path=tmp_path)
        df = backend.download(progress_bar=False)
        assert len(df) == 3
        assert df["lat"].unique().tolist() == [39.74]
        assert df["lon"].unique().tolist() == [-105.18]
        assert df["year"].unique().tolist() == [2020]
        assert df["product"].unique().tolist() == ["nsrdb-psm3"]
        assert len(session.calls) == 1

    def test_url_carries_credentials_and_point(self, nsrdb_csv, bind_session, tmp_path):
        """The issued URL carries the resolved key, email, and WKT point."""
        backend, session = _nrel(nsrdb_csv, bind_session, path=tmp_path)
        backend.download(progress_bar=False)
        url = session.calls[0]
        assert "api_key=DUMMYKEY" in url
        assert "email=me%40example.com" in url
        assert "POINT%28-105.18+39.74%29" in url

    def test_empty_and_populated_share_a_schema(
        self, nsrdb_csv, bind_session, tmp_path
    ):
        """An all-skipped (empty) result has the same columns as a populated one."""
        bind_session(FakeResponse(text=nsrdb_csv))
        populated = NREL(
            start="2020-01-01",
            end="2020-01-01",
            variables=[],
            point=(39.74, -105.18),
            api_key="DUMMYKEY",
            email="me@example.com",
            path=tmp_path,
        ).download(progress_bar=False)
        bind_session(
            FakeResponse(text="x", status_code=400, payload={"errors": ["nope"]})
        )
        empty = NREL(
            start="2020-01-01",
            end="2020-01-01",
            variables=[],
            lat_lim=[10.0, 11.0],
            lon_lim=[10.0, 11.0],
            spacing_deg=1.0,
            api_key="DUMMYKEY",
            email="me@example.com",
            path=tmp_path,
        ).download(progress_bar=False)
        assert len(empty) == 0
        assert list(populated.columns) == list(empty.columns), (
            f"schema drift: populated={list(populated.columns)} "
            f"empty={list(empty.columns)}"
        )
        assert list(populated.columns)[0] == "time"

    def test_writes_csv_table(self, nsrdb_csv, bind_session, tmp_path):
        """download() writes a CSV table under the output directory."""
        backend, _ = _nrel(nsrdb_csv, bind_session, path=tmp_path)
        backend.download(progress_bar=False)
        assert (tmp_path / "nrel_nsrdb-psm3.csv").exists()

    def test_attributes_default_to_product_when_unset(
        self, nsrdb_csv, bind_session, tmp_path
    ):
        """An empty variables list falls back to the product's default attributes."""
        backend, session = _nrel(nsrdb_csv, bind_session, variables=[], path=tmp_path)
        backend.download(progress_bar=False)
        assert "attributes=ghi%2Cdni%2Cdhi" in session.calls[0]


class TestFanOut:
    """Tests for the point x year fan-out and its guard."""

    def test_bbox_one_get_per_grid_point(self, nsrdb_csv, bind_session, tmp_path):
        """A 2x2-degree bbox at 1 degree issues one GET per grid point."""
        session = bind_session(FakeResponse(text=nsrdb_csv))
        backend = NREL(
            start="2020-01-01",
            end="2020-01-01",
            variables=["ghi"],
            lat_lim=[39.0, 40.0],
            lon_lim=[-106.0, -105.0],
            spacing_deg=1.0,
            api_key="DUMMYKEY",
            email="me@example.com",
            path=tmp_path,
        )
        df = backend.download(progress_bar=False)
        assert len(session.calls) == 4
        assert len(df) == 12

    def test_multi_year_one_get_per_year(self, nsrdb_csv, bind_session, tmp_path):
        """A two-year window for one point issues one GET per year."""
        backend, session = _nrel(
            nsrdb_csv, bind_session, start="2019-01-01", end="2020-12-31", path=tmp_path
        )
        backend.download(progress_bar=False)
        names = [c.split("names=")[1].split("&")[0] for c in session.calls]
        assert sorted(names) == ["2019", "2020"]

    def test_tmy_product_single_call(self, nsrdb_csv, bind_session, tmp_path):
        """The TMY product collapses any year window to a single names=tmy call."""
        backend, session = _nrel(
            nsrdb_csv,
            bind_session,
            product="nsrdb-tmy",
            start="2010-01-01",
            end="2020-12-31",
            path=tmp_path,
        )
        df = backend.download(progress_bar=False)
        assert len(session.calls) == 1
        assert "names=tmy" in session.calls[0]
        assert df["year"].unique().tolist() == ["tmy"]

    def test_over_cap_raises(self, nsrdb_csv, bind_session, tmp_path):
        """A fan-out beyond max_requests raises ValueError before fetching."""
        backend, _ = _nrel(
            nsrdb_csv,
            bind_session,
            point=None,
            lat_lim=[0.0, 10.0],
            lon_lim=[0.0, 10.0],
            spacing_deg=1.0,
            max_requests=50,
            path=tmp_path,
        )
        with pytest.raises(ValueError, match="max_requests"):
            backend.download(progress_bar=False)

    def test_large_fan_out_warns(
        self, nsrdb_csv, bind_session, tmp_path, loguru_messages
    ):
        """A fan-out past the soft threshold logs a warning but proceeds."""
        backend, _ = _nrel(
            nsrdb_csv,
            bind_session,
            point=None,
            lat_lim=[0.0, 10.0],
            lon_lim=[0.0, 9.0],
            spacing_deg=1.0,
            path=tmp_path,
        )
        backend.download(progress_bar=False)
        assert any("keyed CSV calls" in m for m in loguru_messages)


class TestAggregateRejection:
    """Tests that the tabular backend rejects a gridded reduction."""

    def test_aggregate_raises_not_implemented(self, nsrdb_csv, bind_session, tmp_path):
        """A non-None aggregate= raises NotImplementedError."""
        backend, _ = _nrel(nsrdb_csv, bind_session, path=tmp_path)
        with pytest.raises(NotImplementedError, match="aggregate"):
            backend.download(aggregate=object())


class TestCoverage:
    """Tests for the out-of-coverage policy."""

    def _err_session(self, bind_session):
        """Bind a session that always returns a 400 error body."""
        return bind_session(
            FakeResponse(
                text='{"errors":["bad coords"]}',
                status_code=400,
                payload={"errors": ["bad coords"]},
            )
        )

    def test_single_point_out_of_coverage_raises(self, bind_session, tmp_path):
        """A single explicit out-of-coverage point raises ValueError naming it."""
        self._err_session(bind_session)
        backend = NREL(
            start="2020-01-01",
            end="2020-01-01",
            variables=["ghi"],
            point=(89.0, 0.0),
            api_key="DUMMYKEY",
            email="me@example.com",
            path=tmp_path,
        )
        with pytest.raises(ValueError, match="lat=89.0"):
            backend.download(progress_bar=False)

    def test_bbox_skips_out_of_coverage_points(
        self, bind_session, tmp_path, loguru_messages
    ):
        """A multi-point bbox skips out-of-coverage points and returns empty."""
        self._err_session(bind_session)
        backend = NREL(
            start="2020-01-01",
            end="2020-01-01",
            variables=["ghi"],
            lat_lim=[88.0, 89.0],
            lon_lim=[0.0, 1.0],
            spacing_deg=1.0,
            api_key="DUMMYKEY",
            email="me@example.com",
            path=tmp_path,
        )
        df = backend.download(progress_bar=False)
        assert len(df) == 0
        assert {"lat", "lon", "year", "product"}.issubset(df.columns)
        assert any("skipped point" in m for m in loguru_messages)


class TestErrorMessage:
    """Tests for `_error_message` extraction."""

    def test_extracts_json_errors(self):
        """A JSON error body surfaces its `errors` list."""
        resp = FakeResponse(text="x", status_code=400, payload={"errors": ["nope"]})
        assert "nope" in NREL._error_message(resp)

    def test_falls_back_to_text(self):
        """A non-JSON body falls back to the truncated text."""
        resp = FakeResponse(text="plain error", status_code=500)
        assert NREL._error_message(resp) == "plain error"

    def test_dict_without_errors_key_stringifies(self):
        """A JSON dict with no errors/error key stringifies the payload."""
        resp = FakeResponse(text="x", status_code=400, payload={"foo": "bar"})
        assert "foo" in NREL._error_message(resp)

    def test_non_dict_json_stringifies(self):
        """A non-dict JSON body (e.g. a list) stringifies the payload."""
        resp = FakeResponse(text="x", status_code=400, payload=["boom"])
        assert "boom" in NREL._error_message(resp)


class TestParquet:
    """Tests for the parquet output path."""

    def test_writes_parquet(self, nsrdb_csv, bind_session, tmp_path):
        """output_format='parquet' writes a parquet table when pyarrow is present."""
        pytest.importorskip("pyarrow")
        backend, _ = _nrel(
            nsrdb_csv, bind_session, output_format="parquet", path=tmp_path
        )
        backend.download(progress_bar=False)
        assert (tmp_path / "nrel_nsrdb-psm3.parquet").exists()


class TestConstructorValidation:
    """Tests for constructor-time validation."""

    def test_variables_mapping_raises_type_error(self, nrel_env):
        """A mapping for variables raises TypeError."""
        with pytest.raises(TypeError, match="list of attribute names"):
            NREL(
                start="2020-01-01",
                end="2020-01-01",
                variables={"a": ["b"]},
                point=(1.0, 2.0),
            )

    def test_variables_bare_string_raises_type_error(self, nrel_env):
        """A bare-string variables raises instead of splitting into characters."""
        with pytest.raises(TypeError, match="single-character"):
            NREL(
                start="2020-01-01",
                end="2020-01-01",
                variables="ghi",
                point=(1.0, 2.0),
            )

    def test_malformed_point_raises_value_error(self, nrel_env):
        """A point that is not a 2-tuple raises a clear ValueError."""
        with pytest.raises(ValueError, match="must be a 2-tuple"):
            NREL(
                start="2020-01-01",
                end="2020-01-01",
                variables=["ghi"],
                point=(1.0,),
            )

    def test_invalid_output_format_raises(self, nrel_env):
        """An unrecognised output_format raises ValueError."""
        with pytest.raises(ValueError, match="output_format"):
            NREL(
                start="2020-01-01",
                end="2020-01-01",
                variables=["ghi"],
                point=(1.0, 2.0),
                output_format="zarr",
            )

    def test_missing_location_raises(self, nrel_env):
        """No point and no bbox raises ValueError."""
        with pytest.raises(ValueError, match="needs a location"):
            NREL(start="2020-01-01", end="2020-01-01", variables=["ghi"])

    def test_missing_credentials_raise_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Construction without any credentials raises AuthenticationError."""
        monkeypatch.delenv("NREL_API_KEY", raising=False)
        monkeypatch.delenv("NREL_EMAIL", raising=False)
        with pytest.raises(AuthenticationError):
            NREL(
                start="2020-01-01",
                end="2020-01-01",
                variables=["ghi"],
                point=(1.0, 2.0),
            )

    def test_point_overrides_bbox(self, nrel_env):
        """An explicit point collapses any bbox to that single coordinate."""
        backend = NREL(
            start="2020-01-01",
            end="2020-01-01",
            variables=["ghi"],
            lat_lim=[0.0, 50.0],
            lon_lim=[0.0, 50.0],
            point=(39.74, -105.18),
        )
        assert backend.space.south == 39.74 and backend.space.north == 39.74

    def test_interval_override_reaches_url(self, nsrdb_csv, bind_session, tmp_path):
        """An explicit interval is sent on the request URL."""
        backend, session = _nrel(nsrdb_csv, bind_session, interval=30, path=tmp_path)
        backend.download(progress_bar=False)
        assert "interval=30" in session.calls[0]


class TestNoLeak:
    """Meta-tests guarding the xarray/rex-free + key-redaction rules."""

    SRC = Path(__file__).resolve().parents[2] / "src" / "earthlens" / "nrel"
    FIXTURES = Path(__file__).parent / "fixtures"

    def test_no_array_archive_imports(self):
        """The backend source never imports xarray / rex / h5pyd."""
        for py in self.SRC.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for banned in ("import xarray", "import rex", "h5pyd", "import xr"):
                assert banned not in text, f"{banned} found in {py.name}"

    def test_no_key_in_fixtures(self):
        """No real-looking API key is baked into the CSV fixtures."""
        for csv in self.FIXTURES.glob("*.csv"):
            text = csv.read_text(encoding="utf-8")
            assert "api_key" not in text.lower()
