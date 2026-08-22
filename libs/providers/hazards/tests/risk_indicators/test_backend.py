"""Unit + integration tests for the RiskIndicators backend routing."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from loguru import logger
from pyramids.feature.collection import FeatureCollection

import earthlens.risk_indicators
from earthlens.risk_indicators import AuthenticationError, RiskIndicators, _helpers

pytestmark = pytest.mark.risk_indicators

SUBPACKAGE = Path(earthlens.risk_indicators.__file__).parent


def _build(monkeypatch, **kwargs):
    """Construct a backend with GFW_API_KEY removed from the environment."""
    monkeypatch.delenv("GFW_API_KEY", raising=False)
    return RiskIndicators(**kwargs)


class TestConstruction:
    """Constructor validation and per-instance OUTPUT_KIND."""

    def test_output_kind_tabular(self, monkeypatch, tmp_path):
        """A ThinkHazard dataset sets OUTPUT_KIND to tabular."""
        b = _build(
            monkeypatch,
            variables=["thinkhazard:flood_river"],
            country="KEN",
            path=tmp_path,
        )
        assert b.OUTPUT_KIND == "tabular"

    def test_output_kind_vector(self, monkeypatch, tmp_path):
        """A GFW geometry dataset sets OUTPUT_KIND to vector."""
        b = _build(
            monkeypatch,
            variables=["gfw:admin_boundary"],
            country="KEN",
            api_key="k",
            path=tmp_path,
        )
        assert b.OUTPUT_KIND == "vector"

    def test_non_gfw_builds_no_auth(self, monkeypatch, tmp_path):
        """A ThinkHazard / INFORM request never constructs GfwAuth."""
        th = _build(
            monkeypatch,
            variables=["thinkhazard:flood_river"],
            country="KEN",
            path=tmp_path,
        )
        inf = _build(
            monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path
        )
        assert th._auth is None and inf._auth is None

    def test_gfw_builds_auth(self, monkeypatch, tmp_path):
        """A GFW request builds and configures a GfwAuth."""
        b = _build(
            monkeypatch,
            variables=["gfw:tree_cover_loss"],
            country="KEN",
            api_key="k",
            path=tmp_path,
        )
        assert b._auth is not None and b._auth.is_authenticated()

    def test_gfw_without_key_raises(self, monkeypatch, tmp_path):
        """A GFW request with no key raises AuthenticationError naming GFW_API_KEY."""
        with pytest.raises(AuthenticationError, match="GFW_API_KEY"):
            _build(
                monkeypatch,
                variables=["gfw:tree_cover_loss"],
                country="KEN",
                path=tmp_path,
            )

    def test_variables_must_be_one_id(self, monkeypatch, tmp_path):
        """Zero or many dataset ids are rejected."""
        with pytest.raises(ValueError, match="exactly one dataset id"):
            _build(monkeypatch, variables=[], country="KEN", path=tmp_path)
        with pytest.raises(ValueError, match="exactly one dataset id"):
            _build(
                monkeypatch,
                variables=["inform:risk", "inform:vulnerability"],
                country="KEN",
                path=tmp_path,
            )

    def test_variables_mapping_rejected(self, monkeypatch, tmp_path):
        """A mapping variables= is a TypeError."""
        with pytest.raises(TypeError, match="not a mapping"):
            _build(
                monkeypatch, variables={"inform:risk": []}, country="KEN", path=tmp_path
            )

    def test_bad_output_format_rejected(self, monkeypatch, tmp_path):
        """An unknown output_format is rejected."""
        with pytest.raises(ValueError, match="output_format"):
            _build(
                monkeypatch,
                variables=["inform:risk"],
                country="KEN",
                output_format="xml",
                path=tmp_path,
            )

    def test_thinkhazard_missing_selector_rejected(self, monkeypatch, tmp_path):
        """A ThinkHazard request without country/admin_code is rejected."""
        with pytest.raises(ValueError, match="country="):
            _build(monkeypatch, variables=["thinkhazard:flood_river"], path=tmp_path)

    def test_gfw_missing_country_rejected(self, monkeypatch, tmp_path):
        """A GFW request without country is rejected."""
        with pytest.raises(ValueError, match="country="):
            _build(
                monkeypatch,
                variables=["gfw:tree_cover_loss"],
                api_key="k",
                path=tmp_path,
            )

    def test_gfw_non_iso3_country_rejected(self, monkeypatch, tmp_path):
        """A GFW request whose country is not a 3-letter ISO3 is rejected."""
        with pytest.raises(ValueError, match="ISO3"):
            _build(
                monkeypatch,
                variables=["gfw:tree_cover_loss"],
                country="Kenya",
                api_key="k",
                path=tmp_path,
            )


class TestGridAndDates:
    """The sentinel extent and tolerant date handling."""

    def test_create_grid_is_global(self, monkeypatch, tmp_path):
        """_create_grid returns a whole-globe extent."""
        b = _build(monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path)
        assert b.space.west == -180.0 and b.space.north == 90.0

    def test_none_dates_allowed(self, monkeypatch, tmp_path):
        """A request with no start/end builds a None-dated extent."""
        b = _build(monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path)
        assert b.time.start_date is None and len(b.time.dates) == 0

    def test_parsed_dates(self, monkeypatch, tmp_path):
        """Explicit start/end parse into the temporal extent."""
        b = _build(
            monkeypatch,
            variables=["inform:risk"],
            country="KEN",
            start="2020-01-01",
            end="2020-12-31",
            path=tmp_path,
        )
        assert len(b.time.dates) == 2


class TestRouting:
    """download() routes per provider and returns the right shape."""

    def test_thinkhazard_resolves_admin(self, fake_http, monkeypatch, tmp_path):
        """A ThinkHazard country request resolves KEN to admin code 133."""
        df = _build(
            monkeypatch,
            variables=["thinkhazard:flood_river"],
            country="KEN",
            path=tmp_path,
        ).download()
        assert isinstance(df, pd.DataFrame) and df.iloc[0]["hazard"] == "FL"
        assert fake_http.calls[0]["url"].endswith("/report/133/FL.json")

    def test_thinkhazard_raw_admin_code(self, fake_http, monkeypatch, tmp_path):
        """A raw admin_code bypasses ISO resolution."""
        _build(
            monkeypatch,
            variables=["thinkhazard:flood_river"],
            admin_code="133",
            path=tmp_path,
        ).download()
        assert fake_http.calls[0]["url"].endswith("/report/133/FL.json")

    def test_thinkhazard_all(self, fake_http, monkeypatch, tmp_path):
        """The all-hazards dataset hits /report/{code}.json and returns 11 rows."""
        df = _build(
            monkeypatch, variables=["thinkhazard:all"], country="KEN", path=tmp_path
        ).download()
        assert len(df) == 11
        assert fake_http.calls[0]["url"].endswith("/report/133.json")

    def test_inform_returns_country_row(self, fake_http, monkeypatch, tmp_path):
        """An INFORM request returns the filtered country score."""
        df = _build(
            monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path
        ).download()
        assert len(df) == 1 and df.iloc[0]["iso3"] == "KEN"

    def test_inform_without_country_returns_all(self, fake_http, monkeypatch, tmp_path):
        """An INFORM request with no country returns every country."""
        df = _build(monkeypatch, variables=["inform:risk"], path=tmp_path).download()
        assert len(df) > 1

    def test_inform_uses_catalog_workflow(self, fake_http, monkeypatch, tmp_path):
        """An INFORM request sends the workflow the catalog pins."""
        _build(
            monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path
        ).download()
        assert fake_http.calls[0]["params"]["WorkflowId"] == 503

    def test_inform_frame_records_the_workflow(self, fake_http, monkeypatch, tmp_path):
        """The returned rows carry the workflow they were fetched with."""
        df = _build(
            monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path
        ).download()
        assert df.iloc[0]["workflow_id"] == 503

    def test_inform_frame_records_an_overridden_workflow(
        self, fake_http, monkeypatch, tmp_path
    ):
        """An overridden workflow is the one stamped into the rows."""
        df = _build(
            monkeypatch,
            variables=["inform:risk"],
            country="KEN",
            workflow_id=493,
            path=tmp_path,
        ).download()
        assert df.iloc[0]["workflow_id"] == 493

    def test_inform_workflow_id_overrides_catalog(
        self, fake_http, monkeypatch, tmp_path
    ):
        """A workflow_id= argument replaces the catalog pin in the request."""
        _build(
            monkeypatch,
            variables=["inform:risk"],
            country="KEN",
            workflow_id=493,
            path=tmp_path,
        ).download()
        assert fake_http.calls[0]["params"]["WorkflowId"] == 493

    def test_workflow_id_ignored_for_other_providers(
        self, fake_http, monkeypatch, tmp_path
    ):
        """A workflow_id= on a ThinkHazard request reaches no request parameter."""
        _build(
            monkeypatch,
            variables=["thinkhazard:flood_river"],
            country="KEN",
            workflow_id=493,
            path=tmp_path,
        ).download()
        call = fake_http.calls[0]
        assert call["url"].endswith("/report/133/FL.json")
        assert "WorkflowId" not in call["params"]

    def test_workflow_id_on_other_provider_warns(self, monkeypatch, tmp_path):
        """A workflow_id= that cannot apply is reported, not silently dropped."""
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING")
        try:
            _build(
                monkeypatch,
                variables=["thinkhazard:flood_river"],
                country="KEN",
                workflow_id=493,
                path=tmp_path,
            )
        finally:
            logger.remove(sink_id)
        assert any("applies to INFORM datasets only" in message for message in messages)

    @pytest.mark.parametrize("bad", ["503", 503.0, True, 0, -5])
    def test_workflow_id_must_be_a_positive_integer(self, monkeypatch, tmp_path, bad):
        """A non-integer or non-positive workflow_id never reaches the query string."""
        with pytest.raises(ValueError, match="workflow_id must be a positive"):
            _build(
                monkeypatch,
                variables=["inform:risk"],
                country="KEN",
                workflow_id=bad,
                path=tmp_path,
            )

    def test_gfw_tabular_forwards_key_and_iso(self, fake_http, monkeypatch, tmp_path):
        """A GFW tabular request sends the key header and the iso in the SQL."""
        df = _build(
            monkeypatch,
            variables=["gfw:tree_cover_loss"],
            country="KEN",
            api_key="secret",
            path=tmp_path,
        ).download()
        assert isinstance(df, pd.DataFrame) and len(df) == 6
        call = fake_http.calls[0]
        assert call["headers"]["x-api-key"] == "secret"
        assert "KEN" in call["params"]["sql"]

    def test_gfw_vector_returns_feature_collection(
        self, fake_http, monkeypatch, tmp_path
    ):
        """A GFW geometry request returns a FeatureCollection."""
        fc = _build(
            monkeypatch,
            variables=["gfw:admin_boundary"],
            country="KEN",
            api_key="secret",
            path=tmp_path,
        ).download()
        assert isinstance(fc, FeatureCollection)

    def test_gfw_upper_cases_country(self, fake_http, monkeypatch, tmp_path):
        """A lower-case country is upper-cased before the GFW SQL / geostore."""
        _build(
            monkeypatch,
            variables=["gfw:tree_cover_loss"],
            country="ken",
            api_key="secret",
            path=tmp_path,
        ).download()
        assert "'KEN'" in fake_http.calls[0]["params"]["sql"]
        assert "ken" not in fake_http.calls[0]["params"]["sql"]


class TestDownloadSemantics:
    """Output writing, aggregate rejection, and empty results."""

    def test_tabular_writes_csv(self, fake_http, monkeypatch, tmp_path):
        """A tabular download writes a CSV to the output directory."""
        _build(
            monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path
        ).download()
        assert (tmp_path / "risk_inform_risk.csv").exists()

    @pytest.mark.parametrize("agg", [object(), {"x": 1}])
    def test_aggregate_rejected(self, monkeypatch, tmp_path, agg):
        """A non-None aggregate= is rejected with NotImplementedError."""
        b = _build(monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path)
        with pytest.raises(NotImplementedError, match="aggregate"):
            b.download(aggregate=agg)

    def test_empty_result_writes_schema_only(self, fake_http, monkeypatch, tmp_path):
        """A country absent from the INFORM payload yields an empty table."""
        df = _build(
            monkeypatch, variables=["inform:risk"], country="ZMB", path=tmp_path
        ).download()
        assert df.empty
        assert (tmp_path / "risk_inform_risk.csv").exists()

    def test_citationless_dataset_logs_nothing(self, fake_http, monkeypatch, tmp_path):
        """A dataset with no citation skips the citation log line."""
        b = _build(monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path)
        b._dataset = b._dataset.model_copy(update={"citation": ""})
        b.download()
        assert b._dataset.citation == ""

    def test_parquet_output(self, fake_http, monkeypatch, tmp_path):
        """A parquet request writes a .parquet file when pyarrow is available."""
        pytest.importorskip("pyarrow")
        _build(
            monkeypatch,
            variables=["inform:risk"],
            country="KEN",
            output_format="parquet",
            path=tmp_path,
        ).download()
        assert (tmp_path / "risk_inform_risk.parquet").exists()


class TestEmptyHint:
    """The diagnosis appended to an empty INFORM result."""

    def test_blames_the_country_filter(self, fake_http, monkeypatch, tmp_path):
        """An unmatched country is reported as a country problem, not a dead workflow."""
        b = _build(monkeypatch, variables=["inform:risk"], country="GRL", path=tmp_path)
        b.download()
        hint = b._empty_hint()
        assert "none for country='GRL'" in hint
        assert "outside the country set INFORM scores" in hint

    def test_blames_the_workflow(self, fake_http, monkeypatch, tmp_path):
        """An upstream that serves nothing names the workflow and the override."""
        monkeypatch.setattr(_helpers, "inform_query", lambda *a, **k: [])
        b = _build(monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path)
        b.download()
        hint = b._empty_hint()
        assert "workflow 503 served no rows at all" in hint
        assert "workflow_id=" in hint

    def test_names_the_overridden_workflow(self, fake_http, monkeypatch, tmp_path):
        """The hint reports the override, not the catalog pin, when one is given."""
        monkeypatch.setattr(_helpers, "inform_query", lambda *a, **k: [])
        b = _build(
            monkeypatch,
            variables=["inform:risk"],
            country="KEN",
            workflow_id=515,
            path=tmp_path,
        )
        b.download()
        assert "workflow 515 served no rows" in b._empty_hint()

    def test_state_does_not_leak_between_requests(
        self, fake_http, monkeypatch, tmp_path
    ):
        """A second download diagnoses its own result, not the previous one."""
        b = _build(monkeypatch, variables=["inform:risk"], country="GRL", path=tmp_path)
        b.download()
        assert "none for country='GRL'" in b._empty_hint()
        monkeypatch.setattr(_helpers, "inform_query", lambda *a, **k: [])
        b.download()
        assert "served no rows at all" in b._empty_hint()

    def test_silent_before_any_request(self, monkeypatch, tmp_path):
        """A backend that has issued no request diagnoses nothing."""
        b = _build(monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path)
        assert b._empty_hint() == ""

    def test_silent_for_other_providers(self, monkeypatch, tmp_path):
        """A non-INFORM dataset gets no INFORM-specific hint."""
        b = _build(
            monkeypatch,
            variables=["thinkhazard:flood_river"],
            country="KEN",
            path=tmp_path,
        )
        assert b._empty_hint() == ""

    def test_warning_carries_the_dead_workflow(self, fake_http, monkeypatch, tmp_path):
        """The warning users see, not just the helper, names the unserved workflow."""
        monkeypatch.setattr(_helpers, "inform_query", lambda *a, **k: [])
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING")
        try:
            _build(
                monkeypatch, variables=["inform:risk"], country="KEN", path=tmp_path
            ).download()
        finally:
            logger.remove(sink_id)
        warning = "".join(messages)
        assert "no rows matched" in warning
        assert "workflow 503 served no rows at all" in warning

    def test_warning_carries_the_country_diagnosis(
        self, fake_http, monkeypatch, tmp_path
    ):
        """A country that the served rows do not cover is named in the warning."""
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING")
        try:
            _build(
                monkeypatch, variables=["inform:risk"], country="GRL", path=tmp_path
            ).download()
        finally:
            logger.remove(sink_id)
        assert "none for country='GRL'" in "".join(messages)


class TestSubpackageHygiene:
    """Guards: no gridded-array import, and no key leaked into fixtures."""

    def test_no_xarray_import_in_subpackage(self):
        """No source module in the subpackage imports xarray."""
        offenders = [
            path.name
            for path in SUBPACKAGE.glob("*.py")
            if "import xarray" in path.read_text(encoding="utf-8")
            or "xr." in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"xarray reference in {offenders}"

    def test_no_uuid_key_in_fixtures(self):
        """No fixture JSON carries a GFW-key-shaped UUID."""
        import re

        uuid = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        )
        for path in (Path(__file__).parent / "data").glob("*.json"):
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text)
            assert not uuid.search(json.dumps(payload)), path.name
