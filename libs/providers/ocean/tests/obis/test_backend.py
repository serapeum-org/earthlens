"""Unit tests for the OBIS occurrence backend."""

from __future__ import annotations

import pytest
from geopandas import GeoDataFrame
from shapely.geometry import Point

from earthlens.biodiversity import LicenseWarning
from earthlens.obis import OBIS, OBIS_COLUMNS


def _backend(tmp_path, variables=None, **kwargs):
    """Build an OBIS backend over a small bbox and a multi-year window."""
    return OBIS(
        start="2015-01-01",
        end="2020-12-31",
        variables=variables if variables is not None else ["common-dolphin"],
        lat_lim=[30.0, 45.0],
        lon_lim=[-10.0, 5.0],
        path=str(tmp_path),
        **kwargs,
    )


@pytest.mark.obis
class TestConstruction:
    """Construction validates `variables` and `file_format`."""

    def test_output_kind_is_vector(self, tmp_path):
        """The backend declares vector output."""
        assert _backend(tmp_path).OUTPUT_KIND == "vector"

    def test_dict_variables_rejected(self, tmp_path):
        """A mapping `variables` raises a clear TypeError."""
        with pytest.raises(TypeError, match="not a mapping"):
            _backend(tmp_path, variables={"blue-whale": 1})

    def test_empty_variables_rejected(self, tmp_path):
        """An empty `variables` raises a ValueError."""
        with pytest.raises(ValueError, match="at least one species"):
            _backend(tmp_path, variables=[])

    def test_unknown_file_format_rejected(self, tmp_path):
        """An unknown file_format raises a ValueError."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            _backend(tmp_path, file_format="shp")


@pytest.mark.obis
class TestPlanSearch:
    """`_plan_search` builds the `occurrences.search` kwargs."""

    def test_objective_kwargs(self, tmp_path):
        """The planned kwargs match the objective request key by key."""
        params = _backend(tmp_path)._plan_search()
        assert params["scientificname"] == "Delphinus delphis"
        assert params["startdate"] == "2015-01-01"
        assert params["enddate"] == "2020-12-31"
        assert params["geometry"] == "POLYGON ((5 30, 5 45, -10 45, -10 30, 5 30))"
        assert params["size"] == 10_000


@pytest.mark.obis
class TestFetchAndDownload:
    """`download` consumes the 1.x DataFrame return and maps it to an FC."""

    def test_consumes_execute_dataframe(self, tmp_path, fake_obis):
        """The backend reads the `.execute()` DataFrame, not a results dict."""
        fake_obis.occurrences.set_frame(
            fake_obis.frame([fake_obis.row(id="a"), fake_obis.row(id="b")])
        )
        fc = _backend(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 2
        assert fc.crs.to_epsg() == 4326
        assert isinstance(fc.geometry.iloc[0], Point)

    def test_null_coordinate_null_geometry(self, tmp_path, fake_obis):
        """A record missing a latitude gets a null geometry."""
        fake_obis.occurrences.set_frame(
            fake_obis.frame([fake_obis.row(decimalLatitude=None)])
        )
        fc = _backend(tmp_path).download()
        assert fc.geometry.iloc[0] is None

    def test_cc_by_nc_warns(self, tmp_path, fake_obis):
        """A CC-BY-NC row raises a LicenseWarning."""
        fake_obis.occurrences.set_frame(
            fake_obis.frame([fake_obis.row(license="CC-BY-NC")])
        )
        with pytest.warns(LicenseWarning):
            _backend(tmp_path).download()

    def test_cc_by_no_warning(self, tmp_path, fake_obis, recwarn):
        """A plain CC-BY-4.0 batch raises no LicenseWarning."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        _backend(tmp_path).download()
        assert not [w for w in recwarn.list if issubclass(w.category, LicenseWarning)]

    def test_empty_result_keeps_schema(self, tmp_path, fake_obis):
        """An empty DataFrame yields an empty FC carrying exactly OBIS_COLUMNS."""
        fc = _backend(tmp_path).download()
        assert len(fc) == 0
        assert list(fc.columns) == [*OBIS_COLUMNS, "geometry"]

    def test_download_writes_geoparquet(self, tmp_path, fake_obis):
        """A non-empty result is written to a GeoParquet file under path."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        _backend(tmp_path).download()
        assert (tmp_path / "obis_occurrences.parquet").exists()

    def test_multiple_species_unioned(self, tmp_path, fake_obis):
        """Several species each issue a search and their rows are concatenated."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        fc = _backend(tmp_path, variables=["blue-whale", "common-dolphin"]).download()
        names = [c["scientificname"] for c in fake_obis.occurrences.calls]
        assert names == ["Balaenoptera musculus", "Delphinus delphis"]
        assert len(fc) == 2

    def test_aggregate_rejected(self, tmp_path, fake_obis):
        """A non-None aggregate raises NotImplementedError mentioning vector."""
        with pytest.raises(NotImplementedError, match="vector"):
            _backend(tmp_path).download(aggregate=object())

    def test_api_returns_collection(self, tmp_path, fake_obis):
        """`_api` returns the occurrence FeatureCollection."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        assert len(_backend(tmp_path)._api()) == 1

    def test_geojson_write(self, tmp_path, fake_obis):
        """A non-parquet file_format writes via the OGR driver."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        _backend(tmp_path, file_format="geojson").download()
        assert (tmp_path / "obis_occurrences.geojson").exists()

    def test_empty_path_opts_out_of_writing(self, tmp_path, fake_obis):
        """`path=""` returns the in-memory FC but writes no file."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        backend = OBIS(
            start="2015-01-01",
            end="2020-12-31",
            variables=["common-dolphin"],
            lat_lim=[30.0, 45.0],
            lon_lim=[-10.0, 5.0],
            path="",
        )
        fc = backend.download()
        assert len(fc) == 1
        assert not any(tmp_path.glob("*.parquet"))


class _RaiseFromCall:
    """A `_fetch_selector` stand-in: delegate, then raise `exc` from `from_call`.

    Hoisted to module scope (no nested-function definitions); holds its own
    call counter. `from_call=2` fails the second selector (a partial failure);
    `from_call=1` fails every selector.
    """

    def __init__(self, real, exc, from_call=2):
        self._real = real
        self._exc = exc
        self._from_call = from_call
        self._calls = 0

    def __call__(self, selector):
        """Delegate until `from_call`, then raise `exc` on that call onward."""
        self._calls += 1
        if self._calls >= self._from_call:
            raise self._exc
        return self._real(selector)


@pytest.mark.obis
class TestErrorsPolicy:
    """`errors=` partial-failure policy across the independent species selectors."""

    def _two_selector_backend(self, tmp_path, fake_obis, monkeypatch, exc, from_call=2):
        """A backend over two selectors whose fetch raises `exc` from `from_call`."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        backend = _backend(tmp_path, variables=["blue-whale", "common-dolphin"])
        monkeypatch.setattr(
            backend,
            "_fetch_selector",
            _RaiseFromCall(backend._fetch_selector, exc, from_call),
        )
        return backend

    def test_raise_propagates_first_failure(self, tmp_path, fake_obis, monkeypatch):
        """errors="raise" surfaces a per-selector exception, not a batch summary."""
        backend = self._two_selector_backend(
            tmp_path, fake_obis, monkeypatch, ValueError("selector boom")
        )
        with pytest.raises(ValueError, match="selector boom"):
            backend.download(errors="raise")

    def test_warn_returns_the_surviving_selector(
        self, tmp_path, fake_obis, monkeypatch
    ):
        """errors="warn" drops a non-fatal per-selector failure, keeps survivors."""
        backend = self._two_selector_backend(
            tmp_path, fake_obis, monkeypatch, ValueError("selector boom")
        )
        fc = backend.download(errors="warn")
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1

    def test_ignore_drops_failures_and_returns_survivor(
        self, tmp_path, fake_obis, monkeypatch
    ):
        """errors="ignore" drops a non-fatal per-selector failure silently."""
        backend = self._two_selector_backend(
            tmp_path, fake_obis, monkeypatch, ValueError("selector boom")
        )
        assert len(backend.download(errors="ignore")) == 1

    def test_default_policy_is_warn(self, tmp_path, fake_obis, monkeypatch):
        """Omitting errors= defaults to warn — a non-fatal failure is survived."""
        backend = self._two_selector_backend(
            tmp_path, fake_obis, monkeypatch, ValueError("selector boom")
        )
        assert len(backend.download()) == 1

    @pytest.mark.parametrize("policy", ["warn", "ignore"])
    def test_every_selector_failing_raises(
        self, tmp_path, fake_obis, monkeypatch, policy
    ):
        """Every selector failing re-raises the first error, never an empty FC."""
        backend = self._two_selector_backend(
            tmp_path,
            fake_obis,
            monkeypatch,
            RuntimeError("all selectors down"),
            from_call=1,
        )
        with pytest.raises(RuntimeError, match="all selectors down"):
            backend.download(errors=policy)

    def test_invalid_policy_rejected(self, tmp_path):
        """An unrecognised errors= value is refused before any request."""
        with pytest.raises(ValueError, match="errors"):
            _backend(tmp_path).download(errors="explode")
