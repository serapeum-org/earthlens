"""Unit + integration tests for the EUMETSAT Data Tailor branch (mocked `eumdac`)."""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.eumetsat
from earthlens.eumetsat import EUMETSAT, TailorConfig

from .conftest import _FakeCustomisation, _FakeProduct

pytestmark = pytest.mark.eumetsat

_CREDS = {"consumer_key": "k", "consumer_secret": "s"}
_OLCI = "EO:EUM:DAT:0409"  # s3-olci-l1-efr, tailor_product_type OLL1EFR


def _backend(fake_eumdac, tmp_path, variables, **kwargs):
    """Build an EUMETSAT backend wired to the fake `eumdac`."""
    return EUMETSAT(
        start="2024-01-01",
        end="2024-01-02",
        variables=variables,
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        **_CREDS,
        **kwargs,
    )


# --- TailorConfig -----------------------------------------------------------


def test_tailorconfig_nswe_from_bbox():
    """bbox (west, south, east, north) maps to the [N, S, W, E] ROI list."""
    cfg = TailorConfig(format="geotiff", crs="geographic", bbox=(4, 48, 8, 52))
    assert cfg.nswe == [52.0, 48.0, 4.0, 8.0]


def test_tailorconfig_defaults_and_no_bbox():
    """Defaults are geotiff/geographic and no bbox yields no ROI list."""
    cfg = TailorConfig()
    assert cfg.format == "geotiff"
    assert cfg.crs == "geographic"
    assert cfg.nswe is None
    assert TailorConfig(bbox=None).nswe is None  # explicit None passes validation


def test_tailorconfig_is_frozen_and_forbids_extra():
    """TailorConfig is immutable and rejects unknown fields."""
    from pydantic import ValidationError

    cfg = TailorConfig()
    with pytest.raises(ValidationError):
        cfg.format = "netcdf4"
    with pytest.raises(ValidationError):
        TailorConfig(bogus=1)


def test_tailorconfig_rejects_blank_format():
    """A blank format / crs is rejected."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TailorConfig(format="  ")


@pytest.mark.parametrize("blank", ["", "  "])
def test_tailorconfig_rejects_blank_crs(blank):
    """A blank crs is still rejected; only None means no reprojection."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TailorConfig(crs=blank)


def test_tailorconfig_crs_none_is_allowed():
    """crs=None is valid and survives validation as None."""
    assert TailorConfig(format="msgnative", crs=None).crs is None


@pytest.mark.parametrize(
    "native_format", ["msgnative", "epsnative", "hrit", "hrit_compressed"]
)
def test_tailorconfig_rejects_native_format_with_projection(native_format):
    """A native format cannot carry a projection -- crs must be None."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="cannot be reprojected"):
        TailorConfig(format=native_format, crs="geographic")


@pytest.mark.parametrize(
    "native_format", ["msgnative", "epsnative", "hrit", "hrit_compressed"]
)
def test_tailorconfig_native_format_with_crs_none_is_valid(native_format):
    """Every native format pairs cleanly with crs=None."""
    cfg = TailorConfig(format=native_format, crs=None)
    assert cfg.crs is None, f"{native_format} should validate with crs=None"


def test_tailorconfig_non_native_format_keeps_default_crs():
    """A non-native format is unaffected by the native/crs cross-check."""
    assert TailorConfig(format="geotiff").crs == "geographic"


def test_tailorconfig_native_check_is_case_sensitive_by_design():
    """A case-mismatched native format bypasses the cross-check -- by design.

    format's *legitimacy* is deliberately not validated client-side (see the
    module docstring's Out of Scope note); Data Tailor's format IDs are
    lowercase, so "MSGNATIVE" is already an invalid value for an unrelated
    reason and would be rejected by the service, just after a round trip
    rather than at construction. Extending NATIVE_FORMATS matching to be
    case-insensitive would silently paper over that typo instead of
    surfacing it, which is not this validator's job.
    """
    cfg = TailorConfig(format="MSGNATIVE", crs="geographic")
    assert cfg.format == "MSGNATIVE", "format is passed through, not case-normalised"


def test_tailorconfig_native_check_sees_stripped_values():
    """The cross-field check runs after whitespace stripping, not before.

    field_validator (which strips) and model_validator(mode="after") (which
    cross-checks) both fire during construction, so a padded native format
    paired with a padded projection must still be caught -- confirming the
    stripped, not the raw, values reach the cross-check.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="cannot be reprojected"):
        TailorConfig(format="  msgnative  ", crs="  geographic  ")


@pytest.mark.parametrize("field", ["format", "crs"])
def test_tailorconfig_strips_surrounding_whitespace(field):
    """Padding is stripped off format and crs alike."""
    cfg = TailorConfig(**{field: "  geographic  " if field == "crs" else "  geotiff  "})
    expected = "geographic" if field == "crs" else "geotiff"
    got = getattr(cfg, field)
    assert got == expected, f"{field} should be stripped to {expected!r}, got {got!r}"


def test_tailorconfig_rejects_none_format():
    """Only crs may be None; format stays a required string."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        TailorConfig(format=None)
    kinds = [e["type"] for e in exc_info.value.errors()]
    assert "string_type" in kinds, f"expected a string_type error, got {kinds}"


def test_nswe_from_extent_orders_bounds():
    """nswe_from_extent returns bounds in [N, S, W, E] order."""
    assert TailorConfig.nswe_from_extent(52, 48, 4, 8) == [52, 48, 4, 8]


@pytest.mark.parametrize(
    "bbox",
    [
        (8, 48, 4, 52),  # west > east
        (4, 52, 8, 48),  # south > north
        (-200, 48, 8, 52),  # lon out of range
        (4, -100, 8, 52),  # lat out of range
    ],
)
def test_tailorconfig_rejects_bad_bbox(bbox):
    """An inverted or out-of-range bbox is rejected at construction."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TailorConfig(bbox=bbox)


# --- happy path -------------------------------------------------------------


def test_tailor_happy_path_builds_chain_streams_and_deletes(
    fake_eumdac, tmp_path, monkeypatch
):
    """A tailorable request builds the Chain, polls to DONE, streams, deletes."""
    monkeypatch.setattr("earthlens.eumetsat.backend.TAILOR_POLL_INITIAL_S", 0.0)
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    cust = _FakeCustomisation(
        statuses=["QUEUED", "RUNNING", "DONE"], outputs=["a.tif", "b.tif"]
    )
    fake_eumdac.tailor.customisation = cust
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(
        progress_bar=False,
        tailor=TailorConfig(format="geotiff", crs="geographic", bbox=(4, 48, 8, 52)),
    )
    assert {p.name for p in paths} == {"a.tif", "b.tif"}
    _product, chain = fake_eumdac.tailor.submitted[0]
    assert chain.product == "OLL1EFR"
    assert chain.format == "geotiff"
    assert chain.projection == "geographic"
    assert chain.roi.NSWE == [52.0, 48.0, 4.0, 8.0]
    assert chain.filter is None
    assert cust.deleted == 1
    # outputs are namespaced under a per-product subdirectory (H1)
    assert {p.parent.name for p in paths} == {"p1"}
    assert (tmp_path / "p1" / "a.tif").read_bytes().startswith(b"TAILORED")


def test_tailor_default_crs_sends_projection(fake_eumdac, tmp_path):
    """The default config still puts a projection on the chain."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(
        statuses=["DONE"], outputs=["a.tif"]
    )
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    backend.download(progress_bar=False, tailor=TailorConfig())
    _product, chain = fake_eumdac.tailor.submitted[0]
    assert chain.kwargs["projection"] == "geographic"


def test_tailor_crs_none_sends_projection_none(fake_eumdac, tmp_path):
    """crs=None reaches the chain as projection=None.

    eumdac's own `Chain.asdict()` drops `None` fields before the request is
    built (see `test_eumdac_chain_asdict_drops_none_projection` below), so a
    `None` projection and an omitted one produce the identical request --
    there is no need for this backend to distinguish the two.
    """
    fake_eumdac.store.products_for["EO:EUM:DAT:MSG:HRSEVIRI"] = [_FakeProduct("p1")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(
        statuses=["DONE"], outputs=["a.nat"]
    )
    backend = _backend(fake_eumdac, tmp_path, {"msg-hrseviri": ["HRSEVIRI"]})
    backend.download(
        progress_bar=False,
        tailor=TailorConfig(
            format="msgnative", crs=None, bbox=(-5.0, 40.0, 15.0, 55.0)
        ),
    )
    _product, chain = fake_eumdac.tailor.submitted[0]
    assert chain.kwargs["projection"] is None
    assert chain.format == "msgnative"
    assert chain.product == "HRSEVIRI"


def test_eumdac_chain_asdict_drops_none_projection():
    """eumdac's Chain treats an explicit None projection like an omitted one.

    This is the real-`eumdac` contract `_tailor_one` relies on to pass
    `projection=tailor.crs` unconditionally instead of building the call
    from a conditional kwargs dict. Skipped when the `eumetsat` extra
    (`eumdac`) is not installed.
    """
    eumdac = pytest.importorskip("eumdac")
    explicit_none = eumdac.tailor_models.Chain(product="X", projection=None)
    omitted = eumdac.tailor_models.Chain(product="X")
    assert explicit_none.asdict() == omitted.asdict()
    assert "projection" not in explicit_none.asdict()


def test_tailor_multiple_products_namespaced_no_collision(fake_eumdac, tmp_path):
    """Two granules sharing an output basename land in distinct product dirs."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1"), _FakeProduct("p2")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(outputs=["out.tif"])
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(progress_bar=False, tailor=TailorConfig())
    assert len(paths) == 2
    assert len(set(paths)) == 2, "outputs collided across products"
    assert {p.parent.name for p in paths} == {"p1", "p2"}
    for p in paths:
        assert p.read_bytes().startswith(b"TAILORED")


def test_tailor_duplicate_product_ids_get_distinct_dirs(fake_eumdac, tmp_path):
    """Two products with an identical id write to distinct subdirs (L3)."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("dup"), _FakeProduct("dup")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(outputs=["out.tif"])
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(progress_bar=False, tailor=TailorConfig())
    assert len(set(paths)) == 2
    assert {p.parent.name for p in paths} == {"dup", "dup_1"}


def test_tailor_outputs_sharing_basename_do_not_overwrite(fake_eumdac, tmp_path):
    """Two outputs that sanitise to one basename get distinct files (L2)."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(
        outputs=["a/out.tif", "b/out.tif"]
    )
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(progress_bar=False, tailor=TailorConfig())
    assert len(set(paths)) == 2
    assert {p.name for p in paths} == {"out.tif", "out.tif_1"}


def test_tailor_subdir_avoids_existing_native_file(fake_eumdac, tmp_path):
    """A pre-existing native file with the product basename doesn't break mkdir (L3)."""
    (tmp_path / "p1").write_bytes(b"native")  # a prior native download's output
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(outputs=["o.tif"])
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(progress_bar=False, tailor=TailorConfig())
    assert paths[0].parent.name == "p1_1"
    assert (tmp_path / "p1").read_bytes() == b"native"  # native file untouched


def test_tailor_roi_falls_back_to_request_extent(fake_eumdac, tmp_path):
    """With no bbox, the ROI comes from the request lat_lim / lon_lim."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(outputs=["o.tif"])
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    backend.download(progress_bar=False, tailor=TailorConfig())
    _product, chain = fake_eumdac.tailor.submitted[0]
    assert chain.roi.NSWE == [52.0, 50.0, -1.0, 1.0]


def test_tailor_filter_and_quicklook_forwarded(fake_eumdac, tmp_path):
    """filter maps to a Filter(bands=...) and quicklook=True is forwarded."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(outputs=["o.tif"])
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    backend.download(
        progress_bar=False,
        tailor=TailorConfig(filter=["B1", "B2"], quicklook=True),
    )
    _product, chain = fake_eumdac.tailor.submitted[0]
    assert chain.filter.bands == ["B1", "B2"]
    assert chain.quicklook is True


# --- failure + lifecycle ----------------------------------------------------


@pytest.mark.parametrize("terminal", ["FAILED", "KILLED"])
def test_tailor_non_done_terminal_raises_and_deletes(fake_eumdac, tmp_path, terminal):
    """A FAILED / KILLED customisation raises with the log and still deletes."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    cust = _FakeCustomisation(statuses=[terminal], logfile="boom-in-log")
    fake_eumdac.tailor.customisation = cust
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(RuntimeError, match=terminal):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert cust.deleted == 1
    assert "boom-in-log" in str(cust.logfile)


def test_tailor_unknown_status_fails_fast(fake_eumdac, tmp_path):
    """An unexpected non-active status (e.g. INACTIVE) fails fast, not a timeout."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    cust = _FakeCustomisation(statuses=["INACTIVE"], logfile="evicted")
    fake_eumdac.tailor.customisation = cust
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(RuntimeError, match="INACTIVE"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert cust.deleted == 1


def test_tailor_timeout_raises_and_deletes(fake_eumdac, tmp_path, monkeypatch):
    """A job that never reaches a terminal state times out and is deleted."""
    monkeypatch.setattr("earthlens.eumetsat.backend.TAILOR_POLL_TIMEOUT_S", 0.0)
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    cust = _FakeCustomisation(statuses=["RUNNING"])
    fake_eumdac.tailor.customisation = cust
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(TimeoutError, match="did not finish"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert cust.deleted == 1


class _NoLogCustomisation(_FakeCustomisation):
    """A customisation whose `logfile` read fails (exercises the placeholder)."""

    @property
    def logfile(self):
        raise RuntimeError("log endpoint down")


def test_tailor_missing_logfile_uses_placeholder(fake_eumdac, tmp_path):
    """A FAILED job whose logfile read fails still raises with a placeholder."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    cust = _NoLogCustomisation(statuses=["FAILED"])
    fake_eumdac.tailor.customisation = cust
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(RuntimeError, match="no customisation log"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert cust.deleted == 1


def test_tailor_delete_failure_does_not_break_success(fake_eumdac, tmp_path):
    """A delete() that raises on a DONE job is swallowed; the download succeeds."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    cust = _FakeCustomisation(outputs=["o.tif"])
    cust.delete_error = RuntimeError("delete endpoint down")
    fake_eumdac.tailor.customisation = cust
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(progress_bar=False, tailor=TailorConfig())
    assert [p.name for p in paths] == ["o.tif"]
    assert cust.deleted == 1


def test_tailor_delete_failure_does_not_mask_original_error(fake_eumdac, tmp_path):
    """A delete() that raises on a FAILED job must not hide the FAILED error."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    cust = _FakeCustomisation(statuses=["FAILED"], logfile="root cause here")
    cust.delete_error = RuntimeError("delete endpoint down")
    fake_eumdac.tailor.customisation = cust
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(RuntimeError, match="FAILED"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert cust.deleted == 1


# --- eligibility ------------------------------------------------------------


def test_tailor_non_eligible_dataset_raises_before_submit(fake_eumdac, tmp_path):
    """A dataset without tailor_product_type is rejected before any search."""
    backend = _backend(fake_eumdac, tmp_path, {"s5p-l2-no2": ["x"]})
    with pytest.raises(ValueError, match="not Data-Tailor-eligible"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert fake_eumdac.tailor.submitted == []
    assert fake_eumdac.store.search_calls == []


def test_tailor_one_defensive_eligibility_guard(fake_eumdac, tmp_path):
    """_tailor_one re-checks eligibility per product (defensive guard)."""
    import types

    from earthlens.eumetsat.catalog import EumetsatDataset

    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    ineligible = EumetsatDataset(
        collection_id="X", group="MSG", tailor_product_type=None
    )
    product = types.SimpleNamespace(
        metadata={"product": object(), "dataset": ineligible}
    )
    with pytest.raises(ValueError, match="not Data-Tailor-eligible"):
        backend._tailor_one(product, TailorConfig(), fake_eumdac.tailor, set())


# --- submit retry (G8) ------------------------------------------------------


def test_tailor_submit_retries_transient_then_succeeds(
    fake_eumdac, tmp_path, monkeypatch
):
    """A transient 502 on submit is retried and the second attempt succeeds."""
    monkeypatch.setattr("earthlens.eumetsat.backend.TAILOR_SUBMIT_BACKOFF_S", 0.0)
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(outputs=["o.tif"])
    fake_eumdac.tailor.submit_errors = [RuntimeError("502 Bad Gateway"), None]
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(progress_bar=False, tailor=TailorConfig())
    assert [p.name for p in paths] == ["o.tif"]
    assert len(fake_eumdac.tailor.submitted) == 2


def test_tailor_submit_non_transient_raised_immediately(fake_eumdac, tmp_path):
    """A non-transient submit error (bad product id) is not retried."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.submit_errors = [ValueError("Invalid product-ID 'X'")]
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(ValueError, match="Invalid product-ID"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert len(fake_eumdac.tailor.submitted) == 1


def test_tailor_submit_transient_exhausted_raises_runtime(
    fake_eumdac, tmp_path, monkeypatch
):
    """Repeated transient submit failures exhaust the retries and raise."""
    monkeypatch.setattr("earthlens.eumetsat.backend.TAILOR_SUBMIT_BACKOFF_S", 0.0)
    monkeypatch.setattr("earthlens.eumetsat.backend.TAILOR_SUBMIT_RETRIES", 3)
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.submit_errors = [RuntimeError("502 Bad Gateway")] * 3
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    with pytest.raises(RuntimeError, match="transient attempts"):
        backend.download(progress_bar=False, tailor=TailorConfig())
    assert len(fake_eumdac.tailor.submitted) == 3


# --- native regression ------------------------------------------------------


def test_facade_forwards_tailor_to_backend(fake_eumdac, tmp_path):
    """EarthLens(...).download(tailor=...) reaches the backend Data Tailor branch."""
    from earthlens.earthlens import EarthLens

    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("p1")]
    fake_eumdac.tailor.customisation = _FakeCustomisation(outputs=["o.tif"])
    el = EarthLens(
        data_source="eumetsat",
        start="2024-01-01",
        end="2024-01-02",
        variables={"s3-olci-l1-efr": ["OLL1EFR"]},
        lat_lim=[50.0, 52.0],
        lon_lim=[-1.0, 1.0],
        path=str(tmp_path),
        **_CREDS,
    )
    paths = el.download(progress_bar=False, tailor=TailorConfig())
    assert [p.name for p in paths] == ["o.tif"]
    assert len(fake_eumdac.tailor.submitted) == 1


def test_native_download_unchanged_without_tailor(fake_eumdac, tmp_path):
    """Without tailor=, download fetches the native product and never tailors."""
    fake_eumdac.store.products_for[_OLCI] = [_FakeProduct("native.nc", b"NATIVE")]
    backend = _backend(fake_eumdac, tmp_path, {"s3-olci-l1-efr": ["OLL1EFR"]})
    paths = backend.download(progress_bar=False)
    assert [p.name for p in paths] == ["native.nc"]
    assert (tmp_path / "native.nc").read_bytes() == b"NATIVE"
    assert fake_eumdac.tailor.submitted == []


# --- static conformance guards (A2) ----------------------------------------


def _eumetsat_py_files() -> list[Path]:
    src = Path(earthlens.eumetsat.__file__).parent
    return list(src.rglob("*.py"))


def test_no_xarray_import_in_eumetsat():
    """The customised file reads via pyramids — the backend never imports xarray."""
    offenders = [
        p for p in _eumetsat_py_files() if "xarray" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"xarray referenced in {offenders}"


def test_no_hardcoded_credentials_in_eumetsat():
    """No long consumer key/secret literal is committed in the backend source."""
    import re

    pattern = re.compile(r"consumer_(?:key|secret)\s*=\s*[\"'][A-Za-z0-9]{20,}[\"']")
    offenders = [
        p for p in _eumetsat_py_files() if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"hardcoded credential literal in {offenders}"
