"""Tests for the ECMWF catalog-tooling handlers (`earthlens.ecmwf.cli`).

Moved out of core's CLI test suite when the ECMWF handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import importlib
import pathlib
import shutil
import sys
import tempfile
import types
from types import SimpleNamespace

import pytest
import yaml
from loguru import logger
from typer.testing import CliRunner

import earthlens.ecmwf._helpers as ecmwf_helpers
import earthlens.ecmwf._hydrate as hydrate_mod
import earthlens.ecmwf._seed as seed_mod
import earthlens.ecmwf.cli as ecmwf_cli
import earthlens.ecmwf.endpoints as ecmwf_endpoints
import earthlens.ecmwf.endpoints as endpoints
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.app import app
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import coverage_one, refresh_one
from earthlens.cli.stanza import StanzaResult, emit_stanza, write_stanza
from earthlens.cli.validate import validate_one

pytestmark = pytest.mark.cli

runner = CliRunner()


def _info():
    """Return the BackendInfo for the ecmwf backend."""
    return next(b for b in list_backends() if b.provider == "ecmwf")


def _catalog_copy(tmp_path, monkeypatch):
    """Copy ecmwf's catalog dir and repoint CATALOG_PATH."""
    info = _info()
    module = importlib.import_module(f"{info.module}.catalog")
    src = module.CATALOG_PATH
    dst = tmp_path / src.name
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy(src, dst)
    monkeypatch.setattr(module, "CATALOG_PATH", dst)
    module.clear_catalog_cache()
    return info, module, dst


def _store_of(url):
    """Resolve which store a collections URL belongs to.

    Each store is matched on its full host, so the checks are order-independent
    and an unrecognised host raises instead of silently defaulting — the
    earlier `else: cds` fallback quietly handed the two ECMWF hosts CDS's
    dataset ids when the stores were added.
    """
    for host, store in (
        ("cds.climate.copernicus.eu", "cds"),
        ("ads.atmosphere.copernicus.eu", "ads"),
        ("ewds.climate.copernicus.eu", "ewds"),
        ("ecds.ecmwf.int", "ecds"),
        ("xds.ecmwf.int", "xds"),
    ):
        if host in url:
            return store
    raise AssertionError(f"unrecognised store URL in test stub: {url}")


_STORE_SAMPLE_ID = {
    "cds": "reanalysis-era5-land",
    "ads": "cams-global-reanalysis-eac4",
    "ewds": "cems-glofas-forecast",
    "ecds": "tigge-forecasts",
    "xds": "derived-fire-fuel-biomass",
}

_STORE_PREFIX = {
    "cds": "reanalysis",
    "ads": "cams",
    "ewds": "cems",
    "ecds": "tigge",
    "xds": "fuel",
}


def _per_store_get_json(url, **kw):
    """Return a distinct single collection id per store host."""
    return {
        "collections": [{"id": _STORE_SAMPLE_ID[_store_of(url)]}],
        "links": [],
    }


def _paginated_get_json(url, **kw):
    """Two pages per store — page 1 links to page 2 via `rel=next`."""
    prefix = _STORE_PREFIX[_store_of(url)]
    if "page2" in url:
        return {"collections": [{"id": f"{prefix}-two"}], "links": []}
    return {
        "collections": [{"id": f"{prefix}-one"}],
        "links": [{"rel": "next", "href": url + "?page2"}],
    }


class _FakeGridVariable:
    """Stands in for the pyramids `Variable` a gridded NetCDF variable becomes."""

    def __init__(self, units="K", long_name="2 metre temperature"):
        self.band_units = [units] if units else []
        self.global_attributes = {"long_name": long_name} if long_name else {}


class _FakeAttribute:
    """One GDAL attribute on an MDArray."""

    def __init__(self, name, value):
        self._name, self._value = name, value

    def GetName(self):  # noqa: N802 - GDAL's own casing
        return self._name

    def ReadAsString(self):  # noqa: N802 - GDAL's own casing
        return self._value


class _FakeMdArray:
    """Stands in for the GDAL MDArray a table column becomes."""

    def GetUnit(self):  # noqa: N802 - GDAL's own casing
        return "degrees_north"

    def GetAttributes(self):  # noqa: N802 - GDAL's own casing
        return [_FakeAttribute("long_name", "Latitude")]


def _raise_unreadable(_path):
    """Reject a container the way an unreadable file would."""
    raise RuntimeError("container cannot be opened")


class TestRefresher:
    """Tests for the ECMWF (CDS catalogue) lister + per-store writer."""

    def test_lists_cds_collection_ids(self, monkeypatch):
        """ecmwf refresh reads the public CDS catalogue collection ids."""
        monkeypatch.setattr(
            ecmwf_cli,
            "get_json",
            lambda url, **kw: {"collections": [{"id": "reanalysis-era5-land"}]},
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "ecmwf refresh ran"
        assert outcome.live_count == 1, "one CDS dataset id listed"

    def test_writes_per_store_index_from_live_fetch(self, tmp_path, monkeypatch):
        """ecmwf --write persists every store's ids into available_datasets."""
        info, module, dst = _catalog_copy(tmp_path, monkeypatch)
        monkeypatch.setattr(ecmwf_cli, "get_json", _per_store_get_json)
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "write succeeded"
        assert outcome.written.endswith("_index.yaml"), "index file written"
        module.clear_catalog_cache()
        data = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        assert data["available_datasets"] == {
            "cds": ["reanalysis-era5-land"],
            "ads": ["cams-global-reanalysis-eac4"],
            "ewds": ["cems-glofas-forecast"],
            "ecds": ["tigge-forecasts"],
            "xds": ["derived-fire-fuel-biomass"],
        }, "per-store ids persisted"
        catalog = load_catalog(info)
        for expected in _STORE_SAMPLE_ID.values():
            assert expected in catalog.available_datasets, f"{expected} unioned"

    def test_pagination_follows_rel_next_across_pages(self, tmp_path, monkeypatch):
        """`rel=next` is followed, so every page's ids land in the per-store index."""
        info, module, dst = _catalog_copy(tmp_path, monkeypatch)
        monkeypatch.setattr(ecmwf_cli, "get_json", _paginated_get_json)
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "write succeeded"
        module.clear_catalog_cache()
        data = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        assert data["available_datasets"] == {
            "cds": ["reanalysis-one", "reanalysis-two"],
            "ads": ["cams-one", "cams-two"],
            "ewds": ["cems-one", "cems-two"],
            "ecds": ["tigge-one", "tigge-two"],
            "xds": ["fuel-one", "fuel-two"],
        }, "both pages' ids per store persisted"


class TestCoverage:
    """Tests for the ecmwf coverage classifier."""

    def test_reports_done_and_addressable_across_stores(self):
        """ecmwf coverage buckets the 3-store universe into DONE vs addressable."""
        outcome = coverage_one(_info())
        assert outcome.status == "ok", "ecmwf coverage is supported"
        assert outcome.counts["DONE"] > 0, "curated rows are DONE"
        assert outcome.counts["addressable"] > 0, "uncurated ids are addressable"
        assert "cams-global-reanalysis-eac4" not in outcome.todo


class TestProber:
    """Tests for the ECMWF constraints prober (public, no creds)."""

    def test_unions_variables_from_constraints(self, monkeypatch):
        """ecmwf probe unions the `variable` values across constraint rows."""
        monkeypatch.setattr(
            ecmwf_cli,
            "_ecmwf_constraints",
            lambda d, strict=False: [
                {"variable": ["2m_temperature", "tp"]},
                {"variable": ["tp"]},
            ],
        )
        result = probe_dataset(_info(), "reanalysis-era5-single-levels")
        assert result.status == "ok", "ecmwf probe ran"
        assert sorted(result.assets) == ["2m_temperature", "tp"], "vars unioned"

    def test_unions_variables_across_rows(self, monkeypatch):
        """The variable values across all constraint rows are unioned + sorted."""
        monkeypatch.setattr(
            ecmwf_cli,
            "_ecmwf_constraints",
            lambda d, strict=False: [
                {"variable": ["t2m", "sp"]},
                {"variable": ["t2m", "msl"]},
            ],
        )
        result = probe_dataset(_info(), "reanalysis-era5-single-levels")
        assert sorted(result.assets) == ["msl", "sp", "t2m"], "vars unioned + sorted"

    def test_constraints_helper_delegates(self, monkeypatch):
        """_ecmwf_constraints delegates to the package fetch_constraints."""
        import earthlens.ecmwf.constraints as constraints

        monkeypatch.setattr(
            constraints,
            "fetch_constraints",
            lambda d, base_url=None, strict=False: [{"variable": []}],
        )
        assert ecmwf_cli._ecmwf_constraints("x") == [{"variable": []}]


def _stub_client(monkeypatch, captured=None, seen_endpoints=None):
    """Replace the shared client factory with one that writes an empty target."""

    def _retrieve(dataset, request, target):
        if captured is not None:
            captured.update(request)
        open(target, "w").close()

    def _open_client(endpoint="cds"):
        if seen_endpoints is not None:
            seen_endpoints.append(endpoint)
        return types.SimpleNamespace(retrieve=_retrieve)

    monkeypatch.setattr(endpoints, "open_client", _open_client)


class TestRetrieveProbeUnpacksAZip:
    """CADS answers some requests with a zip; every CAMS probe takes this path."""

    def test_the_netcdf_inside_a_zip_is_the_one_read(self, monkeypatch, tmp_path):
        """A zipped answer must be unpacked, not handed to the reader as-is."""
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])
        monkeypatch.setattr(ecmwf_endpoints, "open_client", lambda endpoint: object())
        monkeypatch.setenv("EARTHLENS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(
            ecmwf_helpers, "_retrieve_with_retry", _write_zip_holding_one_netcdf
        )
        seen = {}
        monkeypatch.setattr(
            ecmwf_cli,
            "_read_netcdf_var_meta",
            lambda path: (
                seen.setdefault("path", path) and {} or {"t2m": {"units": "K"}}
            ),
        )

        result = ecmwf_cli._retrieve_probe("a-dataset", {"variable": ["x"]})

        assert result == {"t2m": {"units": "K"}}
        assert seen["path"].endswith("inner.nc"), (
            f"the reader was handed {seen['path']}, not the unpacked granule"
        )

    def test_a_zip_holding_no_netcdf_is_read_as_downloaded(self, monkeypatch, tmp_path):
        """With nothing to unpack the original file is still what gets read."""
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])
        monkeypatch.setattr(ecmwf_endpoints, "open_client", lambda endpoint: object())
        monkeypatch.setenv("EARTHLENS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(
            ecmwf_helpers, "_retrieve_with_retry", _write_zip_holding_no_netcdf
        )
        seen: dict[str, str] = {}
        monkeypatch.setattr(ecmwf_cli, "_read_netcdf_var_meta", _recording_reader(seen))

        ecmwf_cli._retrieve_probe("a-dataset", {"variable": ["x"]})

        assert seen["path"].endswith("probe.nc")


class TestDeepSampleRowPicksTheRowsProduct:
    """Sampling the first listed block reads a name under a product never asked for."""

    ROWS = [
        {
            "variable": ["2m_temperature"],
            "statistic": ["24_hour_maximum"],
            "version": ["1_1"],
        },
        {
            "variable": ["2m_temperature"],
            "statistic": ["24_hour_mean"],
            "version": ["1_1"],
        },
    ]

    def test_without_a_preference_the_first_serving_block_is_taken(self):
        """The legacy behaviour, kept for callers that have no row to consult."""
        chosen = ecmwf_cli._deep_sample_row(self.ROWS, "2m_temperature")

        assert chosen["statistic"] == ["24_hour_maximum"]

    def test_the_block_matching_the_rows_request_is_preferred(self):
        """Otherwise the row documents a 24-hour maximum it will never request."""
        chosen = ecmwf_cli._deep_sample_row(
            self.ROWS, "2m_temperature", {"statistic": ["24_hour_mean"]}
        )

        assert chosen["statistic"] == ["24_hour_mean"], (
            "the probe sampled a product the row does not ask for"
        )

    def test_a_preference_no_block_matches_falls_back(self):
        """A row with an unusable request must still probe rather than yield nothing."""
        chosen = ecmwf_cli._deep_sample_row(
            self.ROWS, "2m_temperature", {"statistic": ["nothing_offered"]}
        )

        assert chosen is not None
        assert chosen["statistic"] == ["24_hour_maximum"]

    def test_a_variable_no_block_serves_is_still_none(self):
        """Unchanged: there is nothing to sample."""
        assert (
            ecmwf_cli._deep_sample_row(self.ROWS, "not_offered", {"statistic": ["x"]})
            is None
        )


class TestAuditServeableCommand:
    """`datasets audit ecmwf --serveable`, so re-deriving needs no script."""

    def test_a_clean_catalog_says_so(self, monkeypatch):
        """Silence would leave a reader unsure the check ran."""
        monkeypatch.setattr(ecmwf_cli, "serveability_auditor", lambda: [])

        result = CliRunner().invoke(app, ["datasets", "audit", "ecmwf", "--serveable"])

        assert result.exit_code == 0, result.output
        assert "names a combination the store serves" in result.output

    def test_findings_are_listed_with_what_the_row_asks_for(self, monkeypatch):
        """A bare count would not say which selector to look at."""
        import earthlens.ecmwf._hydrate as hydrate

        monkeypatch.setattr(
            hydrate,
            "audit_serveability",
            lambda: [("a-dataset", "a-slug", {"product_type": ["reanalysis"]})],
        )

        result = CliRunner().invoke(app, ["datasets", "audit", "ecmwf", "--serveable"])

        assert "a-dataset/a-slug" in result.output
        assert "reanalysis" in result.output

    def test_strict_exits_non_zero_when_rows_are_reported(self, monkeypatch):
        """So a CI gate can fail on it."""
        import earthlens.ecmwf._hydrate as hydrate

        monkeypatch.setattr(
            hydrate, "audit_serveability", lambda: [("d", "s", {"x": ["y"]})]
        )

        result = CliRunner().invoke(
            app, ["datasets", "audit", "ecmwf", "--serveable", "--strict"]
        )

        assert result.exit_code == 1, result.output

    def test_an_unreadable_store_exits_two_rather_than_reporting_clean(
        self, monkeypatch
    ):
        """A run that checked nothing must not look like a run that found nothing."""
        import earthlens.ecmwf._hydrate as hydrate

        def _unavailable():
            raise hydrate.ConstraintsUnavailable("the store could not be read")

        monkeypatch.setattr(hydrate, "audit_serveability", _unavailable)

        result = CliRunner().invoke(app, ["datasets", "audit", "ecmwf", "--serveable"])

        assert result.exit_code == 2, result.output
        assert "could not check" in result.output

    def test_a_provider_without_the_role_is_refused(self):
        """Only a provider publishing `serveability_auditor` can answer this."""
        result = CliRunner().invoke(app, ["datasets", "audit", "gee", "--serveable"])

        assert result.exit_code == 2, result.output
        assert "check its own requests" in result.output

    def test_the_role_is_published_by_the_provider_not_named_in_core(self):
        """Core must stay backend-agnostic; the handler lives with ecmwf."""
        from earthlens._cli_tooling import dispatch_table

        assert "ecmwf" in dispatch_table("serveability_auditor")


class TestEndpointFor:
    """Which CADS instance a dataset id is served from."""

    @pytest.mark.parametrize(
        ("dataset_id", "endpoint"),
        [
            ("cams-global-emission-inventories", "ads"),
            ("cams-europe-air-quality-forecasts", "ads"),
            ("cems-glofas-historical", "ewds"),
            ("efas-historical", "ewds"),
            ("reanalysis-era5-single-levels", "cds"),
            ("satellite-ozone-v1", "cds"),
        ],
    )
    def test_the_prefix_picks_the_store_when_the_catalog_says_nothing(
        self, dataset_id, endpoint
    ):
        """A misrouted probe authenticates against the wrong store and fails."""
        catalog = SimpleNamespace()

        assert ecmwf_cli._ecmwf_endpoint_for(catalog, dataset_id) == endpoint, (
            f"{dataset_id} routed to the wrong endpoint"
        )

    def test_the_catalogs_own_index_wins_over_the_prefix(self):
        """A curated store assignment must not be second-guessed by a prefix rule."""
        catalog = SimpleNamespace(store_for=lambda dataset_id: "xds")

        assert ecmwf_cli._ecmwf_endpoint_for(catalog, "cams-anything") == "xds"

    def test_an_empty_store_falls_through_to_the_prefix(self):
        """An index that knows nothing about the id must not return an empty store."""
        catalog = SimpleNamespace(store_for=lambda dataset_id: None)

        assert ecmwf_cli._ecmwf_endpoint_for(catalog, "efas-historical") == "ewds"


class TestDeepSampleVariable:
    """The per-variable probe entry point."""

    def test_a_dataset_no_block_serves_returns_two_empties(self, monkeypatch):
        """Nothing to request means nothing to describe, and no retrieve is sent."""
        monkeypatch.setattr(
            ecmwf_cli, "_ecmwf_constraints", lambda dataset, strict=False: []
        )
        monkeypatch.setattr(
            ecmwf_cli,
            "_retrieve_probe",
            lambda *args, **kwargs: pytest.fail("no retrieve should be issued"),
        )

        assert ecmwf_cli._ecmwf_deep_sample_variable("a-dataset", "t2m") == ({}, {})


class TestReadNetcdfVarMetaSubdatasets:
    """A container whose variables live in subdatasets rather than at the root."""

    def test_every_named_subdataset_is_read_and_merged(self, monkeypatch, tmp_path):
        """Some CDS NetCDFs describe their variables one subdataset down."""
        from osgeo import gdal

        target = tmp_path / "granule.nc"
        target.write_bytes(b"not really netcdf")
        monkeypatch.setattr(
            ecmwf_cli,
            "_read_via_pyramids",
            lambda path: (_ for _ in ()).throw(OSError("unreadable")),
        )
        monkeypatch.setattr(gdal, "Info", _info_with_two_subdatasets)
        monkeypatch.setattr(ecmwf_cli, "_from_info", _info_to_meta)

        meta = ecmwf_cli._read_netcdf_var_meta(str(target))

        assert sorted(meta) == ["sst", "t2m"], f"subdatasets not merged: {sorted(meta)}"
        assert meta["t2m"]["units"] == "K"

    def test_a_root_exposing_variables_is_read_without_subdatasets(
        self, monkeypatch, tmp_path
    ):
        """The ordinary container needs no second pass."""
        from osgeo import gdal

        target = tmp_path / "granule.nc"
        target.write_bytes(b"not really netcdf")
        monkeypatch.setattr(
            ecmwf_cli,
            "_read_via_pyramids",
            lambda path: (_ for _ in ()).throw(OSError("unreadable")),
        )
        monkeypatch.setattr(gdal, "Info", lambda path, format=None: {"path": "t2m"})
        monkeypatch.setattr(ecmwf_cli, "_from_info", _info_to_meta)

        assert ecmwf_cli._read_netcdf_var_meta(str(target)) == {
            "t2m": {"long_name": "2 metre temperature", "units": "K"}
        }


class TestDiscardScratch:
    """Removing one probe's scratch directory, and what it records if it cannot."""

    def test_an_ordinary_directory_is_removed_and_not_recorded(
        self, monkeypatch, tmp_path
    ):
        """The common path leaves nothing on disk and nothing to report."""
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])
        scratch = tmp_path / "probe"
        scratch.mkdir()
        (scratch / "granule.nc").write_bytes(b"x")

        ecmwf_cli._discard_scratch(str(scratch))

        assert not scratch.exists(), "the scratch directory survived"
        assert ecmwf_cli.UNREMOVED_SCRATCH == []

    def test_an_already_gone_directory_is_not_recorded(self, monkeypatch, tmp_path):
        """Nothing is left behind, so there is nothing to attribute."""
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])

        ecmwf_cli._discard_scratch(str(tmp_path / "never-created"))

        assert ecmwf_cli.UNREMOVED_SCRATCH == []

    def test_a_survivor_is_recorded_once_per_call(self, monkeypatch, tmp_path):
        """The sweep summary counts these, so a double entry would overstate it."""
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])
        monkeypatch.setattr(shutil, "rmtree", _refuse_to_remove)
        scratch = tmp_path / "probe"
        scratch.mkdir()

        ecmwf_cli._discard_scratch(str(scratch))

        assert ecmwf_cli.UNREMOVED_SCRATCH == [str(scratch)]


def _write_zip_holding_one_netcdf(client, dataset, request, target, endpoint):
    """Stand in for a store answering with a zipped granule."""
    import zipfile

    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("inner.nc", b"granule bytes")


def _write_zip_holding_no_netcdf(client, dataset, request, target, endpoint):
    """Stand in for a zip carrying no NetCDF member at all."""
    import zipfile

    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("readme.txt", b"no granule here")


def _info_with_two_subdatasets(path, format=None):
    """Stand in for gdal.Info over a container holding two subdatasets."""
    if path.endswith(".nc"):
        return {
            "metadata": {
                "SUBDATASETS": {
                    "SUBDATASET_1_NAME": "NETCDF:granule.nc:t2m",
                    "SUBDATASET_1_DESC": "the temperature field",
                    "SUBDATASET_2_NAME": "NETCDF:granule.nc:sst",
                    "SUBDATASET_2_DESC": "the sea surface field",
                }
            }
        }
    return {"path": path}


def _info_to_meta(info):
    """Turn a stubbed gdal.Info payload into the meta mapping the reader returns."""
    path = str(info.get("path", ""))
    if path.endswith("t2m"):
        return {"t2m": {"long_name": "2 metre temperature", "units": "K"}}
    if path.endswith("sst"):
        return {"sst": {"long_name": "sea surface temperature", "units": "K"}}
    return {}


def _recording_reader(seen):
    """A `_read_netcdf_var_meta` stand-in that records the path it was handed.

    A lambda leaning on `x and {} or y` reads as a puzzle and silently inverts
    if the recorded value is ever falsy.
    """

    def _read(path):
        seen["path"] = path
        return {"t2m": {"units": "K"}}

    return _read


def _refuse_to_remove(path, **kwargs):
    """Stand in for a Windows reader that still holds the granule.

    Patched onto `shutil.rmtree` for the duration of one call rather than a
    directory made genuinely unremovable, since only Windows can produce that
    state and the test has to fail on every platform when the fix goes.
    """


def _stub_probe_transport(monkeypatch, tmp_path):
    """Point a probe at a local write instead of a store, scratch under `tmp_path`."""
    monkeypatch.setattr(
        ecmwf_helpers,
        "_retrieve_with_retry",
        lambda client, dataset, request, target, endpoint: pathlib.Path(
            target
        ).write_bytes(b"probe"),
    )
    monkeypatch.setattr(ecmwf_endpoints, "open_client", lambda endpoint: object())
    monkeypatch.setattr(
        ecmwf_cli, "_read_netcdf_var_meta", lambda path: {"x": {"units": "K"}}
    )
    monkeypatch.setenv("EARTHLENS_CACHE_DIR", str(tmp_path))


class TestDeepProber:
    """Tests for the credentialed ecmwf `--deep` sampler."""

    def test_deep_reads_retrieved_netcdf(self, monkeypatch):
        """ecmwf --deep reads long_name/units from a retrieved NetCDF."""
        monkeypatch.setattr(
            ecmwf_cli,
            "_ecmwf_deep_sample",
            lambda d: {"t2m": {"long_name": "2 metre temperature", "units": "K"}},
        )
        result = probe_dataset(_info(), "reanalysis-era5-single-levels", deep=True)
        assert result.status == "ok", "ecmwf deep probe ran"
        assert result.assets["t2m"]["units"] == "K", "retrieved var units read"

    def test_deep_sample_reads_netcdf(self, monkeypatch):
        """_ecmwf_deep_sample retrieves a tiny NetCDF and reads var metadata."""
        monkeypatch.setattr(
            ecmwf_cli,
            "_ecmwf_constraints",
            lambda d, strict=False: [
                {"variable": ["2m_temperature"], "year": ["2020"]}
            ],
        )
        _stub_client(monkeypatch)
        monkeypatch.setattr(
            ecmwf_cli,
            "_read_netcdf_var_meta",
            lambda path: {"t2m": {"long_name": "2 metre temperature", "units": "K"}},
        )
        out = ecmwf_cli._ecmwf_deep_sample("reanalysis-era5-single-levels")
        assert out["t2m"]["units"] == "K", "retrieved var units read"

    def test_deep_sample_carries_family_selectors(self, monkeypatch):
        """The sampled request forwards every selector of the chosen entry."""
        entry = {
            "variable": ["surface_soil_moisture"],
            "type_of_sensor": ["passive"],
            "time_aggregation": ["month_average"],
            "version": ["v202212"],
            "year": ["2023"],
            "month": ["01"],
            "day": ["01"],
        }
        monkeypatch.setattr(ecmwf_cli, "_ecmwf_constraints", lambda d: [entry])
        captured: dict[str, object] = {}
        _stub_client(monkeypatch, captured)
        monkeypatch.setattr(ecmwf_cli, "_read_netcdf_var_meta", lambda path: {})
        ecmwf_cli._ecmwf_deep_sample("satellite-soil-moisture")
        assert captured["type_of_sensor"] == ["passive"]
        assert captured["version"] == ["v202212"]
        assert captured["time_aggregation"] == ["month_average"]
        assert captured["data_format"] == "netcdf"
        assert "time" not in captured  # the entry enumerates none — fabricate none

    def test_deep_sample_defaults_variable_when_absent(self, monkeypatch):
        """An entry with no variable dimension still sends the widget's `all`."""
        monkeypatch.setattr(
            ecmwf_cli, "_ecmwf_constraints", lambda d: [{"lake": ["achit"]}]
        )
        captured: dict[str, object] = {}
        _stub_client(monkeypatch, captured)
        monkeypatch.setattr(ecmwf_cli, "_read_netcdf_var_meta", lambda path: {})
        ecmwf_cli._ecmwf_deep_sample("satellite-lake-water-level")
        assert captured["variable"] == ["all"]
        assert captured["lake"] == ["achit"]

    def test_read_netcdf_var_meta_via_gdal(self, tmp_path):
        """_read_netcdf_var_meta reads long_name/units from a NetCDF via GDAL."""
        import numpy as np
        import xarray as xr

        path = tmp_path / "probe.nc"
        xr.Dataset(
            {
                "t2m": (
                    ("lat", "lon"),
                    np.ones((2, 2), "f4"),
                    {"units": "K", "long_name": "2 metre temperature"},
                )
            },
            coords={"lat": [1.0, 0.0], "lon": [0.0, 1.0]},
        ).to_netcdf(path)
        meta = ecmwf_cli._read_netcdf_var_meta(str(path))
        assert meta["t2m"] == {"long_name": "2 metre temperature", "units": "K"}

    def test_a_probe_survives_a_scratch_it_cannot_remove(self, monkeypatch, tmp_path):
        """A removal that fails must not discard a retrieve and read that worked."""
        _stub_probe_transport(monkeypatch, tmp_path)
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])
        monkeypatch.setattr(shutil, "rmtree", _refuse_to_remove)

        assert ecmwf_cli._retrieve_probe("a-dataset", {"variable": ["x"]}) == {
            "x": {"units": "K"}
        }

    def test_an_unremovable_scratch_is_named_and_counted(self, monkeypatch, tmp_path):
        """Silent tolerance would leave a sweep's tens of GB unattributable."""
        _stub_probe_transport(monkeypatch, tmp_path)
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])
        monkeypatch.setattr(shutil, "rmtree", _refuse_to_remove)
        warnings: list[str] = []
        sink = logger.add(warnings.append, level="WARNING")

        try:
            ecmwf_cli._retrieve_probe("a-dataset", {"variable": ["x"]})
        finally:
            logger.remove(sink)

        assert len(ecmwf_cli.UNREMOVED_SCRATCH) == 1, "the survivor was not counted"
        left = ecmwf_cli.UNREMOVED_SCRATCH[0]
        assert pathlib.Path(left).exists()
        assert any(left in message for message in warnings), (
            "the path that could not be removed was never named"
        )

    def test_a_removable_scratch_leaves_nothing_behind(self, monkeypatch, tmp_path):
        """The ordinary path still removes the granule and reports no survivor."""
        _stub_probe_transport(monkeypatch, tmp_path)
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])

        ecmwf_cli._retrieve_probe("a-dataset", {"variable": ["x"]})

        assert ecmwf_cli.UNREMOVED_SCRATCH == []
        assert list(tmp_path.iterdir()) == [], "the probe scratch was left on disk"

    def test_the_reader_releases_its_container(self, monkeypatch, tmp_path):
        """The handle has to be let go, which is what keeps the scratch removable."""
        closed: list = []

        class _Container:
            variable_names = ["t2m"]

            def get_variable(self, name):
                return SimpleNamespace(
                    band_units=["K"], global_attributes={"long_name": "temperature"}
                )

            def close(self):
                closed.append(True)

        module = SimpleNamespace(
            NetCDF=SimpleNamespace(read_file=lambda p: _Container())
        )
        monkeypatch.setitem(sys.modules, "pyramids.netcdf", module)
        schema = ecmwf_cli._read_via_pyramids(str(tmp_path / "cube.nc"))
        assert schema == {"t2m": {"long_name": "temperature", "units": "K"}}
        assert closed == [True], "the container was left open"

    def test_read_netcdf_var_meta_prefers_pyramids(self, monkeypatch, tmp_path):
        """pyramids owns NetCDF reading, so its answer is the one used."""
        path = tmp_path / "probe.nc"
        path.write_bytes(b"not really a netcdf")
        monkeypatch.setattr(
            ecmwf_cli, "_read_via_pyramids", lambda p: {"tp": {"units": "m"}}
        )
        assert ecmwf_cli._read_netcdf_var_meta(str(path)) == {"tp": {"units": "m"}}

    def test_read_netcdf_var_meta_falls_back_when_pyramids_is_empty(
        self, monkeypatch, tmp_path
    ):
        """Every hydrated row was read by the classic walk; it must keep working."""
        import numpy as np
        import xarray as xr

        path = tmp_path / "probe.nc"
        xr.Dataset(
            {
                "t2m": (
                    ("lat", "lon"),
                    np.ones((2, 2), "f4"),
                    {"units": "K", "long_name": "2 metre temperature"},
                )
            },
            coords={"lat": [1.0, 0.0], "lon": [0.0, 1.0]},
        ).to_netcdf(path)
        monkeypatch.setattr(ecmwf_cli, "_read_via_pyramids", lambda p: {})
        meta = ecmwf_cli._read_netcdf_var_meta(str(path))
        assert meta["t2m"] == {"long_name": "2 metre temperature", "units": "K"}

    def test_read_netcdf_var_meta_falls_back_when_pyramids_raises(
        self, monkeypatch, tmp_path
    ):
        """An unreadable container must not lose a file the classic walk can read."""
        import numpy as np
        import xarray as xr

        path = tmp_path / "probe.nc"
        xr.Dataset(
            {"tp": (("lat", "lon"), np.ones((2, 2), "f4"), {"units": "m"})},
            coords={"lat": [1.0, 0.0], "lon": [0.0, 1.0]},
        ).to_netcdf(path)
        monkeypatch.setattr(ecmwf_cli, "_read_via_pyramids", _raise_unreadable)
        assert ecmwf_cli._read_netcdf_var_meta(str(path))["tp"]["units"] == "m"

    def test_variable_meta_reads_a_gridded_variable(self):
        """A gridded variable arrives as a pyramids Variable."""
        assert ecmwf_cli._variable_meta(_FakeGridVariable()) == {
            "long_name": "2 metre temperature",
            "units": "K",
        }

    def test_variable_meta_reads_a_table_column(self):
        """A variable with no x/y dimension arrives as the raw MDArray."""
        assert ecmwf_cli._variable_meta(_FakeMdArray()) == {
            "long_name": "Latitude",
            "units": "degrees_north",
        }

    def test_variable_meta_declines_a_variable_carrying_neither(self):
        """Nothing to record is not the same as a unitless empty string."""
        assert (
            ecmwf_cli._variable_meta(_FakeGridVariable(units=None, long_name=None))
            is None
        )

    @pytest.mark.parametrize(
        "dataset, expected",
        [
            ("reanalysis-era5-single-levels", "cds"),
            ("cams-global-emission-inventories", "ads"),
            ("cems-glofas-forecast", "ewds"),
            ("tigge-forecasts", "ecds"),
            ("derived-fire-fuel-biomass", "xds"),
        ],
    )
    def test_deep_sample_retrieves_from_the_datasets_own_store(
        self, monkeypatch, dataset, expected
    ):
        """The sample goes to the row's store; a bare client would 404 off-CDS."""
        monkeypatch.setattr(
            ecmwf_cli, "_ecmwf_constraints", lambda d: [{"variable": ["x"]}]
        )
        seen: list[str] = []
        _stub_client(monkeypatch, seen_endpoints=seen)
        monkeypatch.setattr(ecmwf_cli, "_read_netcdf_var_meta", lambda path: {})
        ecmwf_cli._ecmwf_deep_sample(dataset)
        assert seen == [expected]

    def test_deep_sample_uses_the_index_for_an_uncurated_dataset(self, monkeypatch):
        """An uncurated id resolves via the index, not the CDS default."""
        monkeypatch.setattr(
            ecmwf_cli, "_ecmwf_constraints", lambda d: [{"variable": ["x"]}]
        )
        seen: list[str] = []
        _stub_client(monkeypatch, seen_endpoints=seen)
        monkeypatch.setattr(ecmwf_cli, "_read_netcdf_var_meta", lambda path: {})
        ecmwf_cli._ecmwf_deep_sample("cams-europe-air-quality-forecasts")
        assert seen == ["ads"]

    def test_deep_sample_unknown_dataset_falls_back_to_cds(self, monkeypatch):
        """An id in neither the rows nor the index still samples, against CDS."""
        monkeypatch.setattr(
            ecmwf_cli, "_ecmwf_constraints", lambda d: [{"variable": ["x"]}]
        )
        seen: list[str] = []
        _stub_client(monkeypatch, seen_endpoints=seen)
        monkeypatch.setattr(ecmwf_cli, "_read_netcdf_var_meta", lambda path: {})
        ecmwf_cli._ecmwf_deep_sample("not-a-real-dataset-anywhere")
        assert seen == ["cds"]

    def test_deep_sample_no_constraints(self, monkeypatch):
        """No constraints rows yields an empty schema (after the SDK imports)."""
        monkeypatch.setattr(ecmwf_cli, "_ecmwf_constraints", lambda d: [])
        assert ecmwf_cli._ecmwf_deep_sample("x") == {}


class TestEmitter:
    """Tests for the ECMWF emitter (seeds from the live CADS form.json, mocked)."""

    _HINDCAST_FORM = [
        {"name": "hyear", "details": {}},
        {"name": "hmonth", "details": {}},
        {"name": "hday", "details": {}},
        {"name": "leadtime_hour", "details": {}},
        {
            "name": "variable",
            "details": {"values": ["river_discharge_in_the_last_24_hours"]},
        },
    ]

    def test_seeds_hindcast_row_with_ewds_endpoint(self, monkeypatch):
        """A `hyear`/`hday` form seeds a glofas_hindcast row on the ewds store."""
        monkeypatch.setattr(
            ecmwf_cli, "get_json", lambda url, **kw: self._HINDCAST_FORM
        )
        result = emit_stanza(_info(), "cems-glofas-reforecast")
        assert result.status == "ok"
        assert result.row["endpoint"] == "ewds"
        assert result.row["request_kind"] == "glofas_hindcast"
        assert "river-discharge-in-the-last-24-hours" in result.row["variables"]

    def test_cams_date_form_seeds_ads_endpoint(self, monkeypatch):
        """A `date`-range form on a `cams-*` id seeds a cams_date row on ads."""
        form = [
            {"name": "date", "details": {}},
            {"name": "variable", "details": {"values": ["total_column_ozone"]}},
        ]
        monkeypatch.setattr(ecmwf_cli, "get_json", lambda url, **kw: form)
        result = emit_stanza(_info(), "cams-global-reanalysis-eac4")
        assert result.status == "ok"
        assert result.row["endpoint"] == "ads"
        assert result.row["request_kind"] == "cams_date"

    def test_fire_form_seeds_fire_not_satellite(self, monkeypatch):
        """A grid + `dataset_type` form (no leadtime_hour) seeds a `fire` row."""
        form = [
            {"name": "dataset_type", "details": {}},
            {"name": "grid", "details": {}},
            {"name": "variable", "details": {"values": ["fire_weather_index"]}},
        ]
        monkeypatch.setattr(ecmwf_cli, "get_json", lambda url, **kw: form)
        result = emit_stanza(_info(), "cems-fire-historical-v1")
        assert result.status == "ok"
        assert result.row["request_kind"] == "fire"

    def test_satellite_id_seeds_satellite_cdr(self, monkeypatch):
        """A `satellite-*` id seeds satellite_cdr from its real (grid-less) form."""
        form = [
            {"name": "type_of_sensor", "details": {}},
            {"name": "time_aggregation", "details": {}},
            {"name": "year", "details": {}},
            {"name": "month", "details": {}},
            {"name": "day", "details": {}},
            {
                "name": "variable",
                "details": {"values": ["surface_soil_moisture_volumetric"]},
            },
        ]
        monkeypatch.setattr(ecmwf_cli, "get_json", lambda url, **kw: form)
        result = emit_stanza(_info(), "satellite-soil-moisture")
        assert result.row["request_kind"] == "satellite_cdr"

    def test_seeds_every_variable_the_form_exposes(self, monkeypatch):
        """A multi-variable form seeds one row per variable, all as placeholders."""
        form = [
            {"name": "year", "details": {}},
            {"name": "month", "details": {}},
            {"name": "day", "details": {}},
            {"name": "time", "details": {}},
            {
                "name": "variable",
                "details": {"values": ["2m_temperature", "total_precipitation"]},
            },
        ]
        monkeypatch.setattr(ecmwf_cli, "get_json", lambda url, **kw: form)
        result = emit_stanza(_info(), "reanalysis-era5-single-levels")
        assert result.status == "ok"
        variables = result.row["variables"]
        assert set(variables) == {"2m-temperature", "total-precipitation"}
        assert variables["2m-temperature"]["cds_variable"] == "2m_temperature"
        assert all(v["units"] == "unknown" for v in variables.values())

    def test_error_is_captured(self, monkeypatch):
        """A failed fetch reports 'error', not raised."""

        def boom(url, **kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(ecmwf_cli, "get_json", boom)
        assert emit_stanza(_info(), "reanalysis-era5-single-levels").status == "error"


class TestRequestKind:
    """`_ecmwf_request_kind` maps a form's fields (+ dataset id) to a request kind."""

    @pytest.mark.parametrize(
        "upstream_id, field_names, expected",
        [
            (
                "satellite-soil-moisture",
                ["type_of_sensor", "year", "day"],
                "satellite_cdr",
            ),
            ("cems-glofas-reforecast", ["hyear", "hmonth", "hday"], "glofas_hindcast"),
            ("efas-seasonal-reforecast", ["hyear", "hmonth"], "seasonal_hindcast"),
            ("cams-global-reanalysis-eac4", ["date", "variable"], "cams_date"),
            ("cams-ghg-inversion", ["quantity", "year", "month"], "cams_inversion"),
            (
                "cams-europe-air-quality-reanalyses",
                ["year", "month", "model"],
                "cams_inversion",
            ),
            ("cems-glofas-seasonal", ["leadtime_month", "year", "month"], "seasonal"),
            ("cams-global-emission-inventories", ["year", "month"], "form"),
            ("projections-cmip6", ["year", "month", "model"], "form"),
            (
                "cems-fire-historical-v1",
                ["grid", "dataset_type", "year", "day"],
                "fire",
            ),
            ("cems-fire-seasonal", ["leadtime_hour", "year", "month"], "fire"),
            ("grid-only-cdr", ["grid", "year", "day"], "satellite_cdr"),
            ("reanalysis-era5-single-levels", ["year", "month", "day", "time"], "form"),
        ],
    )
    def test_kind_from_id_and_fields(self, upstream_id, field_names, expected):
        """Each id/field-set combination maps to the documented request kind."""
        form = [{"name": name} for name in field_names]
        result = ecmwf_cli._ecmwf_request_kind(form, upstream_id)
        assert result == expected, (
            f"{upstream_id}/{field_names} → {result}, want {expected}"
        )

    def test_glofas_forecast_grid_absent_falls_through_to_form(self):
        """A leadtime_hour form with no grid is not misread as a grid kind."""
        form = [{"name": "year"}, {"name": "day"}, {"name": "leadtime_hour"}]
        assert ecmwf_cli._ecmwf_request_kind(form, "cems-glofas-forecast") == "form"


class TestWriteStanza:
    """Tests for the ecmwf categoriser via write_stanza."""

    def test_auto_categorises_target(self, tmp_path, monkeypatch):
        """ecmwf without --target auto-picks the per-family shard from the id."""
        info = _info()
        module = importlib.import_module(f"{info.module}.catalog")
        monkeypatch.setattr(module, "CATALOG_PATH", tmp_path)
        result = StanzaResult(
            "ecmwf",
            "reanalysis-era5-complete",
            "reanalysis-era5-complete",
            "ok",
            row={"endpoint": "cds", "request_kind": "form"},
        )
        written = write_stanza(info, result, None)
        assert written.endswith("era5.yaml"), "era5 id routed to era5.yaml"
        assert (tmp_path / "era5.yaml").exists(), "the shard file was written"


class TestLiveValidator:
    """Tests for the ecmwf constraints-based live validator."""

    def test_flags_invalid_request(self, monkeypatch):
        """An ECMWF dataset whose minimal request fails the validator is flagged."""
        import earthlens.ecmwf.constraints as constraints

        catalog = SimpleNamespace(
            datasets={"good": object(), "nocon": object(), "bad": object()},
            minimal_valid_request=lambda key: {
                "good": {"data_format": "netcdf", "variable": ["x"]},
                "nocon": {"data_format": "netcdf"},
                "bad": {"data_format": "netcdf", "variable": ["y"]},
            }[key],
        )

        class FakeValidator:
            def __init__(self, dataset, request):
                self.dataset = dataset

            def check(self):
                if self.dataset == "bad":
                    raise ValueError("missing required selector 'level'")

        monkeypatch.setattr(constraints, "RequestValidator", FakeValidator)
        checked, issues = ecmwf_cli.live_validator(catalog)
        assert checked == 2, "the no-constraints dataset is skipped"
        assert any("bad" in i for i in issues), "invalid request flagged"

    def test_reports_fetch_failure(self):
        """A dataset whose constraints fetch raises is reported, not raised."""

        def boom(key):
            raise RuntimeError("offline")

        catalog = SimpleNamespace(datasets={"d": object()}, minimal_valid_request=boom)
        checked, issues = ecmwf_cli.live_validator(catalog)
        assert any("constraints fetch failed" in i for i in issues), "failure reported"

    def test_supported_under_live(self):
        """ecmwf gains a live-only validator via discovery."""
        assert validate_one(_info()).status == "unsupported"
        assert validate_one(_info(), live=True).status in {"ok", "error"}


class TestCommands:
    """Command-level tests for the ecmwf hydrate / seed passes (SDKs mocked)."""

    def test_fill_empty_runs_bulk_hydrate(self, monkeypatch):
        """ecmwf --fill-empty --write drives the ecmwf hydrate and reports a summary."""
        calls = {}

        def fake_hydrate(limit=None, timeout=None):
            calls["limit"] = limit
            calls["timeout"] = timeout
            return {
                "candidates": 4,
                "hydrated": 3,
                "skipped": 1,
                "timed_out": 0,
                "filled": ["a", "b", "c"],
            }

        monkeypatch.setattr(hydrate_mod, "bulk_hydrate_empty", fake_hydrate)
        result = runner.invoke(
            app, ["datasets", "curate", "ecmwf", "--fill-empty", "--write"]
        )
        assert result.exit_code == 0, f"ecmwf fill-empty failed: {result.output}"
        assert "hydrated 3" in result.output
        assert "/ 4" in result.output
        assert calls["timeout"] == 180, "the default --timeout is threaded through"

    def test_fill_empty_threads_custom_timeout(self, monkeypatch):
        """A custom --timeout is passed to the ecmwf hydrate; 0 means no deadline."""
        calls = {}

        def fake_hydrate(limit=None, timeout=None):
            calls["timeout"] = timeout
            return {
                "candidates": 1,
                "hydrated": 0,
                "skipped": 1,
                "timed_out": 1,
                "filled": [],
            }

        monkeypatch.setattr(hydrate_mod, "bulk_hydrate_empty", fake_hydrate)
        result = runner.invoke(
            app,
            [
                "datasets",
                "curate",
                "ecmwf",
                "--fill-empty",
                "--write",
                "--timeout",
                "0",
            ],
        )
        assert result.exit_code == 0, f"ecmwf fill-empty failed: {result.output}"
        assert calls["timeout"] is None, "--timeout 0 becomes no deadline (None)"
        assert "1 timed out" in result.output, "timed-out count is surfaced"

    def test_all_runs_bulk_seed(self, monkeypatch):
        """ecmwf --all --write drives the bulk seed and reports a summary."""
        monkeypatch.setattr(
            seed_mod,
            "bulk_seed_uncurated",
            lambda limit=None: {
                "candidates": 5,
                "seeded": 4,
                "skipped": 1,
                "failed": [("x", "boom")],
            },
        )
        result = runner.invoke(app, ["datasets", "curate", "ecmwf", "--all", "--write"])
        assert result.exit_code == 0, f"--all failed: {result.output}"
        assert "seeded 4" in result.output
        assert "/ 5" in result.output

    def test_deep_flag_routes_to_credentialed_sampler(self, monkeypatch):
        """probe --deep uses the deep sampler (creds mocked)."""
        monkeypatch.setattr(
            ecmwf_cli, "_ecmwf_deep_sample", lambda d: {"t2m": {"units": "K"}}
        )
        result = runner.invoke(
            app,
            [
                "datasets",
                "probe",
                "ecmwf",
                "reanalysis-era5-single-levels",
                "--deep",
                "--json",
            ],
        )
        import json

        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["assets"]["t2m"]["units"] == "K"


class TestStoreTables:
    """The CLI store tables cover every store, at its real URL."""

    def test_every_store_has_an_api_root(self):
        """All five slugs resolve to their documented API root."""
        assert ecmwf_cli._store_urls() == {
            "cds": "https://cds.climate.copernicus.eu/api",
            "ads": "https://ads.atmosphere.copernicus.eu/api",
            "ewds": "https://ewds.climate.copernicus.eu/api",
            "ecds": "https://ecds.ecmwf.int/api",
            "xds": "https://xds.ecmwf.int/api",
        }

    def test_every_store_has_a_collections_url(self):
        """Each catalogue URL is its store's root plus the collections path."""
        assert ecmwf_cli._store_collections_urls() == {
            "cds": "https://cds.climate.copernicus.eu/api/catalogue/v1/collections",
            "ads": "https://ads.atmosphere.copernicus.eu/api/catalogue/v1/collections",
            "ewds": "https://ewds.climate.copernicus.eu/api/catalogue/v1/collections",
            "ecds": "https://ecds.ecmwf.int/api/catalogue/v1/collections",
            "xds": "https://xds.ecmwf.int/api/catalogue/v1/collections",
        }

    def test_url_override_reaches_the_catalog_tooling(self, monkeypatch):
        """A `<ENDPOINT>_URL` override applies to the CLI, not just the client."""
        monkeypatch.setenv("ECDS_URL", "https://staging.ecds.invalid/api")
        assert ecmwf_cli._store_urls()["ecds"] == "https://staging.ecds.invalid/api"
        assert ecmwf_cli._store_collections_urls()["ecds"] == (
            "https://staging.ecds.invalid/api/catalogue/v1/collections"
        )


class TestRequiredSelectors:
    """Tests for deriving what a variable is only ever served under."""

    CMIP = [
        {
            "variable": ["mean_temperature"],
            "model": ["csiro_mk3_6_0"],
            "experiment": ["amip"],
            "period": ["19790101-19981231"],
        },
        {
            "variable": ["mean_temperature"],
            "model": ["gfdl_esm2g"],
            "experiment": ["historical"],
            "period": ["18610101-18801231"],
        },
    ]
    GLOFAS = [
        {
            "variable": ["river_discharge_in_the_last_24_hours"],
            "timespan": ["time_mean"],
            "hyear": ["2020"],
        },
        {
            "variable": ["river_discharge_in_the_last_24_hours"],
            "timespan": ["time_mean"],
            "hyear": ["2021"],
        },
        {
            "variable": ["snow_depth_water_equivalent"],
            "timespan": ["instantaneous"],
            "hyear": ["2020"],
        },
    ]

    def test_a_selector_the_caller_may_vary_is_not_a_requirement(self):
        """A CMIP variable is served under every model, so none may be pinned."""
        assert ecmwf_cli._required_selectors(self.CMIP, "mean_temperature") == {}

    def test_a_selector_every_serving_entry_agrees_on_is_a_requirement(self):
        """Snow depth is served solely under instantaneous, which is the constraint."""
        required = ecmwf_cli._required_selectors(
            self.GLOFAS, "snow_depth_water_equivalent"
        )
        assert required["timespan"] == ["instantaneous"]

    def test_a_selector_that_varies_is_dropped_even_when_others_hold(self):
        """hyear differs across the serving entries, so it is not a requirement."""
        required = ecmwf_cli._required_selectors(
            self.GLOFAS, "river_discharge_in_the_last_24_hours"
        )
        assert required["timespan"] == ["time_mean"]
        assert "hyear" not in required

    def test_a_variable_no_entry_serves_requires_nothing(self):
        """An unknown variable has no serving entry to derive a requirement from."""
        assert ecmwf_cli._required_selectors(self.GLOFAS, "not_a_variable") == {}


class TestProbeRetriesThrottling:
    """The probe path must survive a throttled store like the download path does."""

    def test_a_throttled_probe_is_retried_then_raises_typed(
        self, monkeypatch, tmp_path
    ):
        """A sweep fires one probe per row, so it meets the queue limit first."""
        from earthlens.ecmwf import CadsUnavailableError, _helpers

        monkeypatch.setattr(_helpers, "CADS_BACKOFF_SECONDS", 0.0)
        calls = []

        class _Throttled:
            def retrieve(self, dataset, request, target):
                calls.append(dataset)
                raise RuntimeError(
                    "400 Client Error: Bad Request. The job has been rejected. "
                    "Number queued requests for this dataset is temporarily limited."
                )

        monkeypatch.setattr(ecmwf_cli, "_endpoint_for", lambda ds: "ads")
        import earthlens.ecmwf.endpoints as endpoints

        monkeypatch.setattr(endpoints, "open_client", lambda endpoint: _Throttled())
        monkeypatch.setenv("EARTHLENS_CACHE_DIR", str(tmp_path))
        with pytest.raises(CadsUnavailableError):
            ecmwf_cli._retrieve_probe(
                "cams-global-emission-inventories", {"variable": ["x"]}
            )
        assert len(calls) == _helpers.CADS_MAX_ATTEMPTS, "retried, not single-shot"
