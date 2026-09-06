"""Tests for `earthlens.gee.backend` — the `GEE` data source.

Earth Engine and the HTTP download are fully faked via `monkeypatch`:
`ee` is replaced with a small chainable recorder
(`_FakeImageCollection` / `_FakeImage` / `_FakeGeometry`), `requests`
with a stub that returns non-zip bytes, and `EarthEngineAuth.initialize`
with a stub that returns a fixed project. The real shipped
`gee_data_catalog.yaml` is used (no network).
"""

from __future__ import annotations

import datetime as dt
import inspect
import math
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.gee import backend as backend_module
from earthlens.gee.backend import GEE
from earthlens.gee.catalog import Dataset, Extent
from earthlens.gee.cloud_masks import sentinel2_scl

# -- fakes ------------------------------------------------------------------


class _FakeImage:
    """Recorder standing in for an `ee.Image` (the composited result)."""

    def __init__(self, label: str = "image", reducer: str | None = None):
        self.label = label
        self.reducer = reducer
        self.calls: list[tuple[str, tuple]] = []
        self.download_params: dict | None = None
        self.download_params_list: list[dict] = []

    def select(self, bands):
        self.calls.append(("select", (tuple(bands),)))
        return self

    def clip(self, geom):
        self.calls.append(("clip", (geom,)))
        return self

    def getDownloadURL(self, params):  # noqa: N802 - mirrors the ee API
        self.download_params = dict(params)
        self.download_params_list.append(dict(params))
        return "http://fake.test/download.tif"


class _FakeImageCollection:
    """Recorder standing in for an `ee.ImageCollection`.

    Chain methods (`filterDate`, `filterBounds`, `select`) return a new
    instance carrying the accumulated call log; the reducer convenience
    methods (`mean`/`median`/`mosaic`/...) return a :class:`_FakeImage`.
    """

    def __init__(self, source, calls: list | None = None):
        self.source = source
        self.calls: list[tuple[str, tuple]] = list(calls or [])

    def _chain(self, name: str, *args) -> _FakeImageCollection:
        return _FakeImageCollection(self.source, self.calls + [(name, args)])

    def filterDate(self, start, end):  # noqa: N802
        return self._chain("filterDate", start, end)

    def filterBounds(self, geom):  # noqa: N802
        return self._chain("filterBounds", geom)

    def filter(self, filt):
        return self._chain("filter", filt)

    def map(self, fn):
        return self._chain("map", fn)

    def select(self, bands):
        return self._chain("select", tuple(bands))

    def _reduce(self, name: str) -> _FakeImage:
        self.calls.append((name, ()))
        return _FakeImage(label=f"{name}({self.source})", reducer=name)

    def mean(self):
        return self._reduce("mean")

    def median(self):
        return self._reduce("median")

    def min(self):
        return self._reduce("min")

    def max(self):
        return self._reduce("max")

    def mode(self):
        return self._reduce("mode")

    def mosaic(self):
        return self._reduce("mosaic")

    def sum(self):
        return self._reduce("sum")

    def method_names(self) -> list[str]:
        """Return just the names of the recorded chain calls (for assertions)."""
        return [name for name, _ in self.calls]


class _FakeGeometry:
    """Stands in for `ee.Geometry.Rectangle(...)` output (or a gdf geometry)."""

    def __init__(self, coords):
        self.coords = coords


class _FakeTask:
    """Stands in for an `ee.batch.Task` returned by `ee.batch.Export.image.to*`."""

    def __init__(
        self, kwargs: dict, states: list[str] | None = None, error: str | None = None
    ):
        self.kwargs = kwargs
        self._states = list(states or ["COMPLETED"])
        self._error = error
        self.started = False
        self.poll_count = 0

    def start(self):
        self.started = True

    def status(self) -> dict:
        self.poll_count += 1
        state = self._states[min(self.poll_count - 1, len(self._states) - 1)]
        out = {"state": state}
        if state == "FAILED" and self._error:
            out["error_message"] = self._error
        return out


class _FakeExportImage:
    """Stands in for `ee.batch.Export.image` (`toDrive` / `toCloudStorage`)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.tasks: list[_FakeTask] = []
        self.next_task_states: list[str] | None = None
        self.next_task_error: str | None = None

    def _make(self, method: str, **kwargs) -> _FakeTask:
        self.calls.append((method, dict(kwargs)))
        task = _FakeTask(kwargs, self.next_task_states, self.next_task_error)
        self.tasks.append(task)
        return task

    def toDrive(self, **kwargs):  # noqa: N802
        return self._make("toDrive", **kwargs)

    def toCloudStorage(self, **kwargs):  # noqa: N802
        return self._make("toCloudStorage", **kwargs)

    def toAsset(self, **kwargs):  # noqa: N802
        return self._make("toAsset", **kwargs)


class _FakeEE:
    """A minimal stand-in for the `ee` module."""

    EEException = RuntimeError  # only needed for the non-service-account path

    def __init__(self):
        self.ic_log: list = []
        self.image_log: list = []
        self.export_image = _FakeExportImage()
        self.batch = SimpleNamespace(Export=SimpleNamespace(image=self.export_image))
        self.authenticate_calls = 0
        self.initialize_calls: list[dict] = []

    def Authenticate(self):  # noqa: N802 - mirrors the ee API
        self.authenticate_calls += 1

    def Initialize(self, **kwargs):  # noqa: N802 - mirrors the ee API
        self.initialize_calls.append(dict(kwargs))

    def ImageCollection(self, source):  # noqa: N802
        if isinstance(source, list):
            source = ("list", len(source))
        self.ic_log.append(source)
        return _FakeImageCollection(source)

    def Image(self, asset_id):  # noqa: N802
        self.image_log.append(asset_id)
        return _FakeImage(label=f"Image({asset_id})")

    @property
    def Geometry(self):  # noqa: N802
        return SimpleNamespace(Rectangle=lambda coords: _FakeGeometry(coords))


class _FakeHTTPResponse:
    """Stand-in for `requests.get(...)` exposing `.content` + `raise_for_status`."""

    def __init__(self, body: bytes):
        self.content = body
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def iter_content(self, chunk_size=None):
        """Yield the canned body in one chunk, as a streamed response would."""
        yield self.content


class _FakePyramidsHandle:
    """Stand-in for a `pyramids.dataset.Dataset` returned by `from_bytes`."""

    def __init__(self, body: bytes, no_data_value=(None,)):
        self._body = body
        # Real getDownloadURL tiles declare no no-data; the backend reads this
        # to inherit it rather than letting merge_rasters stamp its 0 default.
        self.no_data_value = no_data_value

    def to_file(self, path: str) -> None:
        from pathlib import Path as _Path

        _Path(path).write_bytes(self._body)


class _FakePyramidsDataset:
    """Stand-in for `pyramids.dataset.Dataset` — captures `from_*` calls."""

    from_bytes_calls: list[dict] = []
    from_archive_calls: list[dict] = []
    read_file_calls: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.from_bytes_calls = []
        cls.from_archive_calls = []
        cls.read_file_calls = []

    @classmethod
    def read_file(cls, path, **kwargs):
        """Record the staged path and hand back a handle over its bytes."""
        cls.read_file_calls.append(str(path))
        return _FakePyramidsHandle(Path(path).read_bytes())

    @classmethod
    def from_bytes(
        cls, data, *, suffix: str = ".tif", name=None, read_only: bool = True
    ):
        cls.from_bytes_calls.append({"data": data, "suffix": suffix})
        return _FakePyramidsHandle(data)

    @classmethod
    def from_archive(
        cls,
        url_or_path,
        *,
        kind: str = "auto",
        member_glob: str = "*",
        band_names=None,
        align: bool = False,
        no_data_value=None,
        path=None,
    ):
        cls.from_archive_calls.append(
            {
                "url_or_path": str(url_or_path),
                "kind": kind,
                "member_glob": member_glob,
                "path": path,
            }
        )
        from pathlib import Path as _Path

        if path is not None:
            _Path(path).write_bytes(b"unpacked-from-archive")


# A 4-byte big-endian TIFF magic + filler — emphatically not a zip.
_FAKE_TIFF_BYTES = b"MM\x00*" + b"\x00" * 64


def _raise_missing_extra(service_key):
    """Stand in for `credentials_for` when the optional extra is absent."""
    raise ImportError("pip install earthlens[eedai]")


def _stage_then_fail(asset_id, **kwargs):
    """Write the staged mosaic then fail, as a partway mosaic error would."""
    Path(kwargs["path"]).write_bytes(b"partial-mosaic")
    raise RuntimeError("mosaic failed")


def _always_permission_error(source, target):
    """Stand in for a rename that never gets past the lock."""
    raise PermissionError("file is in use")


def _cog_then_fail(path, **kwargs):
    """Write the COG staging file then fail, as a driver dying mid-write would."""
    Path(path).write_bytes(b"trunc-cog")
    raise RuntimeError("cog conversion failed")


def _write_then_fail(path):
    """Write a truncated raster then fail, as a driver dying mid-write would."""
    Path(path).write_bytes(b"trunc")
    raise RuntimeError("write failed")


class _FlakyReplace:
    """Fail one rename, then move the file by hand as a real one would."""

    def __init__(self) -> None:
        self.attempts = 0

    def __call__(self, source, target) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise PermissionError("file is in use")
        Path(target).write_bytes(Path(source).read_bytes())
        Path(source).unlink()


class _WriteWithSidecar:
    """Write a staged raster and drop a GDAL sidecar beside it."""

    def __init__(self, sidecar: Path) -> None:
        self.sidecar = sidecar

    def __call__(self, path) -> None:
        Path(path).write_bytes(b"eedai-tif")
        self.sidecar.write_text("<PAMDataset/>")


_REAL_EEDAI_PLAN = backend_module.GEE._eedai_single_image_plan
_PLANS_SEEN: list = []


def _recording_plan(self, var_info, band_count):
    """Delegate to the real plan, recording every verdict it hands back."""
    result = _REAL_EEDAI_PLAN(self, var_info, band_count)
    _PLANS_SEEN.append(result)
    return result


#: A metre-based projected CRS whose domain is one hemisphere. An AOI on the far
#: side transforms to `inf`, which is the only way to reach the finiteness guard
#: without hand-building a bbox the backend would never produce.
_ORTHO_CRS = "+proj=ortho +lat_0=0 +lon_0=0 +datum=WGS84 +units=m"


def _plan_for(gee, var_info, bands=1):
    """Return the routing plan the backend would compute for this request."""
    return gee._eedai_single_image_plan(var_info, bands)


def _identity_mask(image):
    """A no-op `cloud_mask` used to assert `.map` wiring (returns the image)."""
    return image


class _Raiser:
    """Callable that always raises the exception it was built with."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __call__(self, *args, **kwargs):
        raise self.error


def _adc_backend(make_gee, monkeypatch):
    """Return a `GEE` with no service-account credentials, leaving only the ADC path."""
    gee = make_gee()
    monkeypatch.delenv("GEE_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("GEE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("GEE_PROJECT", raising=False)
    return gee


# -- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="function")
def fake_ee(monkeypatch) -> _FakeEE:
    """Replace `ee` in the backend (and `create_feature`/`requests`) with fakes.

    Returns:
        _FakeEE: The fake `ee` module (its `ic_log` / `image_log` record
        constructions for assertions).
    """
    # Credentials now resolve at authenticate()/first-client-access time; provide
    # them via the environment so the lazy _open_client (and the stubbed
    # EarthEngineAuth.initialize below) resolve without constructor args.
    monkeypatch.setenv("GEE_SERVICE_ACCOUNT", "sa@x.iam")
    monkeypatch.setenv("GEE_SERVICE_KEY", "key.json")
    fake = _FakeEE()
    monkeypatch.setattr(backend_module, "ee", fake)
    # The tile fetch now runs through earthlens.base.http.RequestsGet, which
    # re-imports `requests` per call — so patch the real module's `get`, not
    # the backend module's binding.
    monkeypatch.setattr(
        requests, "get", lambda url, **kwargs: _FakeHTTPResponse(_FAKE_TIFF_BYTES)
    )
    _FakePyramidsDataset.reset()
    monkeypatch.setattr(backend_module, "PyramidsDataset", _FakePyramidsDataset)
    monkeypatch.setattr(
        backend_module,
        "create_feature",
        lambda gdf: SimpleNamespace(geometry=lambda: _FakeGeometry("from-gdf")),
    )
    monkeypatch.setattr(
        backend_module.EarthEngineAuth,
        "initialize",
        staticmethod(
            lambda service_account, service_key, project=None: project or "fake-project"
        ),
    )
    return fake


@pytest.fixture(scope="function")
def make_gee(fake_ee, tmp_path):
    """Return a factory that builds a `GEE` against the fakes.

    The factory accepts the same keyword arguments as `GEE`, with sane
    defaults (a small bbox over Egypt, `path=tmp_path`, a service
    account so the stubbed `EarthEngineAuth.initialize` runs).

    Returns:
        Callable[..., GEE]: The factory.
    """

    def _factory(**overrides) -> GEE:
        params = dict(
            start="2000-02-11",
            end="2000-02-12",
            variables={"USGS/SRTMGL1_003": ["elevation"]},
            lat_lim=[29.9, 30.0],
            lon_lim=[31.2, 31.3],
            path=str(tmp_path),
            scale=90.0,
        )
        params.update(overrides)
        return GEE(**params)

    return _factory


# -- tests ------------------------------------------------------------------


class TestInit:
    """Tests for `GEE.__init__` and the captured attributes."""

    def test_constructs_and_sets_attributes(self, make_gee):
        """A valid construction wires up the catalog and config; auth is lazy."""
        gee = make_gee()
        assert gee.catalog.get_dataset("USGS/SRTMGL1_003").ee_type == "image"
        assert gee.scale == 90.0 and gee.crs == "EPSG:4326"
        assert isinstance(gee.space, SpatialExtent) and isinstance(
            gee.time, TemporalExtent
        )
        # Opening the client lazily runs auth and resolves the project.
        assert gee.client is backend_module.ee
        assert gee.project == "fake-project"

    def test_authenticate_opens_client(self, make_gee):
        """authenticate() eagerly opens the Earth Engine connection."""
        gee = make_gee()
        assert gee.authenticate() is gee, "authenticate() returns self for chaining"
        assert gee.client is backend_module.ee
        assert gee.project == "fake-project"

    def test_bad_export_via_rejected(self, make_gee):
        """An unknown `export_via` raises `ValueError` at construction."""
        with pytest.raises(ValueError, match="export_via must be"):
            make_gee(export_via="ftp")

    def test_cloud_mask_and_filters_default_to_none(self, make_gee):
        """Omitting the hooks leaves `cloud_mask=None` and `filters=()`."""
        gee = make_gee()
        assert gee.cloud_mask is None
        assert gee.filters == ()

    def test_cloud_mask_and_filters_captured(self, make_gee):
        """The hooks are stored; `filters` is normalised to a tuple."""
        first, second = (lambda c: c), (lambda c: c)
        gee = make_gee(cloud_mask=_identity_mask, filters=[first, second])
        assert gee.cloud_mask is _identity_mask
        assert gee.filters == (first, second)

    def test_non_callable_cloud_mask_rejected(self, make_gee):
        """A non-callable `cloud_mask` raises `TypeError` at construction."""
        with pytest.raises(TypeError, match="cloud_mask must be a callable"):
            make_gee(cloud_mask="not-callable")

    def test_non_iterable_filters_rejected(self, make_gee):
        """A non-iterable `filters` raises `TypeError` at construction."""
        with pytest.raises(TypeError, match="filters must be an iterable"):
            make_gee(filters=42)

    def test_non_callable_filter_entry_rejected(self, make_gee):
        """A non-callable entry in `filters` raises `TypeError`."""
        with pytest.raises(TypeError, match="each entry in filters"):
            make_gee(filters=[lambda c: c, "nope"])

    def test_str_filters_rejected(self, make_gee):
        """A `str` (iterable of chars) is rejected rather than iterated per char."""
        with pytest.raises(TypeError, match="filters must be an iterable"):
            make_gee(filters="abc")

    def test_mapping_filters_rejected(self, make_gee):
        """A mapping is rejected up front, not iterated into its keys."""
        with pytest.raises(TypeError, match="filters must be an iterable"):
            make_gee(filters={"a": lambda c: c})

    @pytest.mark.parametrize("value", [b"abc", bytearray(b"abc"), memoryview(b"abc")])
    def test_bytes_like_filters_rejected(self, make_gee, value):
        """bytes / bytearray / memoryview are rejected up front, not iterated per byte."""
        with pytest.raises(TypeError, match="filters must be an iterable"):
            make_gee(filters=value)

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"cloud_mask": "x"}, r"cloud_mask must be a callable"),
            ({"filters": 42}, r"filters must be an iterable"),
            ({"filters": [object()]}, r"each entry in filters"),
        ],
    )
    def test_bad_hooks_fail_before_catalog_load(
        self, monkeypatch, tmp_path, kwargs, match
    ):
        """A bad `cloud_mask` / `filters` raises before the catalog parse."""
        from earthlens.gee import backend as backend_module

        class _ExplodingCatalog:
            def __init__(self, *_a, **_k):
                raise AssertionError(
                    "Catalog() must not be constructed before hook validation"
                )

        monkeypatch.setattr(backend_module, "Catalog", _ExplodingCatalog)
        with pytest.raises(TypeError, match=match):
            GEE(
                start="2000-02-11",
                end="2000-02-12",
                variables={"USGS/SRTMGL1_003": ["elevation"]},
                lat_lim=[29.9, 30.0],
                lon_lim=[31.2, 31.3],
                path=str(tmp_path),
                **kwargs,
            )

    def test_generator_filters_normalised_to_tuple(self, make_gee):
        """A one-shot generator is materialised into a reusable tuple."""
        first, second = (lambda c: c), (lambda c: c)
        gee = make_gee(filters=(f for f in (first, second)))
        assert gee.filters == (first, second)
        assert isinstance(gee.filters, tuple)

    def test_bad_export_via_fails_before_catalog_load(self, monkeypatch, tmp_path):
        """A typo'd `export_via` raises before paying for the catalog parse (M3)."""
        from earthlens.gee import backend as backend_module

        loads = 0

        class _ExplodingCatalog:
            def __init__(self, *_a, **_k):
                nonlocal loads
                loads += 1

        monkeypatch.setattr(backend_module, "Catalog", _ExplodingCatalog)
        with pytest.raises(ValueError, match="export_via must be"):
            GEE(
                start="2000-02-11",
                end="2000-02-12",
                variables={"USGS/SRTMGL1_003": ["elevation"]},
                lat_lim=[29.9, 30.0],
                lon_lim=[31.2, 31.3],
                path=str(tmp_path),
                export_via="ftp",
            )
        assert loads == 0, (
            "Catalog() should not be constructed when export_via is invalid"
        )

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"temporal_resolution": "hourly"}, r"temporal_resolution must be 'raw'"),
            ({"start": "2024-13-01"}, r"start='2024-13-01' is not parseable"),
            ({"end": "not-a-date"}, r"end='not-a-date' is not parseable"),
            (
                {"start": "2024-06-01", "end": "2024-05-01"},
                r"start='2024-06-01' is after end='2024-05-01'",
            ),
        ],
    )
    def test_bad_pure_config_fails_before_catalog_load(
        self,
        monkeypatch,
        tmp_path,
        kwargs,
        match,
    ):
        """L6: bad date / temporal_resolution short-circuits the catalog parse."""
        from earthlens.gee import backend as backend_module

        loads = 0

        class _ExplodingCatalog:
            def __init__(self, *_a, **_k):
                nonlocal loads
                loads += 1
                raise AssertionError(
                    "Catalog() must not be constructed before pure-config validation"
                )

        monkeypatch.setattr(backend_module, "Catalog", _ExplodingCatalog)
        defaults = dict(
            start="2000-02-11",
            end="2000-02-12",
            variables={"USGS/SRTMGL1_003": ["elevation"]},
            lat_lim=[29.9, 30.0],
            lon_lim=[31.2, 31.3],
            path=str(tmp_path),
        )
        defaults.update(kwargs)
        with pytest.raises(ValueError, match=match):
            GEE(**defaults)
        assert loads == 0

    def test_construct_without_credentials_is_lazy(
        self, fake_ee, tmp_path, monkeypatch
    ):
        """The constructor takes no credentials and never authenticates."""
        monkeypatch.delenv("GEE_SERVICE_ACCOUNT", raising=False)
        monkeypatch.delenv("GEE_SERVICE_KEY", raising=False)
        monkeypatch.delenv("GEE_PROJECT", raising=False)
        gee = GEE(
            start="2000-02-11",
            end="2000-02-12",
            variables={"USGS/SRTMGL1_003": ["elevation"]},
            lat_lim=[29.9, 30.0],
            lon_lim=[31.2, 31.3],
            path=str(tmp_path),
        )
        assert gee.project is None

    def test_authenticate_without_credentials_raises(
        self, fake_ee, tmp_path, monkeypatch
    ):
        """No credentials anywhere → authenticate() raises AuthenticationError."""
        from earthlens.gee.backend import AuthenticationError

        monkeypatch.delenv("GEE_SERVICE_ACCOUNT", raising=False)
        monkeypatch.delenv("GEE_SERVICE_KEY", raising=False)
        monkeypatch.delenv("GEE_PROJECT", raising=False)
        gee = GEE(
            start="2000-02-11",
            end="2000-02-12",
            variables={"USGS/SRTMGL1_003": ["elevation"]},
            lat_lim=[29.9, 30.0],
            lon_lim=[31.2, 31.3],
            path=str(tmp_path),
        )
        with pytest.raises(AuthenticationError, match="needs either service_account"):
            gee.authenticate()

    def test_authenticate_with_explicit_credentials(
        self, fake_ee, tmp_path, monkeypatch
    ):
        """Explicit service_account/service_key passed to authenticate() are used."""
        monkeypatch.delenv("GEE_SERVICE_ACCOUNT", raising=False)
        monkeypatch.delenv("GEE_SERVICE_KEY", raising=False)
        gee = GEE(
            start="2000-02-11",
            end="2000-02-12",
            variables={"USGS/SRTMGL1_003": ["elevation"]},
            lat_lim=[29.9, 30.0],
            lon_lim=[31.2, 31.3],
            path=str(tmp_path),
        )
        result = gee.authenticate(
            service_account="sa@x.iam", service_key="key.json", project="explicit-proj"
        )
        assert result is gee
        assert gee.project == "explicit-proj"  # stubbed EarthEngineAuth echoes project


class TestApplicationDefaultFallback:
    """Tests for `_open_client`'s no-service-account branch (`ee.Authenticate`)."""

    def test_authenticates_against_the_resolved_project(
        self, fake_ee, make_gee, monkeypatch
    ):
        """Without a key pair the backend falls back to application-default credentials."""
        gee = _adc_backend(make_gee, monkeypatch)
        gee.authenticate(project="adc-project")
        assert fake_ee.authenticate_calls == 1, "ee.Authenticate() was not called"
        assert fake_ee.initialize_calls == [{"project": "adc-project"}], (
            f"unexpected ee.Initialize call: {fake_ee.initialize_calls}"
        )
        assert gee.project == "adc-project", f"project not stored: {gee.project}"

    def test_an_unregistered_project_points_at_registration(
        self, fake_ee, make_gee, monkeypatch
    ):
        """The registration branch is classified on the raw message here too."""
        monkeypatch.setattr(
            fake_ee,
            "Initialize",
            _Raiser(
                fake_ee.EEException("Project p is not registered to use Earth Engine")
            ),
        )
        gee = _adc_backend(make_gee, monkeypatch)
        with pytest.raises(backend_module.AuthenticationError) as excinfo:
            gee.authenticate(project="p")
        rendered = str(excinfo.value)
        assert "Register it at" in rendered, f"no registration pointer: {rendered}"
        assert excinfo.value.__cause__ is None, "the exception chain was not broken"

    def test_a_failure_never_reports_adc_credential_material(
        self, fake_ee, make_gee, monkeypatch
    ):
        """An ADC file is an `authorized_user` JSON: no PEM armour, but still secret."""
        raw = (
            'could not load {"client_secret": "SUPERSECRET", '
            '"refresh_token": "ALSOSECRET"}'
        )
        monkeypatch.setattr(fake_ee, "Initialize", _Raiser(fake_ee.EEException(raw)))
        gee = _adc_backend(make_gee, monkeypatch)
        with pytest.raises(backend_module.AuthenticationError) as excinfo:
            gee.authenticate(project="p")
        rendered = str(excinfo.value)
        assert "SUPERSECRET" not in rendered, f"a client secret survived: {rendered}"
        assert "ALSOSECRET" not in rendered, f"a refresh token survived: {rendered}"
        assert "<service key redacted>" in rendered, f"nothing was redacted: {rendered}"
        assert excinfo.value.__cause__ is None, "the exception chain was not broken"

    def test_a_non_ee_failure_is_wrapped_and_unchained(
        self, fake_ee, make_gee, monkeypatch
    ):
        """A failure that is not an `EEException` is wrapped, redacted, and unchained."""
        monkeypatch.setattr(
            fake_ee, "Authenticate", _Raiser(OSError('no ADC file: "private_key": "x"'))
        )
        gee = _adc_backend(make_gee, monkeypatch)
        with pytest.raises(
            backend_module.AuthenticationError, match="initialisation failed"
        ) as excinfo:
            gee.authenticate(project="p")
        assert "<service key redacted>" in str(excinfo.value), (
            f"the generic branch did not redact: {excinfo.value}"
        )
        assert excinfo.value.__cause__ is None, "the exception chain was not broken"


class TestCheckInputDates:
    """Tests for `GEE._check_input_dates`."""

    def test_raw_single_bucket(self, make_gee):
        """`temporal_resolution="raw"` → one date (the start)."""
        gee = make_gee(start="2020-01-01", end="2020-01-31", temporal_resolution="raw")
        assert len(gee.time.dates) == 1
        assert gee.time.dates[0] == pd.Timestamp("2020-01-01")
        assert gee.time.resolution == "raw"

    @pytest.mark.parametrize(
        "resolution, start, end, expected_n",
        [
            ("daily", "2020-01-01", "2020-01-05", 5),
            ("monthly", "2020-01-01", "2020-03-15", 3),
            ("yearly", "2018-06-01", "2021-06-01", 3),
        ],
    )
    def test_periodic_buckets(self, make_gee, resolution, start, end, expected_n):
        """daily / monthly / yearly produce the expected number of buckets."""
        gee = make_gee(start=start, end=end, temporal_resolution=resolution)
        assert len(gee.time.dates) == expected_n
        assert gee.time.resolution == resolution

    def test_unknown_resolution_raises(self, make_gee):
        """An unknown `temporal_resolution` raises `ValueError`."""
        with pytest.raises(ValueError, match="must be 'raw', 'daily', 'monthly', or"):
            make_gee(temporal_resolution="hourly")

    def test_start_after_end_raises(self, make_gee):
        """`start` later than `end` raises `ValueError`."""
        with pytest.raises(ValueError):
            make_gee(start="2020-06-01", end="2020-01-01")


class TestCreateGrid:
    """Tests for `GEE._create_grid`."""

    def test_returns_spatial_extent_without_resolution(self, make_gee):
        """The bbox is captured as a `SpatialExtent` with no `resolution`."""
        gee = make_gee(lat_lim=[10.0, 20.0], lon_lim=[-5.0, 5.0])
        assert gee.space.latitude_min == 10.0 and gee.space.latitude_max == 20.0
        assert gee.space.longitude_min == -5.0 and gee.space.longitude_max == 5.0
        assert gee.space.resolution is None


class TestClampWindowToExtent:
    """Tests for `GEE._clamp_window_to_extent`."""

    def test_overlap_clamps_to_dataset_extent(self, make_gee):
        """The window is clamped to the dataset's published extent."""
        gee = make_gee(start="1999-01-01", end="2010-01-01")
        ds = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        start, end_excl = gee._clamp_window_to_extent(ds)
        assert start == dt.datetime(2000, 2, 11)
        assert end_excl == dt.datetime(2000, 2, 23)

    def test_no_overlap_returns_none(self, make_gee):
        """A window entirely after the dataset's extent yields `(None, None)`."""
        gee = make_gee(start="2020-01-01", end="2020-01-02")
        ds = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        assert gee._clamp_window_to_extent(ds) == (None, None)

    def test_open_ended_dataset_clamps_to_now(self, make_gee, monkeypatch):
        """For a dataset with `end_date: null`, the upper bound is "now + 1 day"."""
        fixed_now = dt.datetime(2026, 5, 13)

        class _FixedDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        monkeypatch.setattr(backend_module.dt, "datetime", _FixedDatetime)
        gee = make_gee(
            start="2020-01-01",
            end="2099-01-01",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        start, end_excl = gee._clamp_window_to_extent(ds)
        assert start == dt.datetime(2020, 1, 1)
        assert end_excl == fixed_now + dt.timedelta(days=1)


class TestDiscoverExtent:
    """Tests for the EE-side extent-discovery fallback (L3)."""

    def test_default_does_not_query_ee(self, make_gee, monkeypatch):
        """`discover_extent=False` (the default) never calls `_discover_ee_extent`."""
        calls: list[str] = []
        monkeypatch.setattr(
            GEE,
            "_discover_ee_extent",
            lambda self, var_info: calls.append(var_info.id) or (None, None),
        )
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            start="2024-01-01",
            end="2024-01-02",
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._clamp_window_to_extent(ds)
        assert calls == []

    def test_discover_extent_clamps_to_ee_max_when_catalog_open_ended(
        self,
        make_gee,
        monkeypatch,
    ):
        """For `end_date is None`, EE max becomes the upper bound (cached per asset)."""
        ee_max = dt.datetime(2025, 12, 15)
        calls: list[str] = []

        def _fake_discover(self, var_info):
            calls.append(var_info.id)
            return dt.datetime(1981, 1, 1), ee_max

        monkeypatch.setattr(GEE, "_discover_ee_extent", _fake_discover)
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            start="2020-01-01",
            end="2099-01-01",
            discover_extent=True,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        start, end_excl = gee._clamp_window_to_extent(ds)
        assert start == dt.datetime(2020, 1, 1)
        assert end_excl == ee_max + dt.timedelta(days=1)

        # Second call hits the cache.
        gee._clamp_window_to_extent(ds)
        assert calls == ["UCSB-CHG/CHIRPS/DAILY"]

    def test_discover_extent_falls_back_to_now_on_failure(self, make_gee, monkeypatch):
        """If `_discover_ee_extent` returns `(None, None)`, fall back to `now()`."""
        fixed_now = dt.datetime(2026, 5, 13)

        class _FixedDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        monkeypatch.setattr(backend_module.dt, "datetime", _FixedDatetime)
        monkeypatch.setattr(
            GEE,
            "_discover_ee_extent",
            lambda self, var_info: (None, None),
        )
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            start="2020-01-01",
            end="2099-01-01",
            discover_extent=True,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        _, end_excl = gee._clamp_window_to_extent(ds)
        assert end_excl == fixed_now + dt.timedelta(days=1)

    def test_real_discover_queries_reduce_columns(self, make_gee, monkeypatch):
        """`_discover_ee_extent` issues `reduceColumns(minMax, system:time_start)`."""
        captured: dict = {}

        class _FakeReducer:
            @staticmethod
            def minMax():  # noqa: N802
                return "REDUCER:minMax"

        class _FakeReducedFC:
            def getInfo(self):  # noqa: N802
                # Earth Engine returns a dict like {"min": <ms>, "max": <ms>}.
                return {"min": 1700000000000, "max": 1750000000000}

        class _FakeCollection:
            def __init__(self, asset_id):
                captured["asset"] = asset_id

            def reduceColumns(self, reducer, properties):  # noqa: N802
                captured["reducer"] = reducer
                captured["properties"] = properties
                return _FakeReducedFC()

        gee = make_gee(discover_extent=True)
        monkeypatch.setattr(
            backend_module.ee, "ImageCollection", _FakeCollection, raising=False
        )
        monkeypatch.setattr(backend_module.ee, "Reducer", _FakeReducer, raising=False)

        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        min_dt, max_dt = gee._discover_ee_extent(ds)
        assert captured["asset"] == "UCSB-CHG/CHIRPS/DAILY"
        assert captured["reducer"] == "REDUCER:minMax"
        assert captured["properties"] == ["system:time_start"]
        assert min_dt == dt.datetime.fromtimestamp(
            1700000000, tz=dt.timezone.utc
        ).replace(tzinfo=None)
        assert max_dt == dt.datetime.fromtimestamp(
            1750000000, tz=dt.timezone.utc
        ).replace(tzinfo=None)

    def test_real_discover_swallows_ee_errors(self, make_gee, monkeypatch):
        """Network / EE failures inside `_discover_ee_extent` return `(None, None)`."""

        class _ExplodingCollection:
            def __init__(self, asset_id):
                pass

            def reduceColumns(self, reducer, properties):  # noqa: N802
                raise RuntimeError("network down")

        gee = make_gee(discover_extent=True)
        monkeypatch.setattr(
            backend_module.ee, "ImageCollection", _ExplodingCollection, raising=False
        )
        monkeypatch.setattr(
            backend_module.ee,
            "Reducer",
            SimpleNamespace(minMax=lambda: "REDUCER:minMax"),
            raising=False,
        )

        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        assert gee._discover_ee_extent(ds) == (None, None)

    def test_real_discover_returns_none_on_empty_minmax(self, make_gee, monkeypatch):
        """An empty collection (no `min`/`max` keys) yields `(None, None)`."""

        class _EmptyReducedFC:
            def getInfo(self):  # noqa: N802
                return {}

        class _EmptyCollection:
            def __init__(self, asset_id):
                pass

            def reduceColumns(self, reducer, properties):  # noqa: N802
                return _EmptyReducedFC()

        gee = make_gee(discover_extent=True)
        monkeypatch.setattr(
            backend_module.ee, "ImageCollection", _EmptyCollection, raising=False
        )
        monkeypatch.setattr(
            backend_module.ee,
            "Reducer",
            SimpleNamespace(minMax=lambda: "REDUCER:minMax"),
            raising=False,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        assert gee._discover_ee_extent(ds) == (None, None)

    def test_extent_cache_survives_a_none_result(self, make_gee, monkeypatch):
        """A `(None, None)` result still populates the cache (no repeat queries)."""
        calls: list[str] = []

        def _fake_discover(self, var_info):
            calls.append(var_info.id)
            return None, None

        monkeypatch.setattr(GEE, "_discover_ee_extent", _fake_discover)
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            start="2020-01-01",
            end="2099-01-01",
            discover_extent=True,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._maybe_discover_ee_extent(ds)
        gee._maybe_discover_ee_extent(ds)
        assert calls == ["UCSB-CHG/CHIRPS/DAILY"]

    def test_extent_cache_is_shared_across_gee_instances(
        self,
        make_gee,
        monkeypatch,
    ):
        """L5: a second `GEE(...)` reuses the cached extent — no extra round trip."""
        calls: list[str] = []

        def _fake_discover(self, var_info):
            calls.append(var_info.id)
            return (
                dt.datetime(2020, 1, 1),
                dt.datetime(2024, 12, 31),
            )

        monkeypatch.setattr(GEE, "_discover_ee_extent", _fake_discover)

        gee_a = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            start="2020-01-01",
            end="2099-01-01",
            discover_extent=True,
        )
        ds = gee_a.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee_a._maybe_discover_ee_extent(ds)

        # Fresh instance — should hit the module-level cache, not re-query EE.
        gee_b = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            start="2020-01-01",
            end="2099-01-01",
            discover_extent=True,
        )
        gee_b._maybe_discover_ee_extent(ds)

        assert calls == ["UCSB-CHG/CHIRPS/DAILY"], (
            f"second instance should hit the shared cache; got {calls}"
        )

    def test_clear_extent_cache_drops_every_entry(self, make_gee, monkeypatch):
        """`clear_extent_cache()` forces the next call to re-query EE."""
        from earthlens.gee.backend import clear_extent_cache

        calls: list[str] = []

        def _fake_discover(self, var_info):
            calls.append(var_info.id)
            return (None, None)

        monkeypatch.setattr(GEE, "_discover_ee_extent", _fake_discover)
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            start="2020-01-01",
            end="2099-01-01",
            discover_extent=True,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._maybe_discover_ee_extent(ds)
        clear_extent_cache()
        gee._maybe_discover_ee_extent(ds)
        assert calls == ["UCSB-CHG/CHIRPS/DAILY", "UCSB-CHG/CHIRPS/DAILY"]


class TestDownloadRejections:
    """Tests for invalid / not-yet-supported configurations."""

    def test_aggregate_rejected(self, make_gee):
        """Passing `aggregate=` raises `NotImplementedError`."""
        with pytest.raises(NotImplementedError, match="aggregate="):
            make_gee().download(aggregate=object(), progress_bar=False)

    def test_drive_without_folder_rejected_at_construction(self, make_gee):
        """`export_via="drive"` requires `drive_folder` at construction."""
        with pytest.raises(
            ValueError, match="export_via='drive' requires drive_folder"
        ):
            make_gee(export_via="drive")

    def test_gcs_without_bucket_rejected_at_construction(self, make_gee):
        """`export_via="gcs"` requires `gcs_bucket` at construction."""
        with pytest.raises(ValueError, match="export_via='gcs' requires gcs_bucket"):
            make_gee(export_via="gcs")


class TestExportViaBatch:
    """Tests for the asynchronous `export_via="drive"` / `"gcs"` paths."""

    def test_drive_export_queues_polls_and_returns_destination(self, make_gee):
        """A Drive export queues a `toDrive` task, polls it, and returns `drive://...`."""
        gee = make_gee(export_via="drive", drive_folder="ee_out")
        results = gee.download(progress_bar=False)
        assert results == ["drive://ee_out/USGS_SRTMGL1_003_elevation_20000211"]
        ((method, kwargs),) = gee.client.export_image.calls
        assert method == "toDrive"
        assert kwargs["folder"] == "ee_out" and kwargs["scale"] == 90.0
        assert kwargs["crs"] == "EPSG:4326" and kwargs["maxPixels"] == 1e13
        assert kwargs["fileNamePrefix"] == "USGS_SRTMGL1_003_elevation_20000211"

    def test_gcs_export_uses_to_cloud_storage(self, make_gee):
        """A GCS export queues a `toCloudStorage` task and returns `gs://...`."""
        gee = make_gee(export_via="gcs", gcs_bucket="my-bucket")
        results = gee.download(progress_bar=False)
        assert results == ["gs://my-bucket/USGS_SRTMGL1_003_elevation_20000211"]
        ((method, kwargs),) = gee.client.export_image.calls
        assert method == "toCloudStorage" and kwargs["bucket"] == "my-bucket"

    def test_failed_export_task_raises(self, make_gee):
        """A `FAILED` export task surfaces as a `RuntimeError` with the message."""
        gee = make_gee(export_via="drive", drive_folder="ee_out")
        gee.client.export_image.next_task_states = ["FAILED"]
        gee.client.export_image.next_task_error = "out of quota"
        with pytest.raises(RuntimeError, match="ended FAILED: out of quota"):
            gee.download(progress_bar=False)

    def test_task_is_started_and_polled(self, make_gee):
        """`wait_for_task` calls `task.start()` and then polls `task.status()`."""
        gee = make_gee(export_via="drive", drive_folder="ee_out")
        gee.download(progress_bar=False)
        task = gee.client.export_image.tasks[0]
        assert task.started is True and task.poll_count >= 1


class TestWaitForExport:
    """Tests for the `wait_for_export=False` non-blocking path (jobs-plan M1)."""

    def test_default_blocks_and_returns_destination_string(self, make_gee):
        """`wait_for_export=True` (the default) returns a `drive://...` string."""
        gee = make_gee(export_via="drive", drive_folder="ee_out")
        results = gee.download(progress_bar=False)
        assert results == ["drive://ee_out/USGS_SRTMGL1_003_elevation_20000211"]

    def test_non_blocking_returns_task_info_and_starts_task(self, make_gee):
        """`wait_for_export=False` starts the task and returns a `TaskInfo`."""
        from earthlens.gee import TaskInfo

        gee = make_gee(export_via="drive", drive_folder="ee_out", wait_for_export=False)
        results = gee.download(progress_bar=False)
        assert len(results) == 1
        info = results[0]
        assert isinstance(info, TaskInfo)
        # The fake task starts immediately + reports its default `COMPLETED`
        # state in `task.status()` even before any polling, so the adapter
        # captures a fully-populated TaskInfo.
        task = gee.client.export_image.tasks[0]
        assert task.started is True

    def test_non_blocking_does_not_poll(self, make_gee):
        """In `wait_for_export=False`, the backend does not call `wait_for_task`."""
        gee = make_gee(export_via="drive", drive_folder="ee_out", wait_for_export=False)
        gee.download(progress_bar=False)
        task = gee.client.export_image.tasks[0]
        # `wait_for_task` would have polled `task.status()` at least once;
        # the non-blocking path also calls `task.status()` once (to build
        # the `TaskInfo`), so `poll_count == 1` is correct.
        assert task.poll_count == 1


class TestExportViaAsset:
    """Tests for the `export_via="asset"` path (M1)."""

    def test_missing_asset_id_rejected_at_construction(self, make_gee):
        """`export_via="asset"` without an `asset_id=` raises a clear `ValueError`."""
        with pytest.raises(ValueError, match="export_via='asset' requires asset_id"):
            make_gee(export_via="asset")

    def test_unknown_export_via_lists_asset_too(self, make_gee):
        """The updated ValueError message advertises `'asset'` as a valid sink."""
        with pytest.raises(ValueError, match="'url', 'drive', 'gcs', or 'asset'"):
            make_gee(export_via="ftp")

    def test_asset_export_queues_to_asset_and_returns_ee_uri(self, make_gee):
        """An Asset export queues a `toAsset` task and returns `ee://<asset>/<prefix>`."""
        gee = make_gee(export_via="asset", asset_id="projects/p/assets/my-folder")
        results = gee.download(progress_bar=False)
        assert results == [
            "ee://projects/p/assets/my-folder/USGS_SRTMGL1_003_elevation_20000211"
        ]
        ((method, kwargs),) = gee.client.export_image.calls
        assert method == "toAsset"
        assert (
            kwargs["assetId"]
            == "projects/p/assets/my-folder/USGS_SRTMGL1_003_elevation_20000211"
        )
        assert "fileNamePrefix" not in kwargs  # `toAsset` doesn't use it
        assert kwargs["scale"] == 90.0 and kwargs["maxPixels"] == 1e13

    def test_trailing_slash_on_asset_id_is_tolerated(self, make_gee):
        """A trailing `/` on `asset_id` is stripped to avoid a double slash."""
        gee = make_gee(export_via="asset", asset_id="projects/p/assets/my-folder/")
        gee.download(progress_bar=False)
        ((_, kwargs),) = gee.client.export_image.calls
        assert "//" not in kwargs["assetId"].split(":", 1)[-1]
        assert kwargs["assetId"].startswith("projects/p/assets/my-folder/")
        assert "//" not in kwargs["assetId"]


class TestBuildCollection:
    """Tests for `GEE._build_collection`."""

    def test_image_collection_chain(self, make_gee):
        """A collection dataset is filtered by date, bounds, then bands."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]}, scale=5566.0
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 2)
        )
        assert col.method_names() == ["filterDate", "filterBounds", "select"]

    def test_static_image_skips_filter_date(self, make_gee, monkeypatch):
        """A hookless static image dataset is not date-filtered and logs no warning."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee()
        ds = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        col = gee._build_collection(
            ds, ["elevation"], dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 13)
        )
        assert col.method_names() == ["filterBounds", "select"]
        assert gee.client.image_log == ["USGS/SRTMGL1_003"]
        assert warnings == []

    def test_filters_applied_after_bounds_before_select(self, make_gee):
        """Constructor `filters` are applied left to right, after bounds."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            filters=[lambda c: c.filter("a"), lambda c: c.filter("b")],
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 2)
        )
        assert col.method_names() == [
            "filterDate",
            "filterBounds",
            "filter",
            "filter",
            "select",
        ]
        assert [args for name, args in col.calls if name == "filter"] == [
            ("a",),
            ("b",),
        ]

    def test_cloud_mask_mapped_before_select(self, make_gee):
        """A `cloud_mask` is `.map`-applied before the band `select`."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            cloud_mask=_identity_mask,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 2)
        )
        assert col.method_names() == ["filterDate", "filterBounds", "map", "select"]
        # The exact callable is threaded through to `.map`.
        assert [args for name, args in col.calls if name == "map"] == [
            (_identity_mask,)
        ]

    def test_filters_then_cloud_mask_then_select(self, make_gee, monkeypatch):
        """On a collection the order is filters → cloud_mask → select, with no warning."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            filters=[lambda c: c.filter("cc")],
            cloud_mask=_identity_mask,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 2)
        )
        assert col.method_names() == [
            "filterDate",
            "filterBounds",
            "filter",
            "map",
            "select",
        ]
        assert warnings == []

    def test_real_cloud_mask_threaded_through_build(self, make_gee):
        """A real mask (`sentinel2_scl`) is the exact callable handed to `.map`."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            cloud_mask=sentinel2_scl,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 2)
        )
        assert [args for name, args in col.calls if name == "map"] == [(sentinel2_scl,)]

    def test_static_image_applies_filters_and_cloud_mask(self, make_gee, monkeypatch):
        """A static image dataset applies the hooks before select and logs a warning."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee(
            filters=[lambda c: c.filter("cc")],
            cloud_mask=_identity_mask,
        )
        ds = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        col = gee._build_collection(
            ds, ["elevation"], dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 13)
        )
        assert col.method_names() == ["filterBounds", "filter", "map", "select"]
        assert any("static single-image" in w for w in warnings), warnings

    @pytest.mark.parametrize(
        "hooks",
        [
            {"filters": [lambda c: c.filter("cc")]},
            {"cloud_mask": _identity_mask},
        ],
    )
    def test_static_image_single_hook_warns(self, make_gee, monkeypatch, hooks):
        """A static image warns when only one of filters / cloud_mask is set."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee(**hooks)
        ds = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._build_collection(
            ds, ["elevation"], dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 13)
        )
        assert any("static single-image" in w for w in warnings), warnings


class TestComposite:
    """Tests for `GEE._composite`."""

    def test_raw_yields_single_bucket(self, make_gee):
        """`temporal_resolution="raw"` yields one `(start, image)` bucket."""
        gee = make_gee(
            start="2020-06-01",
            end="2020-06-30",
            temporal_resolution="raw",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)
        )
        buckets = list(
            gee._composite(col, ds, dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1))
        )
        assert len(buckets) == 1
        when, image, _bs, _be = buckets[0]
        assert when == dt.datetime(2020, 6, 1)
        assert image.reducer == "mean"

    def test_monthly_yields_one_bucket_per_month(self, make_gee):
        """Monthly resolution splits the window into per-month buckets."""
        gee = make_gee(
            start="2020-06-01",
            end="2020-07-31",
            temporal_resolution="monthly",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 8, 1)
        )
        buckets = list(
            gee._composite(col, ds, dt.datetime(2020, 6, 1), dt.datetime(2020, 8, 1))
        )
        assert [b[0] for b in buckets] == [
            dt.datetime(2020, 6, 1),
            dt.datetime(2020, 7, 1),
        ]
        assert all(b[1].reducer == "mean" for b in buckets)

    def test_monthly_maps_cloud_mask_once_not_per_bucket(self, make_gee):
        """The `cloud_mask` is `.map`-applied once at build, not re-applied per bucket."""
        gee = make_gee(
            start="2020-06-01",
            end="2020-07-31",
            temporal_resolution="monthly",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            cloud_mask=_identity_mask,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 8, 1)
        )
        assert col.method_names().count("map") == 1
        buckets = list(
            gee._composite(col, ds, dt.datetime(2020, 6, 1), dt.datetime(2020, 8, 1))
        )
        assert len(buckets) == 2
        assert all(b[1].reducer == "mean" for b in buckets)

    def test_static_image_one_bucket_regardless_of_resolution(self, make_gee):
        """A static `image` dataset always yields a single bucket."""
        gee = make_gee(temporal_resolution="monthly")
        ds = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        col = gee._build_collection(
            ds, ["elevation"], dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 13)
        )
        buckets = list(
            gee._composite(col, ds, dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 13))
        )
        assert len(buckets) == 1

    def test_reducer_override(self, make_gee):
        """The constructor `reducer` overrides the dataset's `default_reducer`."""
        gee = make_gee(
            reducer="median",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-02",
            scale=5566.0,
        )
        ds = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        col = gee._build_collection(
            ds, ["precipitation"], dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 3)
        )
        ((_, image, _bs, _be),) = gee._composite(
            col, ds, dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 3)
        )
        assert image.reducer == "median"


class TestApi:
    """Tests for `GEE._api`."""

    @pytest.fixture(autouse=True)
    def _force_ee_engine(self, monkeypatch):
        """Keep these `getDownloadURL` tests on Earth Engine (no `[eedai]`)."""
        monkeypatch.setattr(backend_module, "eedai_available", lambda: False)

    def test_size_guard_rejects_oversized_request(self, make_gee):
        """A bbox×scale exceeding 32768 px per axis raises a clear `ValueError`."""
        gee = make_gee(
            start="2000-02-11",
            end="2000-02-12",
            lat_lim=[0.0, 40.0],
            lon_lim=[0.0, 40.0],
            scale=30.0,
        )
        with pytest.raises(ValueError, match="32768-px"):
            gee.download(progress_bar=False)

    def test_missing_scale_raises(self, make_gee):
        """`_api` raises when there is no `scale` and no dataset `spatial_resolution`."""
        gee = make_gee(scale=None)
        gee.scale = None
        bare = Dataset(
            id="DEMO/IMG",
            title="x",
            ee_type="image",
            extent=Extent(start_date="2000-01-01"),
            spatial_resolution=None,
        )
        with pytest.raises(ValueError, match="no output scale"):
            gee._api(
                _FakeImage(),
                bare,
                ["b"],
                dt.datetime(2000, 1, 1),
                dt.datetime(2000, 1, 1),
                dt.datetime(2000, 1, 2),
            )

    def test_successful_download_writes_geotiff(self, make_gee, tmp_path):
        """A within-limits request writes a `.tif` and returns its path."""
        gee = make_gee()
        paths = gee.download(progress_bar=False)
        assert len(paths) == 1
        target = paths[0]
        assert target.name == "USGS_SRTMGL1_003_elevation_20000211.tif"
        assert target.parent == tmp_path
        assert target.read_bytes() == _FAKE_TIFF_BYTES
        assert not zipfile.is_zipfile(target)

    def test_zip_response_unpacked_via_pyramids(self, make_gee, monkeypatch, tmp_path):
        """A multi-band zip response is routed through `Dataset.from_archive`.

        Earth Engine returns a zip-of-tifs when the request asks for several
        bands; the backend writes the body to `<prefix>.zip` and unpacks it
        into the target via pyramids, then deletes the zip.
        """
        import io

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("inner.tif", b"data")
        monkeypatch.setattr(
            requests, "get", lambda url, **kwargs: _FakeHTTPResponse(buf.getvalue())
        )
        paths = make_gee().download(progress_bar=False)
        target = paths[0]
        assert target.exists()
        assert target.read_bytes() == b"unpacked-from-archive"
        assert not (tmp_path / f"{target.stem}.zip").exists()  # cleaned up
        assert len(_FakePyramidsDataset.from_archive_calls) == 1
        call = _FakePyramidsDataset.from_archive_calls[0]
        assert call["kind"] == "zip"
        assert call["member_glob"] == "*.tif"
        assert call["path"] == str(target)

    def test_http_timeout_passthrough(self, make_gee, monkeypatch):
        """`http_timeout=` is forwarded verbatim to `requests.get`."""
        captured: dict = {}

        def _capture_get(url, timeout=None, **kwargs):
            captured["timeout"] = timeout
            return _FakeHTTPResponse(_FAKE_TIFF_BYTES)

        monkeypatch.setattr(requests, "get", _capture_get)
        make_gee(http_timeout=42.5).download(progress_bar=False)
        assert captured["timeout"] == 42.5

    def test_download_passes_geotiff_format_and_scale(self, make_gee):
        """The `getDownloadURL` request uses `format="GEO_TIFF"`, the scale, and the CRS."""
        gee = make_gee(scale=120.0, crs="EPSG:3857")
        # Reach the image the pipeline produced by re-running the build/composite:
        ds = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        col = gee._build_collection(
            ds, ["elevation"], dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 13)
        )
        ((_, image, _bs, _be),) = gee._composite(
            col, ds, dt.datetime(2000, 2, 11), dt.datetime(2000, 2, 13)
        )
        gee._api(
            image,
            ds,
            ["elevation"],
            dt.datetime(2000, 2, 11),
            dt.datetime(2000, 1, 1),
            dt.datetime(2000, 1, 2),
        )
        assert image.download_params["format"] == "GEO_TIFF"
        assert image.download_params["scale"] == 120.0
        assert image.download_params["crs"] == "EPSG:3857"


class TestAutoSplit:
    """Tests for `auto_split=True` (H2 — auto-split oversized URL downloads)."""

    @pytest.fixture(autouse=True)
    def _force_ee_engine(self, monkeypatch):
        """Keep these `getDownloadURL` tests on Earth Engine (no `[eedai]`)."""
        monkeypatch.setattr(backend_module, "eedai_available", lambda: False)

    def test_default_keeps_existing_value_error(self, make_gee):
        """`auto_split=False` (the default) preserves the historical guard."""
        gee = make_gee(lat_lim=[0.0, 40.0], lon_lim=[0.0, 40.0], scale=30.0)
        with pytest.raises(ValueError, match="auto_split=True"):
            gee.download(progress_bar=False)

    def test_ctor_arg_persists(self, make_gee):
        """`auto_split=True` is captured on the instance."""
        assert make_gee(auto_split=True).auto_split is True
        assert make_gee().auto_split is False

    def test_oversized_aoi_is_tiled_and_merged(self, make_gee, monkeypatch, tmp_path):
        """An oversized AOI with `auto_split=True` downloads N tiles + mosaics."""
        merge_calls: list[dict] = []

        def _fake_merge(src, dst, **kwargs):
            from pathlib import Path as _Path

            merge_calls.append({"src": list(src), "dst": str(dst), "kwargs": kwargs})
            _Path(dst).write_bytes(b"merged")

        monkeypatch.setattr(backend_module, "merge_rasters", _fake_merge)

        gee = make_gee(
            lat_lim=[0.0, 40.0], lon_lim=[0.0, 40.0], scale=30.0, auto_split=True
        )
        paths = gee.download(progress_bar=False)

        assert len(paths) == 1
        target = paths[0]
        assert target.parent == tmp_path
        assert target.name == "USGS_SRTMGL1_003_elevation_20000211.tif"
        assert target.read_bytes() == b"merged"

        assert len(merge_calls) == 1
        assert merge_calls[0]["dst"] == str(target)
        assert merge_calls[0]["kwargs"]["no_data_value"] == "none"
        assert len(merge_calls[0]["src"]) > 1
        for tile_path in merge_calls[0]["src"]:
            assert tile_path.endswith(".tif")
            assert "_tile_" in tile_path
            assert not Path(tile_path).exists()  # tile files cleaned up post-merge

    def test_each_tile_request_is_within_the_synchronous_cap(
        self, make_gee, monkeypatch
    ):
        """Every `getDownloadURL` call's `region` covers <= EE_MAX_DIMENSION px."""
        from earthlens.gee._helpers import EE_MAX_DIMENSION as _CAP

        monkeypatch.setattr(
            backend_module,
            "merge_rasters",
            lambda src, dst, **k: Path(dst).write_bytes(b"merged"),
        )

        gee = make_gee(
            lat_lim=[0.0, 40.0], lon_lim=[0.0, 40.0], scale=30.0, auto_split=True
        )
        gee.download(progress_bar=False)

        # The fake `ee.Image` records every `getDownloadURL` call's params.
        # The fake `ee.Geometry.Rectangle` returns a `_FakeGeometry` with
        # `.coords` set to `[west, south, east, north]`. The tile is at-cap
        # when its width-in-degrees ≤ EE_MAX_DIMENSION * (scale / METRES_PER_DEGREE).
        from earthlens.base.spatial import METRES_PER_DEGREE

        deg_per_px = 30.0 / METRES_PER_DEGREE
        cap_deg = _CAP * deg_per_px
        # Replay the image's recorded download_params_list:
        ds = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        col = gee._build_collection(
            ds,
            ["elevation"],
            dt.datetime(2000, 2, 11),
            dt.datetime(2000, 2, 13),
        )
        ((_, image, _bs, _be),) = gee._composite(
            col,
            ds,
            dt.datetime(2000, 2, 11),
            dt.datetime(2000, 2, 13),
        )
        gee._api(
            image,
            ds,
            ["elevation"],
            dt.datetime(2000, 2, 11),
            dt.datetime(2000, 1, 1),
            dt.datetime(2000, 1, 2),
        )

        assert len(image.download_params_list) > 1
        for call in image.download_params_list:
            west, south, east, north = call["region"].coords
            assert (east - west) <= cap_deg + 1e-9
            assert (north - south) <= cap_deg + 1e-9


class TestEeRegion:
    """Tests for `GEE._ee_region`."""

    def test_bbox_rectangle_when_no_region(self, make_gee):
        """With no `region` GeoDataFrame, the clip geometry is an `ee.Geometry.Rectangle`."""
        gee = make_gee(lat_lim=[10.0, 20.0], lon_lim=[-5.0, 5.0])
        region = gee._ee_region()
        assert isinstance(region, _FakeGeometry)
        assert region.coords == [-5.0, 10.0, 5.0, 20.0]
        assert gee._ee_region() is region

    def test_geodataframe_region_uses_create_feature(self, make_gee):
        """A `region` GeoDataFrame is routed through `features.create_feature`."""
        sentinel_gdf = object()
        gee = make_gee(region=sentinel_gdf)
        region = gee._ee_region()
        assert isinstance(region, _FakeGeometry) and region.coords == "from-gdf"

    def test_polygon_aoi_used_when_no_region(self, make_gee):
        """A polygon aoi= (no region=) clips through create_feature too."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        poly = shapely.geometry.Polygon([(-5, 10), (5, 10), (0, 20)])
        gdf = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
        gee = make_gee(aoi=gdf, lat_lim=None, lon_lim=None)
        assert gee.space.geometry is not None, "polygon aoi should attach a mask"
        region = gee._ee_region()
        assert isinstance(region, _FakeGeometry) and region.coords == "from-gdf"

    def test_bbox_aoi_still_uses_rectangle(self, make_gee):
        """A bbox aoi= attaches no mask, so the clip stays an ee Rectangle."""
        gee = make_gee(aoi=[-5.0, 10.0, 5.0, 20.0], lat_lim=None, lon_lim=None)
        assert gee.space.geometry is None, "a bbox aoi attaches no mask"
        region = gee._ee_region()
        assert region.coords == [
            -5.0,
            10.0,
            5.0,
            20.0,
        ], f"bad rectangle: {region.coords}"


class TestDownloadEndToEnd:
    """An end-to-end `download()` over the fakes."""

    def test_multi_bucket_collection_download(self, make_gee, tmp_path):
        """A monthly CHIRPS request writes one GeoTIFF per month."""
        gee = make_gee(
            start="2020-06-01",
            end="2020-07-31",
            temporal_resolution="monthly",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
        )
        paths = gee.download(progress_bar=False)
        names = sorted(p.name for p in paths)
        assert names == [
            "UCSB-CHG_CHIRPS_DAILY_precipitation_20200601.tif",
            "UCSB-CHG_CHIRPS_DAILY_precipitation_20200701.tif",
        ]
        assert all(p.parent == tmp_path for p in paths)

    def test_non_overlapping_window_writes_nothing(self, make_gee):
        """A request window outside a dataset's extent yields no files."""
        gee = make_gee(start="2020-01-01", end="2020-01-02")
        assert gee.download(progress_bar=False) == []


def test_gee_declares_raster_output_kind():
    """GEE declares OUTPUT_KIND='raster' so the facade forwards aggregate=."""
    assert GEE.OUTPUT_KIND == "raster"


class TestGeeStreams:
    """L7: the getDownloadURL body must reach disk without being buffered.

    The other five ARC-4b migrations each carry a lock like this; gee was the
    only site without one, and its fake still exposed `.content`.
    """

    @pytest.fixture(autouse=True)
    def _force_ee_engine(self, monkeypatch):
        """Keep these `getDownloadURL` tests on Earth Engine (no `[eedai]`)."""
        monkeypatch.setattr(backend_module, "eedai_available", lambda: False)

    def test_tile_fetch_never_touches_response_content(self, make_gee, monkeypatch):
        """A regression to `response.content` fails this test."""

        class _NoBufferResponse:
            status_code = 200
            headers: dict[str, str] = {}

            def __init__(self, body: bytes):
                self._body = body

            @property
            def content(self) -> bytes:
                raise AssertionError(
                    "the body must be streamed, not buffered via .content"
                )

            def raise_for_status(self) -> None:
                """A 200 needs no action."""

            def iter_content(self, chunk_size=None):
                """Yield the body in small blocks, as a streamed response does."""
                for start in range(0, len(self._body), 8):
                    yield self._body[start : start + 8]

            def close(self) -> None:
                """No socket to release."""

        seen: list[dict] = []

        def fake_get(url, **kwargs):
            seen.append(kwargs)
            return _NoBufferResponse(_FAKE_TIFF_BYTES)

        monkeypatch.setattr(requests, "get", fake_get)
        make_gee().download(progress_bar=False)
        assert seen, "the tile URL should have been fetched"
        assert seen[0].get("stream") is True, f"the GET must stream: {seen[0]}"


class _FakeCrs:
    """Stand-in for a pyproj CRS: only `to_epsg()` is consulted."""

    def __init__(self, epsg):
        self._epsg = epsg

    def to_epsg(self):
        return self._epsg


class _FakePolygonAoi:
    """Stand-in for a `GeoDataFrame` AOI: `total_bounds`, `crs`, `to_crs`."""

    def __init__(self, epsg=None, total_bounds=(31.2, 29.9, 31.3, 30.0)):
        self.total_bounds = total_bounds
        self.crs = _FakeCrs(epsg) if epsg is not None else None
        self.reprojected_to = None
        self.assumed_crs = None
        # Every CRS `to_crs` was asked for, in order. The returned fake carries
        # fixed bounds, so this is the only record of *which* CRS was requested
        # - without it a test that only inspects the result cannot tell a
        # reprojection to the right CRS from one to the wrong CRS.
        self.reprojection_requests: list[str] = []

    def set_crs(self, crs):
        out = _FakePolygonAoi(epsg=4326, total_bounds=self.total_bounds)
        out.assumed_crs = crs
        return out

    def to_crs(self, crs):
        # A real reprojection moves the coordinates, so the result carries the
        # fake's default lat/lon bounds rather than the source's.
        self.reprojection_requests.append(crs)
        out = _FakePolygonAoi(epsg=4326)
        out.reprojected_to = crs
        out.assumed_crs = self.assumed_crs
        return out


class _FakeCogWriter:
    """Stand-in for `Dataset.cog`, recording `to_cog` writes."""

    def __init__(self, dataset):
        self._dataset = dataset

    def to_cog(self, path, **kwargs):
        self._dataset.written = str(path)
        self._dataset.wrote_cog = True
        Path(path).write_bytes(b"eedai-cog")
        return Path(path)


class _FakeEedaiDataset:
    """Stand-in for the pyramids `Dataset` the EEDAI reader returns."""

    def __init__(self):
        self.written: str | None = None
        self.wrote_cog = False
        self.cog = _FakeCogWriter(self)

    def to_file(self, path):
        self.written = str(path)
        Path(path).write_bytes(b"eedai-tif")


class _FakeWindow:
    """Stand-in for `pyramids_eo.Window`; carries the spatial read spec.

    The four fields the backend must supply (`bbox`, `crs`, `shape`,
    `resample`) are required, so dropping one from the `Window(...)`
    construction fails loudly here instead of silently inheriting a default
    that would let the assertion pass anyway. `scale` keeps its `None` default
    because the backend resolves scale into `shape` and never passes it.
    """

    def __init__(self, *, bbox, crs, shape, resample, scale=None):
        self.bbox = bbox
        self.crs = crs
        self.scale = scale
        self.shape = shape
        self.resample = resample


def _reader_error(message: str) -> Exception:
    """Return upstream's own `ReaderError` so the fake fails the way the real one does.

    A stand-in `RuntimeError` would not be in the set the backend catches, so
    the test would pass for the wrong reason.

    Args:
        message: The failure text.

    Returns:
        A `pyramids_eo` `ReaderError` when installed, else `OSError`.
    """
    try:
        from pyramids_eo.errors import ReaderError
    except ImportError:  # pragma: no cover - the extra is installed in CI
        return OSError(message)
    return ReaderError(message)


def _bind_to_real_signature(name: str, asset_id: str, kwargs: dict) -> None:
    """Reject keywords the installed pyramids-eo would not accept.

    A `**kwargs` fake takes any keyword, so a typo here or a renamed parameter
    upstream would pass the whole unit suite and fail only against the live
    service. Binding to the real signature — when the optional extra is
    installed — makes that drift a test failure instead.

    Args:
        name: The reader function being stood in for.
        asset_id: The positional asset id the call passed.
        kwargs: The keyword arguments the call passed.

    Raises:
        AssertionError: The real function would reject this call.
    """
    try:
        from pyramids_eo import earthengine as _real
    except ImportError:  # pragma: no cover - the extra is installed in CI
        return
    real = getattr(_real, name, None)
    if real is None:
        return
    try:
        inspect.signature(real).bind(asset_id, **kwargs)
    except TypeError as exc:
        raise AssertionError(
            f"{name}() would reject this call against the installed pyramids-eo: {exc}"
        ) from exc


class _FakeReaderModule:
    """Stand-in for `pyramids_eo.earthengine`; records `from_earthengine`."""

    Window = _FakeWindow

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.cost_calls: list[tuple[str, dict]] = []
        self.dataset = _FakeEedaiDataset()
        # A small, servable collection by default; tests override per case.
        self.cost = SimpleNamespace(scene_count=3, min_pixel_size=5566.0)
        self.cost_error: Exception | None = None
        self.read_error: Exception | None = None

    def estimate_earthengine_cost(self, asset_id, **kwargs):
        _bind_to_real_signature("estimate_earthengine_cost", asset_id, kwargs)
        self.cost_calls.append((asset_id, kwargs))
        if self.cost_error is not None:
            raise self.cost_error
        return self.cost

    def from_earthengine(self, asset_id, **kwargs):
        _bind_to_real_signature("from_earthengine", asset_id, kwargs)
        self.calls.append((asset_id, kwargs))
        if self.read_error is not None:
            raise self.read_error
        # Mirror the combinations upstream's `_validate_read_request` rejects,
        # so a plan that produces one fails here instead of passing silently.
        if kwargs.get("tile_size") is not None:
            window = kwargs.get("window")
            if getattr(window, "resample", "nearest") != "nearest":
                raise ValueError("'tile_size' supports only resample='nearest'")
            if kwargs.get("geometry") is not None:
                raise ValueError("'tile_size' cannot be combined with a 'geometry'")
            if kwargs.get("path") is None:
                raise ValueError("'tile_size' needs 'path'")
        path = kwargs.get("path")
        if path is not None:
            # A tiled read streams the mosaic to `path` itself.
            Path(path).write_bytes(b"eedai-tiled")
            self.dataset.written = str(path)
        return self.dataset


@pytest.fixture
def fake_reader(monkeypatch):
    """Patch the guarded pyramids-eo loader with an in-memory fake reader."""
    reader = _FakeReaderModule()
    monkeypatch.setattr(backend_module, "import_earthengine_reader", lambda: reader)
    reader.credential_builds = []
    monkeypatch.setattr(
        backend_module,
        "credentials_for",
        lambda key: reader.credential_builds.append(key) or ("creds", key),
    )
    monkeypatch.setattr(backend_module, "eedai_available", lambda: True)
    return reader


class TestEngineOption:
    """Tests for the `engine` constructor option."""

    def test_defaults_to_auto(self, make_gee):
        """`engine` defaults to `"auto"`."""
        assert make_gee().engine == "auto"

    @pytest.mark.parametrize("engine", ["auto", "ee", "eedai"])
    def test_accepts_known_engines(self, make_gee, engine):
        """Each supported engine name is captured verbatim."""
        assert make_gee(engine=engine).engine == engine

    def test_unknown_engine_rejected(self, make_gee):
        """An unknown engine raises `ValueError` at construction."""
        with pytest.raises(ValueError, match="engine must be one of"):
            make_gee(engine="gdal")


class TestPropertyFilter:
    """C5: the reader-only property_filter string, its validation and warnings."""

    def test_non_string_property_filter_is_rejected(self, make_gee):
        """A non-string property_filter fails fast at construction."""
        with pytest.raises(ValueError, match="OGR attribute-filter string"):
            make_gee(property_filter=20)

    @pytest.mark.parametrize(
        "bad, match",
        [
            ("   ", "must not be blank"),
            ("NAME = 'abc", "unterminated quote"),
            ('NAME = "abc', "unterminated quote"),
            ("(CLOUD < 20", "unclosed"),
            ("CLOUD < 20; DROP", "single expression"),
            ("CLOUD < 20 -- rest", "single expression"),
            # Balanced totals, reversed order: this escapes the wrapper upstream
            # puts around the fragment and neutralises the time/space clauses.
            ("1=1) OR (1=1", "never opened"),
            ("A > 0) OR (system:index LIKE '%'", "never opened"),
            (")(", "never opened"),
        ],
    )
    def test_malformed_property_filters_are_rejected(self, make_gee, bad, match):
        """A malformed filter fails at construction, not as an opaque 'no scenes'.

        It is interpolated verbatim into the reader's OGR filter, so a stray
        quote would otherwise surface as a GDAL error the routing gate reports
        as a discovery failure.
        """
        with pytest.raises(ValueError, match=match):
            make_gee(property_filter=bad)

    @pytest.mark.parametrize(
        "good",
        [
            "(CLOUDY_PIXEL_PERCENTAGE < 20) AND MGRS_TILE = '36RUU'",
            "CLOUDY_PIXEL_PERCENTAGE < 20",
            "((A > 1) AND (B < 2)) OR C = 3",
            # A separator or comment marker inside a quoted literal is data,
            # not a second statement, so it must not be rejected.
            "PRODUCT_ID = 'a;b'",
            "PRODUCT_ID = 'a--b'",
            "NAME = 'O''Brien'",
        ],
    )
    def test_well_formed_filters_are_accepted(self, make_gee, good):
        """Legitimate expressions, including quoted separators, pass."""
        assert make_gee(property_filter=good).property_filter == good

    def test_property_filter_warns_when_earth_engine_serves_the_request(
        self, make_gee, fake_reader, monkeypatch
    ):
        """Driven through `_api`: the notice fires on the path that drops the filter."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee(engine="ee", property_filter="CLOUDY_PIXEL_PERCENTAGE < 20")
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        for _ in range(2):
            gee._api(
                _FakeImage(),
                var_info,
                ["elevation"],
                dt.datetime(2000, 2, 11),
                dt.datetime(2000, 1, 1),
                dt.datetime(2000, 1, 2),
            )
        assert len([w for w in warnings if "property_filter has no effect" in w]) == 1

    def test_declined_collection_warns_that_the_filter_was_dropped(
        self, make_gee, fake_reader, monkeypatch
    ):
        """The case the notice exists for: an eligible collection that declined.

        The filter is silently lost when a bucket falls back, so the composite is
        built from every scene - cloudy ones included - and a multi-bucket run
        can mix filtered and unfiltered buckets.
        """
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee(
            start="2020-06-01",
            end="2020-06-30",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            property_filter="CLOUDY_PIXEL_PERCENTAGE < 20",
        )
        fake_reader.cost = SimpleNamespace(scene_count=5000, min_pixel_size=5566.0)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._api(
            _FakeImage(),
            var_info,
            ["precipitation"],
            dt.datetime(2020, 6, 1),
            dt.datetime(2020, 6, 1),
            dt.datetime(2020, 7, 1),
        )
        assert any("property_filter has no effect" in w for w in warnings), (
            "a declined collection dropped the user's scene filter silently"
        )

    def test_no_warning_when_the_reader_actually_applies_the_filter(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A served collection uses the filter, so nothing is warned."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee(
            start="2020-06-01",
            end="2020-06-30",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            property_filter="CLOUDY_PIXEL_PERCENTAGE < 20",
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._api(
            _FakeImage(),
            var_info,
            ["precipitation"],
            dt.datetime(2020, 6, 1),
            dt.datetime(2020, 6, 1),
            dt.datetime(2020, 7, 1),
        )
        assert [w for w in warnings if "property_filter has no effect" in w] == []


class TestEedaiCollections:
    """C1: an eligible ImageCollection is composited through the reader per bucket."""

    START = dt.datetime(2020, 6, 1)
    END = dt.datetime(2020, 7, 1)

    def _collection_gee(self, make_gee, **overrides):
        """A GEE over a small CHIRPS collection window."""
        params = dict(
            start="2020-06-01",
            end="2020-06-30",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
        )
        params.update(overrides)
        return make_gee(**params)

    def test_composite_kwargs_are_forwarded_to_the_reader(self, make_gee, fake_reader):
        """The reader is asked to composite the bucket's window with the reducer."""
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert plan.can_serve, plan.reason
        gee._export_via_eedai(
            var_info, ["precipitation"], 5566.0, "chirps", plan, self.START, self.END
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["start"] == "2020-06-01"
        # The reader's `end` is inclusive while this backend's bucket end is
        # exclusive, so the last covered day is what must be sent.
        assert kwargs["end"] == "2020-06-30"
        assert kwargs["reducer"] == var_info.default_reducer

    def test_single_image_read_sends_no_composite_kwargs(self, make_gee, fake_reader):
        """A static image is read directly, with no start/end/reducer."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert "start" not in kwargs
        assert "reducer" not in kwargs

    def test_over_the_scene_cap_declines(self, make_gee, fake_reader):
        """More scenes than the cap fall back to Earth Engine's server-side reduce."""
        gee = self._collection_gee(make_gee)
        fake_reader.cost = SimpleNamespace(scene_count=5000, min_pixel_size=5566.0)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "scene cap" in plan.reason

    def test_budget_uses_the_output_grid_not_the_native_one(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A finer `scale` than the asset must raise the estimate, not leave it flat.

        Upstream warps every scene onto the output window before stacking them,
        so sizing the budget from the native grid under-counts exactly when the
        request asks for more pixels than the asset has.
        """
        coarse = self._collection_gee(make_gee, scale=5566.0)
        fine = self._collection_gee(make_gee, scale=100.0)
        var_info = coarse.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        # Chosen so one scene's window fits comfortably but the three-scene
        # stack does not: this must exercise the collection budget, not the
        # single-scene gate that runs before it.
        monkeypatch.setattr(backend_module, "_EEDAI_MAX_PIXELS", 20_000)
        coarse_plan = coarse._eedai_collection_fits(var_info, 1, self.START, self.END)
        fine_plan = fine._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert coarse_plan.can_serve, coarse_plan.reason
        assert not fine_plan.can_serve, (
            "a much finer scale did not raise the estimated footprint"
        )

    def test_over_the_pixel_budget_declines(self, make_gee, fake_reader, monkeypatch):
        """Scenes that together exceed the single-pass budget are declined."""
        gee = self._collection_gee(make_gee)
        fake_reader.cost = SimpleNamespace(scene_count=50, min_pixel_size=5566.0)
        # Shrink the budget so the modest AOI footprint x 50 scenes overruns it.
        monkeypatch.setattr(backend_module, "_EEDAI_MAX_PIXELS", 10)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "single-pass budget" in plan.reason

    def test_scene_discovery_failure_declines(self, make_gee, fake_reader):
        """A transport-level discovery error declines rather than crashing."""
        gee = self._collection_gee(make_gee)
        fake_reader.cost_error = OSError("EEDA is unreachable")
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "discovery for" in plan.reason
        assert "failed" in plan.reason

    def test_no_scenes_is_reported_separately_from_a_failure(
        self, make_gee, fake_reader
    ):
        """An empty window is a quiet decline, not a reported failure."""
        gee = self._collection_gee(make_gee)
        fake_reader.cost = SimpleNamespace(scene_count=0, min_pixel_size=5566.0)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "no UCSB-CHG/CHIRPS/DAILY scenes" in plan.reason
        assert "failed" not in plan.reason

    def test_credential_failure_warns_and_falls_back_under_auto(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A bad key must not be reported as "no scenes", nor fail an auto run."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = self._collection_gee(make_gee)
        monkeypatch.setattr(
            type(gee),
            "_eedai_credentials",
            lambda self: (_ for _ in ()).throw(
                backend_module.AuthenticationError("bad key")
            ),
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "credential" in plan.reason
        assert any("credential could not be built" in w for w in warnings)

    def test_credential_failure_raises_under_a_forced_engine(
        self, make_gee, fake_reader, monkeypatch
    ):
        """`engine="eedai"` asked for the reader, so a bad key is an error."""
        gee = self._collection_gee(make_gee, engine="eedai")
        monkeypatch.setattr(
            type(gee),
            "_eedai_credentials",
            lambda self: (_ for _ in ()).throw(
                backend_module.AuthenticationError("bad key")
            ),
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        with pytest.raises(backend_module.AuthenticationError):
            gee._eedai_collection_fits(var_info, 1, self.START, self.END)

    def test_collection_without_native_resolution_declines(self, make_gee, fake_reader):
        """A collection whose catalog row has no resolution cannot be sized."""
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY").model_copy(
            update={"spatial_resolution": None}
        )
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "no native resolution" in plan.reason

    def test_missing_bucket_window_raises(self, make_gee, fake_reader):
        """A missing window is a caller bug, not a reason to fall back silently.

        Declining would turn a programming error into a permanent, invisible
        downgrade to Earth Engine for the whole run.
        """
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        with pytest.raises(ValueError, match="needs a bucket date window"):
            gee._eedai_collection_fits(var_info, 1, None, None)

    def test_a_monthly_download_runs_end_to_end_through_the_reader(
        self, make_gee, fake_reader
    ):
        """The whole `download()` path, not just the pieces, must route per bucket.

        Every other case here calls the routing helpers directly, which is how a
        per-bucket cost that only *looked* amortised survived review: the count
        of discovery queries and the bucket windows are only observable from a
        real multi-bucket run.
        """
        gee = self._collection_gee(
            make_gee,
            start="2020-06-01",
            end="2020-08-31",
            temporal_resolution="monthly",
        )
        written = gee.download(progress_bar=False)
        assert len(written) == 3, f"one file per month expected, got {written}"
        assert all(Path(p).is_file() for p in written), written
        assert len(fake_reader.calls) == 3, (
            "each month must be composited by the reader, not by Earth Engine"
        )
        assert len(fake_reader.cost_calls) == 3, (
            "scene discovery costs one query per bucket, no more and no fewer: "
            f"{len(fake_reader.cost_calls)}"
        )
        windows = [(k["start"], k["end"]) for _asset, k in fake_reader.calls]
        assert windows == [
            ("2020-06-01", "2020-06-30"),
            ("2020-07-01", "2020-07-31"),
            ("2020-08-01", "2020-08-31"),
        ], windows

    def test_a_monthly_download_falls_back_whole_when_the_reader_declines(
        self, make_gee, fake_reader
    ):
        """A declined collection must still write every bucket, via Earth Engine."""
        gee = self._collection_gee(
            make_gee,
            start="2020-06-01",
            end="2020-08-31",
            temporal_resolution="monthly",
            reducer="mosaic",
        )
        written = gee.download(progress_bar=False)
        assert len(written) == 3, f"the fallback dropped buckets: {written}"
        assert fake_reader.calls == [], "the declined reducer still reached the reader"

    def test_the_region_is_reprojected_once_across_buckets(self, make_gee, fake_reader):
        """A many-bucket run must not warp the same region once per bucket.

        The region never changes for the life of a backend, so a daily run over
        a year would otherwise pay a thousand reprojections of the same
        `GeoDataFrame` to reach the same lat/lon envelope.
        """
        region = _FakePolygonAoi(
            epsg=32636, total_bounds=(330000.0, 3310000.0, 340000.0, 3320000.0)
        )
        gee = self._collection_gee(make_gee, region=region)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        july = dt.datetime(2020, 7, 1)
        august = dt.datetime(2020, 8, 1)
        gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        gee._eedai_collection_fits(var_info, 1, july, august)
        assert region.reprojection_requests == ["EPSG:4326"], (
            "the second bucket reprojected the region again: "
            f"{region.reprojection_requests}"
        )
        assert [k["bbox"] for _asset, k in fake_reader.cost_calls] == [
            (31.2, 29.9, 31.3, 30.0),
            (31.2, 29.9, 31.3, 30.0),
        ], "the cached reprojection changed the AOI the buckets discovered over"

    def test_estimate_is_queried_with_the_bucket_window(self, make_gee, fake_reader):
        """Scene discovery uses the bucket's dates and a lat/lon AOI envelope."""
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        _asset_id, kwargs = fake_reader.cost_calls[0]
        assert kwargs["start"] == "2020-06-01"
        assert kwargs["end"] == "2020-06-30"
        assert len(kwargs["bbox"]) == 4

    def test_mosaic_reducer_is_declined_before_any_network_call(
        self, make_gee, fake_reader
    ):
        """`mosaic` means last-wins in Earth Engine but first-scene in the reader.

        It is also the most common `default_reducer` in the catalog, so serving
        it would quietly turn many composites into "the earliest scene".
        """
        gee = self._collection_gee(make_gee, reducer="mosaic")
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "last-wins" in plan.reason
        assert fake_reader.cost_calls == [], (
            "the unsupported reducer should be caught before scene discovery"
        )

    def test_a_dataset_defaulting_to_mosaic_is_declined(self, make_gee, fake_reader):
        """The decline follows the catalog's own default, not just an override."""
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY").model_copy(
            update={"default_reducer": "mosaic"}
        )
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "last-wins" in plan.reason

    def test_a_supported_reducer_still_serves(self, make_gee, fake_reader):
        """A statistical reducer is unaffected by the mosaic decline."""
        gee = self._collection_gee(make_gee, reducer="median")
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        assert gee._eedai_collection_fits(var_info, 1, self.START, self.END).can_serve

    def test_discovery_uses_the_regions_bounds_not_the_bbox(
        self, make_gee, fake_reader
    ):
        """With a `region`, scenes must be discovered over the ground actually read.

        The region supersedes the lat/lon bbox for the clip, so discovering over
        the bbox would count scenes for one geometry while the pixel footprint
        came from another.
        """
        region = _FakePolygonAoi(epsg=4326, total_bounds=(10.0, 5.0, 10.5, 5.5))
        gee = self._collection_gee(make_gee, region=region)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert fake_reader.cost_calls[0][1]["bbox"] == (10.0, 5.0, 10.5, 5.5)

    def test_discovery_brings_a_projected_region_back_to_latlon(
        self, make_gee, fake_reader
    ):
        """A region in another CRS is reprojected before it bounds discovery."""
        region = _FakePolygonAoi(
            epsg=32636, total_bounds=(330000.0, 3310000.0, 340000.0, 3320000.0)
        )
        gee = self._collection_gee(make_gee, region=region)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert region.reprojection_requests == ["EPSG:4326"], (
            "discovery must reproject the region to lat/lon, and only there: "
            f"{region.reprojection_requests}"
        )
        bbox = fake_reader.cost_calls[0][1]["bbox"]
        assert bbox == (31.2, 29.9, 31.3, 30.0), (
            f"discovery did not use the reprojected region's bounds: {bbox}"
        )

    def test_a_late_reader_refusal_falls_back_under_auto(
        self, make_gee, fake_reader, monkeypatch
    ):
        """The reader can refuse after routing commits; `auto` must not crash.

        A band set spanning resolution groups is refused by the collection
        reader although the single-image one handles it, and only upstream
        knows that — so the failure arrives after the credential build and the
        scene discovery, on a request Earth Engine could serve.
        """
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = self._collection_gee(make_gee)
        fake_reader.read_error = _reader_error("bands span multiple resolution groups")
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        out = gee._api(
            _FakeImage(),
            var_info,
            ["precipitation"],
            dt.datetime(2020, 6, 1),
            dt.datetime(2020, 6, 1),
            dt.datetime(2020, 7, 1),
        )
        assert out is not None, "the bucket produced no output at all"
        assert any("could not serve" in w for w in warnings)

    def test_a_late_reader_refusal_raises_under_a_forced_engine(
        self, make_gee, fake_reader
    ):
        """`engine="eedai"` asked for the reader, so its refusal is the answer."""
        gee = self._collection_gee(make_gee, engine="eedai")
        fake_reader.read_error = _reader_error("bands span multiple resolution groups")
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        image = _FakeImage()
        when = dt.datetime(2020, 6, 1)
        bucket_end = dt.datetime(2020, 7, 1)
        with pytest.raises(Exception, match="resolution groups"):
            gee._api(image, var_info, ["precipitation"], when, when, bucket_end)

    def test_an_empty_bucket_does_not_abort_a_forced_run(self, make_gee, fake_reader):
        """No scenes is a fact about the data, so a forced engine skips the bucket.

        Raising would kill a long run at the first month with no imagery, after
        every earlier bucket had already been written.
        """
        gee = self._collection_gee(make_gee, engine="eedai")
        fake_reader.cost = SimpleNamespace(scene_count=0, min_pixel_size=5566.0)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        use_reader, plan = gee._use_eedai(var_info, 1, self.START, self.END)
        assert use_reader is False
        assert plan is None

    def test_a_forced_run_still_raises_on_an_ineligible_request(
        self, make_gee, fake_reader
    ):
        """Skipping empty buckets must not soften the forced-engine contract."""
        gee = self._collection_gee(make_gee, engine="eedai", reducer="mosaic")
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        start, end = self.START, self.END
        with pytest.raises(ValueError, match="cannot serve"):
            gee._use_eedai(var_info, 1, start, end)

    def test_a_sub_day_bucket_never_inverts_its_window(self, make_gee, fake_reader):
        """A bucket shorter than a day must not ask for an end before its start."""
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(
            var_info,
            1,
            dt.datetime(2020, 6, 3, 0, 0),
            dt.datetime(2020, 6, 3, 12, 0),
        )
        kwargs = fake_reader.cost_calls[0][1]
        assert kwargs["start"] <= kwargs["end"], (
            f"inverted window: {kwargs['start']}..{kwargs['end']}"
        )

    def test_a_sizing_refusal_falls_back_under_auto(
        self, make_gee, fake_reader, monkeypatch
    ):
        """Sizing that raises must route to Earth Engine, not abort the download."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = self._collection_gee(make_gee)
        monkeypatch.setattr(
            type(gee),
            "_eedai_verdict",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("bounds are not finite")),
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        use_reader, plan = gee._use_eedai(var_info, 1, self.START, self.END)
        assert use_reader is False
        assert plan is None
        assert any("could not size" in w for w in warnings)

    def test_each_bucket_costs_exactly_one_discovery_query(self, make_gee, fake_reader):
        """Discovery is one catalog query per bucket - no more, and no caching.

        Each bucket has a distinct window and is visited once, so there is
        nothing to reuse; this pins the cost so a future change that adds a
        second query per bucket is visible.
        """
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        gee._eedai_collection_fits(
            var_info, 1, dt.datetime(2020, 7, 1), dt.datetime(2020, 8, 1)
        )
        assert len(fake_reader.cost_calls) == 2

    def test_collection_obeys_the_per_axis_budget(self, make_gee, fake_reader):
        """A window too tall for one pass is declined, as it is for a single image.

        The per-axis cap is about the window's *shape*, so no scene-count
        multiple of the area would catch it.
        """
        gee = self._collection_gee(
            make_gee, lat_lim=[0.0, 40.0], lon_lim=[31.2, 31.3], scale=100.0
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY").model_copy(
            update={"spatial_resolution": 5.0}
        )
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert not plan.can_serve
        assert "per-axis budget" in plan.reason

    def test_a_served_collection_reports_its_scene_count(self, make_gee, fake_reader):
        """The plan carries the real scene count, not a placeholder."""
        gee = self._collection_gee(make_gee)
        fake_reader.cost = SimpleNamespace(scene_count=7, min_pixel_size=5566.0)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        assert plan.can_serve
        assert plan.tiles == 7

    def test_discovery_bbox_is_labelled_latlon(self, make_gee, fake_reader):
        """Scene discovery must declare EPSG:4326, since the AOI it sends is lat/lon."""
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        kwargs = fake_reader.cost_calls[0][1]
        assert kwargs["crs"] == "EPSG:4326", (
            f"discovery CRS {kwargs['crs']!r} does not match the lat/lon bbox sent"
        )

    def test_projected_crs_collection_still_discovers_in_latlon(
        self, make_gee, fake_reader
    ):
        """A projected output CRS must not relabel the lat/lon discovery AOI.

        The two features are easy to test apart and wrong together: labelling
        degrees as UTM metres discovers scenes over the wrong ground.
        """
        gee = self._collection_gee(make_gee, crs="EPSG:32636")
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        kwargs = fake_reader.cost_calls[0][1]
        assert kwargs["crs"] == "EPSG:4326"
        assert kwargs["bbox"] == (31.2, 29.9, 31.3, 30.0), (
            f"discovery bbox {kwargs['bbox']} is not the request's lat/lon box"
        )
        assert plan.can_serve, plan.reason

    def test_consecutive_buckets_do_not_overlap(self, make_gee, fake_reader):
        """Adjacent buckets must not both claim the boundary day.

        The reader's `end` is inclusive and this backend's bucket end is
        exclusive, so sending the raw boundary would make each bucket read one
        extra day and overlap the next — a daily bucket would be a two-day
        reduce.
        """
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        june = dt.datetime(2020, 6, 1)
        july = dt.datetime(2020, 7, 1)
        august = dt.datetime(2020, 8, 1)
        first = gee._eedai_collection_fits(var_info, 1, june, july)
        second = gee._eedai_collection_fits(var_info, 1, july, august)
        assert first.can_serve
        assert second.can_serve
        first_end = fake_reader.cost_calls[0][1]["end"]
        second_start = fake_reader.cost_calls[1][1]["start"]
        assert first_end < second_start, (
            f"bucket windows overlap: first ends {first_end}, next starts "
            f"{second_start}"
        )

    def test_a_single_day_bucket_reads_exactly_that_day(self, make_gee, fake_reader):
        """A one-day bucket collapses to start == end, not a two-day window."""
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(
            var_info, 1, dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 2)
        )
        kwargs = fake_reader.cost_calls[0][1]
        assert (kwargs["start"], kwargs["end"]) == ("2020-06-01", "2020-06-01")

    def test_property_filter_reaches_estimate_and_composite(
        self, make_gee, fake_reader
    ):
        """A property_filter narrows the scene estimate and the composite read."""
        gee = self._collection_gee(
            make_gee, property_filter="CLOUDY_PIXEL_PERCENTAGE < 20"
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        gee._export_via_eedai(
            var_info, ["precipitation"], 5566.0, "chirps", plan, self.START, self.END
        )
        _cid, cost_kwargs = fake_reader.cost_calls[0]
        assert cost_kwargs["property_filter"] == "CLOUDY_PIXEL_PERCENTAGE < 20"
        _rid, read_kwargs = fake_reader.calls[0]
        assert read_kwargs["property_filter"] == "CLOUDY_PIXEL_PERCENTAGE < 20"

    def test_no_property_filter_sends_none_to_the_reader(self, make_gee, fake_reader):
        """Without a property_filter the composite read carries no such kwarg."""
        gee = self._collection_gee(make_gee)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(var_info, 1, self.START, self.END)
        gee._export_via_eedai(
            var_info, ["precipitation"], 5566.0, "chirps", plan, self.START, self.END
        )
        _rid, read_kwargs = fake_reader.calls[0]
        assert "property_filter" not in read_kwargs


class TestEedaiProjectedCrs:
    """C2: the fast-path reads a metre-based projected CRS, not just EPSG:4326."""

    def test_output_grid_delegates_to_the_degree_grid_under_4326(self, make_gee):
        """Under EPSG:4326 the output grid is the geographic grid, unchanged."""
        gee = make_gee()
        bbox = (31.2, 29.9, 31.3, 30.0)
        assert gee._eedai_output_grid(bbox, 90.0) == gee._eedai_grid(bbox, 90.0)

    def test_output_grid_sizes_by_metres_under_a_projected_crs(self, make_gee):
        """A projected CRS sizes each axis by its metre span over the scale."""
        gee = make_gee(crs="EPSG:32636")
        rows, cols = gee._eedai_output_grid(
            (300000.0, 3300000.0, 301000.0, 3301000.0), 100.0
        )
        assert (rows, cols) == (10, 10)

    def test_output_grid_rejects_a_non_positive_scale_when_projected(self, make_gee):
        """A projected read still needs a positive metre scale to size a grid."""
        gee = make_gee(crs="EPSG:32636")
        with pytest.raises(ValueError, match="positive number of metres"):
            gee._eedai_output_grid((300000.0, 3300000.0, 301000.0, 3301000.0), 0.0)

    def test_window_reprojects_the_aoi_into_the_projected_crs(self, make_gee):
        """The lat/lon AOI comes back as projected metres, not degrees."""
        gee = make_gee(crs="EPSG:32636")
        (min_x, min_y, max_x, max_y), cutline = gee._eedai_window()
        assert cutline is None
        assert min_x > 100_000, min_x
        assert min_y > 1_000_000, min_y
        assert max_x > min_x, (min_x, max_x)
        assert max_y > min_y, (min_y, max_y)

    def test_an_aoi_outside_the_projection_is_refused_by_name(self, make_gee):
        """An AOI the target projection cannot represent must say so.

        An orthographic CRS is metre-based and projected, so it passes the
        eligibility check, but `transform_bounds` answers `inf` for ground on
        the far side of the globe. Without the finiteness guard the budget
        arithmetic raises an opaque `OverflowError` out of `math.ceil` instead
        of naming the AOI.
        """
        gee = make_gee(crs=_ORTHO_CRS, lat_lim=[-10.0, 10.0], lon_lim=[150.0, 160.0])
        bbox, _cutline = gee._eedai_window()
        assert not all(math.isfinite(bound) for bound in bbox), (
            f"the fixture no longer produces a non-finite envelope: {bbox}"
        )
        with pytest.raises(ValueError, match="must all be finite"):
            gee._eedai_output_grid(bbox, 90.0)

    def test_an_aoi_outside_the_projection_falls_back_under_auto(
        self, make_gee, fake_reader, monkeypatch
    ):
        """`auto` must route that request to Earth Engine, not abort the download.

        The user asked for a download, not for this engine; Earth Engine can
        still serve an AOI the reader cannot size.
        """
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee(crs=_ORTHO_CRS, lat_lim=[-10.0, 10.0], lon_lim=[150.0, 160.0])
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        use_reader, plan = gee._use_eedai(var_info, 1, None, None)
        assert use_reader is False
        assert plan is None
        assert any("could not size" in w for w in warnings), warnings

    def test_an_aoi_outside_the_projection_raises_under_a_forced_engine(
        self, make_gee, fake_reader
    ):
        """Forcing the reader keeps the sizing failure visible."""
        gee = make_gee(
            crs=_ORTHO_CRS,
            lat_lim=[-10.0, 10.0],
            lon_lim=[150.0, 160.0],
            engine="eedai",
        )
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        with pytest.raises(ValueError, match="must all be finite"):
            gee._use_eedai(var_info, 1, None, None)

    def test_window_passes_latlon_through_under_4326(self, make_gee):
        """Under EPSG:4326 the AOI is the lat/lon box, unreprojected."""
        gee = make_gee()
        bbox, _cutline = gee._eedai_window()
        assert bbox == (31.2, 29.9, 31.3, 30.0)

    def test_crsless_region_is_assumed_latlon_then_reprojected(self, make_gee):
        """A region with no CRS is taken as lat/lon, then reprojected to the target."""
        region = _FakePolygonAoi(epsg=None)
        gee = make_gee(crs="EPSG:32636", region=region)
        out = gee._region_in_output_crs(region)
        assert out.assumed_crs == "EPSG:4326", (
            "the CRS-less region was not assumed 4326"
        )
        assert out.reprojected_to == "EPSG:32636", (
            "it was not reprojected to the target"
        )

    def test_crsless_region_passes_through_under_4326(self, make_gee):
        """Under EPSG:4326 a CRS-less region needs no reprojection at all."""
        region = _FakePolygonAoi(epsg=None)
        gee = make_gee(region=region)
        assert gee._region_in_output_crs(region) is region

    def test_region_without_set_crs_is_reprojected_directly(self, make_gee):
        """An AOI object lacking `set_crs` still reaches `to_crs` rather than failing."""

        class _MinimalAoi:
            """An AOI exposing only what the reprojection strictly needs."""

            crs = None
            total_bounds = (31.2, 29.9, 31.3, 30.0)

            def __init__(self):
                self.reprojected_to = None

            def to_crs(self, crs):
                out = _MinimalAoi()
                out.reprojected_to = crs
                return out

        gee = make_gee(crs="EPSG:32636")
        assert gee._region_in_output_crs(_MinimalAoi()).reprojected_to == "EPSG:32636"

    def test_an_unparseable_target_never_takes_the_epsg_shortcut(self, make_gee):
        """A target PROJ cannot parse falls to the code check, which declines it.

        The fallback trusts `to_epsg()` only against an `EPSG:` target, so a
        region reporting 3857 does not slip through an `ESRI:3857` target on the
        shared number alone - the bbox and cutline would describe different
        ground.
        """
        region = _FakePolygonAoi(epsg=3857)
        gee = make_gee(region=region)
        gee.crs = "ESRI:3857"  # PROJ raises CRSError on this one; ESRI:102100 is real
        assert gee._region_in_output_crs(region) is not region

    def test_a_proj_string_region_matching_the_target_is_not_reprojected(
        self, make_gee
    ):
        """PROJ decides equality, so an equivalent PROJ string needs no warp.

        This is the branch the CRS-object comparison exists for: neither side
        can be answered by parsing an `AUTH:CODE` tail, and warping a region
        that is already in the output CRS costs a full reprojection for nothing.
        """
        from pyproj import CRS

        region = _FakePolygonAoi(epsg=4326)
        region.crs = CRS.from_user_input("+proj=utm +zone=36 +datum=WGS84")
        gee = make_gee(crs="EPSG:32636", region=region)
        assert gee._region_in_output_crs(region) is region
        assert region.reprojection_requests == []

    def test_a_real_crs_differing_from_the_target_is_reprojected(self, make_gee):
        """The same object comparison must still warp a genuinely different CRS."""
        from pyproj import CRS

        region = _FakePolygonAoi(epsg=4326)
        region.crs = CRS.from_user_input("EPSG:4326")
        gee = make_gee(crs="EPSG:32636", region=region)
        assert gee._region_in_output_crs(region).reprojected_to == "EPSG:32636"

    def test_a_non_epsg_authority_naming_the_same_crs_is_not_reprojected(
        self, make_gee
    ):
        """`ESRI:102100` and `EPSG:3857` are one CRS, so PROJ answers "no warp".

        Parsing the code would have called them different on the authority
        alone; the object comparison gets it right.
        """
        from pyproj import CRS

        region = _FakePolygonAoi(epsg=3857)
        region.crs = CRS.from_user_input("ESRI:102100")
        gee = make_gee(region=region)
        gee.crs = "EPSG:3857"
        assert gee._region_in_output_crs(region) is region

    def test_region_reprojects_when_the_target_has_no_epsg_code(self, make_gee):
        """A target CRS with no `AUTH:CODE` form cannot be EPSG-matched, so it warps."""
        region = _FakePolygonAoi(epsg=4326)
        gee = make_gee(region=region)
        gee.crs = "+proj=utm +zone=36 +datum=WGS84"
        assert gee._region_in_output_crs(region).reprojected_to == gee.crs

    def test_region_already_in_the_target_crs_is_not_reprojected(self, make_gee):
        """A region whose EPSG already matches the target is passed through."""
        region = _FakePolygonAoi(epsg=32636)
        gee = make_gee(crs="EPSG:32636", region=region)
        assert gee._region_in_output_crs(region) is region

    def test_region_in_another_crs_is_reprojected(self, make_gee):
        """A region in a different EPSG is reprojected to the output CRS."""
        region = _FakePolygonAoi(epsg=3857)
        gee = make_gee(crs="EPSG:32636", region=region)
        assert gee._region_in_output_crs(region).reprojected_to == "EPSG:32636"

    def test_region_and_window_agree_under_a_projected_crs(self, make_gee, fake_reader):
        """End to end: the cutline and the bbox must land in the same space.

        A region left in another CRS would window one patch of ground and clip
        another, which produces a valid-looking raster of the wrong place.
        """
        region = _FakePolygonAoi(epsg=4326, total_bounds=(31.2, 29.9, 31.3, 30.0))
        gee = make_gee(crs="EPSG:32636", region=region)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_utm_region", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        cutline = kwargs["geometry"]
        assert cutline is not region, "the lat/lon region reached the reader unchanged"
        assert cutline.reprojected_to == "EPSG:32636"
        assert kwargs["window"].bbox == tuple(cutline.total_bounds), (
            "the window and the cutline describe different ground"
        )
        assert kwargs["window"].crs == "EPSG:32636"

    def test_projected_read_hands_the_reader_a_projected_window(
        self, make_gee, fake_reader
    ):
        """The reader receives the projected CRS and a metric bbox, so it reads
        the right ground rather than lon/lat as metres."""
        gee = make_gee(engine="eedai", crs="EPSG:32636")
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_utm", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["window"].crs == "EPSG:32636"
        assert kwargs["window"].bbox[0] > 100_000, kwargs["window"].bbox


class TestEedaiEligibility:
    """Tests for `_eedai_eligible` / `_use_eedai`."""

    def test_static_image_without_hooks_is_eligible(self, make_gee):
        """A raw single-asset read with no server-side compute is eligible."""
        gee = make_gee()
        assert gee._eedai_eligible(gee.catalog.get_dataset("USGS/SRTMGL1_003"))

    def test_image_collection_is_eligible(self, make_gee):
        """A collection with no server-side shaping is composited by the reader (C1)."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]}, scale=5566.0
        )
        assert gee._eedai_eligible(gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY"))

    def test_collection_with_cloud_mask_stays_ineligible(self, make_gee):
        """A collection that still needs a server-side mask is not eligible."""
        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            cloud_mask=_identity_mask,
        )
        assert not gee._eedai_eligible(gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY"))

    @pytest.mark.parametrize(
        "hooks",
        [{"cloud_mask": _identity_mask}, {"filters": [lambda c: c]}],
    )
    def test_server_side_hooks_make_it_ineligible(self, make_gee, hooks):
        """A `cloud_mask` or `filters` keeps the request on Earth Engine."""
        gee = make_gee(**hooks)
        assert not gee._eedai_eligible(gee.catalog.get_dataset("USGS/SRTMGL1_003"))

    def test_projected_metric_crs_is_eligible(self, make_gee):
        """A metre-based projected `crs` is served by the reader (C2)."""
        gee = make_gee(crs="EPSG:32636")
        assert gee._eedai_eligible(gee.catalog.get_dataset("USGS/SRTMGL1_003"))

    def test_non_metre_geographic_crs_is_not_eligible(self, make_gee):
        """A geographic CRS other than EPSG:4326 is not sized by a metre scale."""
        gee = make_gee(crs="EPSG:4269")
        assert not gee._eedai_eligible(gee.catalog.get_dataset("USGS/SRTMGL1_003"))

    def test_a_broken_pyproj_is_not_reported_as_an_unsupported_crs(
        self, make_gee, monkeypatch
    ):
        """An import failure is a broken environment, not a CRS we cannot serve.

        Swallowing it would silently route every projected request to Earth
        Engine with nothing to point at.
        """
        import builtins

        real_import = builtins.__import__

        def _explode(name, *args, **kwargs):
            if name == "pyproj":
                raise ImportError("pyproj is not installed")
            return real_import(name, *args, **kwargs)

        gee = make_gee(crs="EPSG:32636")
        monkeypatch.setattr(builtins, "__import__", _explode)
        with pytest.raises(ImportError):
            gee._eedai_crs_supported()

    def test_unparseable_crs_is_not_eligible(self, make_gee):
        """A CRS pyproj cannot parse is declined rather than raising."""
        gee = make_gee(crs="NOT-A-CRS")
        assert not gee._eedai_eligible(gee.catalog.get_dataset("USGS/SRTMGL1_003"))

    def test_engine_eedai_names_the_crs_limit(self, make_gee, fake_reader):
        """Forcing the reader with an unsupported `crs` explains the CRS limit."""
        gee = make_gee(engine="eedai", crs="EPSG:4269")
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        with pytest.raises(ValueError, match="metre-based projected CRS"):
            gee._use_eedai(var_info, 1)

    def test_batch_sink_is_not_eligible(self, make_gee):
        """The asynchronous sinks are Earth Engine-only."""
        gee = make_gee(export_via="drive", drive_folder="out")
        assert not gee._eedai_eligible(gee.catalog.get_dataset("USGS/SRTMGL1_003"))

    def test_engine_ee_never_uses_eedai(self, make_gee, fake_reader):
        """`engine="ee"` stays on `getDownloadURL` even when eligible."""
        gee = make_gee(engine="ee")
        assert (
            gee._use_eedai(gee.catalog.get_dataset("USGS/SRTMGL1_003"), 1)[0] is False
        )

    def test_engine_auto_uses_eedai_when_available(self, make_gee, fake_reader):
        """`engine="auto"` takes the fast-path when eligible and installed."""
        gee = make_gee()
        assert gee._use_eedai(gee.catalog.get_dataset("USGS/SRTMGL1_003"), 1)[0] is True

    def test_engine_auto_falls_back_when_not_installed(self, make_gee, monkeypatch):
        """Without the extra, `engine="auto"` falls back to Earth Engine."""
        monkeypatch.setattr(backend_module, "eedai_available", lambda: False)
        gee = make_gee()
        assert (
            gee._use_eedai(gee.catalog.get_dataset("USGS/SRTMGL1_003"), 1)[0] is False
        )

    def test_engine_eedai_forces_the_reader_when_eligible(self, make_gee, fake_reader):
        """`engine="eedai"` takes the reader for an eligible request."""
        gee = make_gee(engine="eedai")
        assert gee._use_eedai(gee.catalog.get_dataset("USGS/SRTMGL1_003"), 1)[0] is True

    def test_engine_eedai_rejects_ineligible_request(self, make_gee, fake_reader):
        """Forcing the reader on a server-side-shaped request raises `ValueError`.

        A collection is no longer the example here: the reader composites those
        now. What stays ineligible is work only Earth Engine can do — a
        per-image `cloud_mask` runs inside the graph the reader cannot execute.
        """
        gee = make_gee(
            engine="eedai",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            cloud_mask=_identity_mask,
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)
        with pytest.raises(ValueError, match="engine='eedai' cannot serve"):
            gee._use_eedai(var_info, 1, start, end)


class TestForcedEngineRemedies:
    """M2: a forced-engine error must suggest the dial that actually applies."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("the reducer 'mosaic' is last-wins upstream", "statistical reducer"),
            ("the EEDAI credential could not be built", "service key"),
            ("X has no native resolution to size the read", "spatial_resolution"),
            (
                "about 9,000,000,000 px across 40 scenes on a 3x3 grid, over the "
                "200,000,000-px single-pass budget",
                "coarser scale",
            ),
            (
                "900 scenes in this bucket, over the 500-scene cap - Earth Engine "
                "reduces this server-side",
                "property_filter",
            ),
            ("too large, and it cannot be tiled behind a cutline", "cutline"),
            (
                "too large, and it cannot be tiled with resample='bilinear'",
                "resample='nearest'",
            ),
            ("32,769 px on the longest axis", "smaller bbox"),
        ],
    )
    def test_each_reason_gets_its_own_remedy(self, reason, expected):
        """Every decline reason names a change that would fix that reason.

        A single fixed suffix used to tell a user with an unsupported reducer to
        shrink their bbox, which cannot help — the remedies only earn their
        place by differing.
        """
        assert expected in backend_module._eedai_remedy(reason)

    def test_a_stack_over_budget_is_not_read_as_a_scene_cap(self):
        """The pixel-budget reason names scenes too, so the order must hold."""
        remedy = backend_module._eedai_remedy(
            "about 9,000,000,000 px across 40 scenes on a 3x3 grid, over the "
            "200,000,000-px single-pass budget"
        )
        assert "coarser scale" in remedy
        assert "property_filter" not in remedy

    def test_the_forced_error_carries_the_matching_remedy(self, make_gee, fake_reader):
        """The reason and its remedy arrive together in the raised message."""
        gee = make_gee(
            engine="eedai",
            start="2020-06-01",
            end="2020-06-30",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
            reducer="mosaic",
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)
        with pytest.raises(ValueError, match="statistical reducer"):
            gee._use_eedai(var_info, 1, start, end)


class TestExportViaEedai:
    """Tests for `_export_via_eedai` and the `_api` routing."""

    def test_a_collection_export_without_a_bucket_window_raises(
        self, make_gee, fake_reader
    ):
        """A collection needs its window; reading without one is a caller bug.

        `_use_eedai` always supplies it, so reaching the exporter without one
        means the composite would silently read the reader's default window
        instead of the bucket's — the wrong dates, written under the bucket's
        name.
        """
        gee = make_gee(
            start="2020-06-01",
            end="2020-06-30",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            scale=5566.0,
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        plan = gee._eedai_collection_fits(
            var_info, 1, dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)
        )
        assert plan.can_serve, plan.reason
        with pytest.raises(ValueError, match="needs a bucket window"):
            gee._export_via_eedai(
                var_info, ["precipitation"], 5566.0, "chirps_no_window", plan
            )
        assert fake_reader.calls == [], "the guard let the read happen anyway"

    def test_writes_the_tif_through_the_reader(self, make_gee, fake_reader):
        """The reader's dataset is written to `<prefix>.tif`."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        target = gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        assert target.name == "srtm_elev.tif"
        assert target.exists()
        assert target.read_bytes() == b"eedai-tif"
        assert not list(target.parent.glob("*.partial.tif")), "staged file left behind"

    def test_a_failed_write_leaves_no_file_at_the_final_name(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A mid-write failure must not leave a truncated raster at the target."""
        monkeypatch.setattr(fake_reader.dataset, "to_file", _write_then_fail)
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = _plan_for(gee, var_info)
        with pytest.raises(RuntimeError, match="write failed"):
            gee._export_via_eedai(var_info, ["elevation"], 90.0, "srtm_elev", plan)
        assert not (gee.root_dir / "srtm_elev.tif").exists()
        assert not list(gee.root_dir.glob("*.partial.tif"))

    def test_forwards_asset_bands_crs_shape_and_bbox(self, make_gee, fake_reader):
        """Asset id, bands, crs, credentials and the bbox AOI are passed."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        asset_id, kwargs = fake_reader.calls[0]
        assert asset_id == "USGS/SRTMGL1_003"
        assert kwargs["bands"] == ["elevation"]
        assert kwargs["window"].crs == "EPSG:4326"
        assert kwargs["window"].bbox == (31.2, 29.9, 31.3, 30.0)
        assert kwargs["geometry"] is None
        assert kwargs["window"].resample == "nearest"

    def test_metre_scale_becomes_an_explicit_pixel_grid(self, make_gee, fake_reader):
        """`scale` (metres) is resolved to `shape=(rows, cols)`, not passed through.

        The reader sizes output in CRS units (degrees here), so a raw metre
        `scale` would produce a one-pixel raster.
        """
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["window"].shape == gee._eedai_grid(kwargs["window"].bbox, 90.0)
        assert kwargs["window"].scale is None
        assert all(axis > 1 for axis in kwargs["window"].shape), kwargs["window"].shape

    def test_grid_has_square_ground_pixels(self, make_gee):
        """The grid resolves to ~`scale` metres on both axes, not a worst-case bound.

        `estimate_pixel_dims` deliberately over-counts (it guards a pixel cap),
        which would skew a square AOI into non-square pixels.
        """
        gee = make_gee()
        bbox = (31.2, 29.9, 31.3, 30.0)
        rows, cols = gee._eedai_grid(bbox, 90.0)
        height_m = (bbox[3] - bbox[1]) * 111_320.0
        width_m = (bbox[2] - bbox[0]) * 111_320.0 * math.cos(math.radians(29.95))
        assert abs(height_m / rows - 90.0) < 1.0
        assert abs(width_m / cols - 90.0) < 1.0

    def test_tiny_aoi_still_yields_at_least_one_pixel(self, make_gee):
        """A sub-pixel AOI never rounds down to a zero-sized grid."""
        gee = make_gee()
        assert gee._eedai_grid((31.2, 29.9, 31.2001, 29.9001), 90.0) == (1, 1)

    @pytest.mark.parametrize(
        "bbox, scale, match",
        [
            ((31.2, 29.9, float("nan"), 30.0), 90.0, "finite"),
            ((31.2, 29.9, 31.3, 30.0), 0.0, "positive"),
            ((31.2, 29.9, 31.3, 30.0), -90.0, "positive"),
        ],
    )
    def test_degenerate_grid_inputs_are_rejected(self, make_gee, bbox, scale, match):
        """Non-finite bounds and a non-positive scale raise instead of sizing."""
        gee = make_gee()
        with pytest.raises(ValueError, match=match):
            gee._eedai_grid(bbox, scale)

    def test_grid_uses_the_poleward_edge_of_a_tall_aoi(self, make_gee):
        """A tall high-latitude AOI is sized so no row samples coarser than asked.

        Taking `cos` at the mid-latitude would under-count columns near the
        poleward edge, quietly sampling coarser than the requested scale.
        """
        gee = make_gee()
        tall = (0.0, 60.0, 1.0, 70.0)
        _rows, cols = gee._eedai_grid(tall, 1000.0)
        width_at_pole_m = 1.0 * 111_320.0 * math.cos(math.radians(70.0))
        assert width_at_pole_m / cols <= 1000.0 + 1.0

    def test_resample_is_forwarded(self, make_gee, fake_reader):
        """An explicit `resample` reaches the reader instead of its default."""
        gee = make_gee(resample="average")
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["window"].resample == "average"

    def test_api_routes_eligible_requests_to_eedai(self, make_gee, fake_reader):
        """`_api` takes the EEDAI path instead of `getDownloadURL`."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        image = _FakeImage()
        out = gee._api(
            image,
            var_info,
            ["elevation"],
            dt.datetime(2000, 2, 11),
            dt.datetime(2000, 1, 1),
            dt.datetime(2000, 1, 2),
        )
        assert out.suffix == ".tif"
        assert fake_reader.calls, "the EEDAI reader was not used"
        assert image.download_params is None, "getDownloadURL should not be called"

    def test_plain_geotiff_by_default(self, make_gee, fake_reader):
        """Without `cog=True` the raster is written via `to_file`."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        assert fake_reader.dataset.wrote_cog is False

    def test_cog_option_writes_a_cloud_optimized_geotiff(self, make_gee, fake_reader):
        """`cog=True` routes the write through `Dataset.cog.to_cog`."""
        gee = make_gee(cog=True)
        assert gee.cog is True
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        target = gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        assert fake_reader.dataset.wrote_cog is True
        assert target.read_bytes() == b"eedai-cog"

    def test_polygon_region_is_passed_as_a_cutline(self, make_gee, fake_reader):
        """A `region` exposing `total_bounds` is forwarded as `geometry=`."""
        region = _FakePolygonAoi()
        gee = make_gee(region=region)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["geometry"] is region
        assert kwargs["window"].bbox == region.total_bounds

    def test_region_window_and_grid_agree(self, make_gee, fake_reader):
        """The grid is sized for the region's window, not the wider lat/lon bbox.

        The reader windows on the bbox, so sizing the grid from a different
        extent would scale the ground resolution by the ratio between them.
        """
        region = _FakePolygonAoi()
        region.total_bounds = (31.20, 29.90, 31.22, 29.92)
        gee = make_gee(region=region, lat_lim=[29.0, 30.0], lon_lim=[31.0, 32.0])
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["window"].bbox == region.total_bounds
        assert kwargs["window"].shape == gee._eedai_grid(region.total_bounds, 90.0)
        wide = gee._eedai_grid((31.0, 29.0, 32.0, 30.0), 90.0)
        assert kwargs["window"].shape != wide, (
            "grid was sized from the bbox, not the region"
        )

    def test_oversized_read_is_served_by_tiling(self, make_gee, fake_reader):
        """A window too large for one pass is streamed in tiles, not refused."""
        gee = make_gee(lat_lim=[0.0, 15.0], lon_lim=[0.0, 15.0], scale=30.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size = plan.can_serve, plan.tile_size
        assert can_serve is True
        assert tile_size is not None
        assert tile_size >= 1
        assert gee._use_eedai(var_info, 1)[0] is True

    def test_tile_size_keeps_each_tile_native_read_within_budget(self, make_gee):
        """The tile shrinks so one tile's native-resolution read stays bounded."""
        gee = make_gee(lat_lim=[0.0, 15.0], lon_lim=[0.0, 15.0], scale=30.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size = plan.can_serve, plan.tile_size
        native_ratio = 30.0 / var_info.spatial_resolution
        assert tile_size * native_ratio <= backend_module.EE_MAX_DIMENSION

    def test_oversized_read_streams_to_a_path_in_tiles(self, make_gee, fake_reader):
        """The tiled read hands the reader `tile_size` and a destination path."""
        gee = make_gee(lat_lim=[0.0, 15.0], lon_lim=[0.0, 15.0], scale=30.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        target = gee._export_via_eedai(
            var_info, ["elevation"], 30.0, "srtm_big", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["tile_size"] >= 1
        assert kwargs["path"].endswith(".partial.tif")
        assert target.name == "srtm_big.tif"
        assert target.read_bytes() == b"eedai-tiled"
        assert not list(target.parent.glob("*.partial*.tif"))

    def test_total_pixel_budget_is_enforced_below_the_per_axis_cap(
        self, make_gee, fake_reader
    ):
        """A window under the per-axis cap can still be too big overall.

        Memory scales with the pixel count, not the longest side, so a wide
        square AOI can sit inside the per-axis budget and still be far past
        what one read may hold.
        """
        gee = make_gee(lat_lim=[0.0, 5.4], lon_lim=[0.0, 5.4], scale=1000.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        bbox, _cutline = gee._eedai_window()
        rows, cols = gee._eedai_grid(bbox, var_info.spatial_resolution)
        assert max(rows, cols) <= backend_module.EE_MAX_DIMENSION, "per-axis cap hit"
        fits, reason = gee._eedai_native_fits(var_info, bbox, 1)
        assert fits is False
        assert "budget" in reason

    def test_missing_extra_propagates_from_the_credential_build(
        self, make_gee, monkeypatch
    ):
        """An absent `[eedai]` extra surfaces as ImportError, not AuthenticationError."""
        monkeypatch.setattr(backend_module, "credentials_for", _raise_missing_extra)
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = _plan_for(gee, var_info)
        with pytest.raises(ImportError, match="eedai"):
            gee._export_via_eedai(var_info, ["elevation"], 90.0, "srtm_elev", plan)

    def test_non_nearest_resample_cannot_be_tiled(self, make_gee, fake_reader):
        """Upstream refuses `tile_size` with an interpolating resampler.

        `resample="average"` is the documented choice for coarser-than-native
        reads of continuous data — exactly the requests that trigger tiling —
        so this must fall back rather than surface upstream's raw error.
        """
        gee = make_gee(
            lat_lim=[0.0, 15.0], lon_lim=[0.0, 15.0], scale=30.0, resample="average"
        )
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size, reason = plan.can_serve, plan.tile_size, plan.reason
        assert can_serve is False
        assert tile_size is None
        assert "resample" in reason
        assert gee._use_eedai(var_info, 1)[0] is False

    def test_a_hard_constraint_is_reported_before_a_tunable_budget(
        self, make_gee, fake_reader
    ):
        """When both apply, the decline names the rule the user cannot tune.

        A coarse scale over a fine asset trips this repo's tiling-ratio budget,
        which the user can change; an interpolating resampler is refused by the
        reader itself, which they cannot.
        """
        gee = make_gee(
            lat_lim=[0.0, 15.0],
            lon_lim=[0.0, 15.0],
            scale=3000.0,
            resample="average",
        )
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        reason = gee._eedai_single_image_plan(var_info, 1).reason
        assert "resample" in reason, f"the tunable budget spoke first: {reason}"
        assert "worse than Earth Engine" not in reason

    def test_tile_respects_the_total_pixel_budget_not_just_the_axis_cap(self, make_gee):
        """One tile's native read must satisfy both budgets, not only per-axis.

        Sizing a tile so its native side lands on the per-axis cap would
        materialise ~32768**2 px — many times the total-pixel budget the
        single-pass path refuses.
        """
        gee = make_gee(lat_lim=[0.0, 15.0], lon_lim=[0.0, 15.0], scale=30.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size = plan.can_serve, plan.tile_size
        native_side = tile_size * (30.0 / var_info.spatial_resolution)
        assert native_side <= backend_module.EE_MAX_DIMENSION
        assert native_side**2 <= backend_module._EEDAI_MAX_PIXELS

    def test_too_many_tiles_falls_back_rather_than_starting(
        self, make_gee, fake_reader
    ):
        """A job needing thousands of tiles is refused, not silently started.

        Every tile is its own fetch, and the mosaic opens them together.
        """
        gee = make_gee(lat_lim=[0.0, 40.0], lon_lim=[0.0, 40.0], scale=30.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size, reason = plan.can_serve, plan.tile_size, plan.reason
        assert can_serve is False
        assert tile_size is None
        assert "total work" in reason
        assert gee._use_eedai(var_info, 1)[0] is False

    def test_a_much_coarser_read_falls_back_to_earth_engine(
        self, make_gee, fake_reader
    ):
        """Far-coarser-than-native reads belong on Earth Engine, not the reader.

        Tiling one would fetch `ratio**2` native pixels per output pixel only
        to discard them, where Earth Engine aggregates server-side and returns
        a small raster in one round trip.
        """
        gee = make_gee(lat_lim=[0.0, 40.0], lon_lim=[0.0, 40.0], scale=5000.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size, reason = plan.can_serve, plan.tile_size, plan.reason
        assert can_serve is False
        assert tile_size is None
        assert "worse than Earth Engine" in reason
        assert gee._use_eedai(var_info, 1)[0] is False

    def test_tiled_cog_write_leaves_no_staging_files(self, make_gee, fake_reader):
        """A tiled read plus `cog=True` stages through two names and cleans both."""
        gee = make_gee(lat_lim=[0.0, 15.0], lon_lim=[0.0, 15.0], scale=30.0, cog=True)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        target = gee._export_via_eedai(
            var_info, ["elevation"], 30.0, "srtm_big", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["tile_size"] >= 1
        assert target.read_bytes() == b"eedai-cog"
        assert not list(target.parent.glob("*.partial*.tif"))

    def test_a_failed_cog_conversion_leaves_no_staging_file(
        self, make_gee, fake_reader
    ):
        """A COG conversion that dies mid-write takes both staging files with it.

        The COG path stages through a second name, so a failure there can leave
        a truncated raster the next read would find and trust.
        """
        gee = make_gee(cog=True)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        fake_reader.dataset.cog.to_cog = _cog_then_fail
        plan = _plan_for(gee, var_info)
        with pytest.raises(RuntimeError, match="cog conversion failed"):
            gee._export_via_eedai(var_info, ["elevation"], 90.0, "srtm_elev", plan)
        assert not list(gee.root_dir.glob("*.partial*")), (
            "a staging file survived the failed conversion"
        )
        assert not (gee.root_dir / "srtm_elev.tif").exists(), (
            "a failed conversion still produced an output"
        )

    def test_a_failed_tiled_write_leaves_no_staging_file(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A mosaic that fails partway must not leave its staged file behind."""
        monkeypatch.setattr(fake_reader, "from_earthengine", _stage_then_fail)
        gee = make_gee(lat_lim=[0.0, 15.0], lon_lim=[0.0, 15.0], scale=30.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = _plan_for(gee, var_info)
        with pytest.raises(RuntimeError, match="mosaic failed"):
            gee._export_via_eedai(var_info, ["elevation"], 30.0, "srtm_big", plan)
        assert not list(gee.root_dir.glob("*.partial*.tif"))
        assert not (gee.root_dir / "srtm_big.tif").exists()

    def test_budget_is_spent_per_band(self, make_gee, fake_reader):
        """Band count divides the budget: the reader holds every band at once.

        A window that fits for one band can be several times over the limit
        for a multi-band request.
        """
        gee = make_gee(lat_lim=[0.0, 3.0], lon_lim=[0.0, 3.0], scale=90.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        bbox, _cutline = gee._eedai_window()
        one_band, _reason = gee._eedai_native_fits(var_info, bbox, 1)
        many_bands, reason = gee._eedai_native_fits(var_info, bbox, 13)
        assert one_band is True
        assert many_bands is False
        assert "13 band(s)" in reason

    def test_more_bands_never_loosen_the_plan(self, make_gee):
        """Adding bands only ever constrains the plan — never relaxes it.

        Past some band count the tile shrinks until the job needs more tiles
        than the ceiling allows, at which point the plan declines outright;
        both outcomes are stricter, never looser.
        """
        gee = make_gee(lat_lim=[0.0, 15.0], lon_lim=[0.0, 15.0], scale=30.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan_one = gee._eedai_single_image_plan(var_info, 1)
        ok_one, tile_one = plan_one.can_serve, plan_one.tile_size
        plan_many = gee._eedai_single_image_plan(var_info, 9)
        ok_many, tile_many = plan_many.can_serve, plan_many.tile_size
        assert ok_one is True
        assert (ok_many is False) or (tile_many < tile_one)

    def test_rename_retries_past_a_lingering_lock(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A `PermissionError` from a lingering GDAL handle is retried, not fatal.

        The rename happens after every tile has been fetched, so failing there
        would throw away the whole read.
        """
        flaky_replace = _FlakyReplace()
        monkeypatch.setattr(backend_module.os, "replace", flaky_replace)
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        target = gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        assert flaky_replace.attempts == 2, "the rename was not retried"
        assert target.exists()

    def test_a_persistent_lock_is_not_swallowed(
        self, make_gee, fake_reader, monkeypatch
    ):
        """If the rename keeps failing, that surfaces — losing the output silently
        would be worse than the error."""
        monkeypatch.setattr(backend_module.os, "replace", _always_permission_error)
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = _plan_for(gee, var_info)
        with pytest.raises(PermissionError):
            gee._export_via_eedai(var_info, ["elevation"], 90.0, "srtm_elev", plan)

    def test_staging_sidecars_are_cleaned_up(self, make_gee, fake_reader):
        """GDAL sidecars written next to a staged raster go with it."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        sidecar = gee.root_dir / "srtm_elev.partial.tif.aux.xml"

        fake_reader.dataset.to_file = _WriteWithSidecar(sidecar)
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        assert not sidecar.exists(), "the .aux.xml sidecar leaked"
        assert not list(gee.root_dir.glob("*.partial*"))

    def test_the_plan_is_computed_once_per_bucket(
        self, make_gee, fake_reader, monkeypatch
    ):
        """`_api` plans once and hands the verdict down, rather than re-deriving it.

        Recomputing in the exporter reprojects the region again and lets the
        routing decision and the read disagree.
        """
        _PLANS_SEEN.clear()
        monkeypatch.setattr(
            backend_module.GEE, "_eedai_single_image_plan", _recording_plan
        )
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._api(
            _FakeImage(),
            var_info,
            ["elevation"],
            dt.datetime(2000, 2, 11),
            dt.datetime(2000, 1, 1),
            dt.datetime(2000, 1, 2),
        )
        assert len(_PLANS_SEEN) == 1, f"the plan was computed {len(_PLANS_SEEN)} times"

    def test_an_empty_band_request_budgets_for_every_band(self, make_gee, fake_reader):
        """No bands means upstream opens them all, so the budget must say so."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        one_band = gee._eedai_native_fits(var_info, (0.0, 0.0, 3.0, 3.0), 1)
        every_band = gee._eedai_native_fits(
            var_info, (0.0, 0.0, 3.0, 3.0), len(var_info.bands)
        )
        assert one_band[0] is True
        assert every_band[0] is one_band[0] or every_band[0] is False

    def test_exporting_a_plan_that_cannot_serve_raises(self, make_gee, fake_reader):
        """Handing the exporter a declining plan raises instead of reading anyway.

        `_api` never does this; the guard exists so a future reordering cannot
        take the unguarded read silently.
        """
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        declined = backend_module.EedaiPlan(False, None, 0, "nope")
        with pytest.raises(ValueError, match="cannot serve"):
            gee._export_via_eedai(var_info, ["elevation"], 90.0, "srtm_elev", declined)
        assert not fake_reader.calls

    def test_an_unremovable_staging_file_does_not_fail_the_write(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A staging file that will not delete costs a stray temp, not the output."""
        real_unlink = Path.unlink

        def stubborn_unlink(self, missing_ok=False):
            if ".partial" in self.name:
                raise OSError("file is in use")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", stubborn_unlink)
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        target = gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        assert target.exists(), "the write was lost to a cleanup failure"

    def test_window_padding_can_leave_no_workable_tile(
        self, make_gee, fake_reader, monkeypatch
    ):
        """When the pad eats the whole per-tile budget, the read declines.

        Regression guard: dividing the grid by a zero-sized tile raised
        `ZeroDivisionError` mid-plan. The shipped budget always leaves a
        workable tile, so it is lowered here to reach the guard at all.
        """
        monkeypatch.setattr(backend_module, "_EEDAI_MAX_PIXELS", 25)
        gee = make_gee(lat_lim=[0.0, 3.0], lon_lim=[0.0, 3.0], scale=90.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size, reason = plan.can_serve, plan.tile_size, plan.reason
        assert can_serve is False
        assert tile_size is None
        assert "window padding" in reason

    def test_a_long_thin_aoi_exceeds_the_tile_ceiling(self, make_gee, fake_reader):
        """A narrow strip can pass the work budget and still need too many tiles.

        Tile count is not implied by total work: an elongated AOI keeps the
        pixel count modest while splitting into thousands of tiles, each its
        own fetch, all opened together to mosaic.
        """
        gee = make_gee(lat_lim=[0.0, 89.0], lon_lim=[0.0, 0.1], scale=1.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003").model_copy(
            update={"spatial_resolution": 1.0}
        )
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size, reason = plan.can_serve, plan.tile_size, plan.reason
        assert can_serve is False
        assert tile_size is None
        assert "tile ceiling" in reason
        assert "native px" not in reason, (
            "the work budget declined first, so this no longer covers the tile "
            f"ceiling: {reason}"
        )

    def test_a_cutline_cannot_be_tiled_so_it_falls_back(self, make_gee, fake_reader):
        """Upstream refuses `tile_size` with a polygon cutline, so `auto` falls back."""
        region = _FakePolygonAoi(total_bounds=(0.0, 0.0, 40.0, 40.0))
        gee = make_gee(region=region, scale=5000.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = gee._eedai_single_image_plan(var_info, 1)
        can_serve, tile_size, reason = plan.can_serve, plan.tile_size, plan.reason
        assert can_serve is False
        assert tile_size is None
        assert "cutline" in reason
        assert gee._use_eedai(var_info, 1)[0] is False

    def test_forced_eedai_reports_an_untileable_read(self, make_gee, fake_reader):
        """`engine="eedai"` turns an untileable oversized read into an error."""
        region = _FakePolygonAoi(total_bounds=(0.0, 0.0, 40.0, 40.0))
        gee = make_gee(engine="eedai", region=region, scale=5000.0)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = _plan_for(gee, var_info)
        with pytest.raises(ValueError, match="cutline"):
            gee._use_eedai(var_info, 1)

    def test_unknown_native_resolution_is_treated_as_unbounded(
        self, make_gee, fake_reader
    ):
        """An asset with no catalogued resolution cannot be sized, so it falls back.

        15 of the shipped `ee_type="image"` rows have no `spatial_resolution`,
        and an unknown native grid is the case that most needs bounding.
        """
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003").model_copy(
            update={"spatial_resolution": None}
        )
        fits, reason = gee._eedai_native_fits(var_info, (31.2, 29.9, 31.3, 30.0), 1)
        assert fits is False
        assert "no catalogued native resolution" in reason
        assert gee._use_eedai(var_info, 1)[0] is False

    def test_modest_aoi_passes_the_preflight(self, make_gee, fake_reader):
        """A small AOI is not blocked by the native-resolution budget."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        assert gee._use_eedai(var_info, 1)[0] is True
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        assert fake_reader.calls

    def test_credentials_are_built_once_and_reused(self, make_gee, fake_reader):
        """The credential is resolved once per instance, not per written bucket.

        Inline key material lands in a temp file whose removal is left to the
        GC, so rebuilding per bucket would scatter transient key files.
        """
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "a", _plan_for(gee, var_info)
        )
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "b", _plan_for(gee, var_info)
        )
        assert len(fake_reader.credential_builds) == 1
        assert len(fake_reader.calls) == 2

    def test_missing_key_warns_before_falling_back_to_adc(
        self, make_gee, fake_reader, monkeypatch
    ):
        """No resolvable key logs the ADC fallback instead of switching silently."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee()
        monkeypatch.setattr(gee, "_resolve_credentials", lambda: (None, None, None))
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        assert any("Application Default Credentials" in w for w in warnings), warnings

    def test_credential_failure_becomes_an_authentication_error(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A reader credential failure surfaces as earthlens's AuthenticationError."""
        monkeypatch.setattr(
            backend_module,
            "credentials_for",
            lambda key: (_ for _ in ()).throw(RuntimeError("bad key")),
        )
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        plan = _plan_for(gee, var_info)
        with pytest.raises(backend_module.AuthenticationError, match="EEDAI"):
            gee._export_via_eedai(var_info, ["elevation"], 90.0, "srtm_elev", plan)

    def test_cog_on_an_ee_request_warns_once(self, make_gee, fake_reader, monkeypatch):
        """`cog=True` cannot apply on the Earth Engine path, so it says so once."""
        warnings: list[str] = []
        monkeypatch.setattr(
            backend_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = make_gee(engine="ee", cog=True)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        for _ in range(2):
            gee._api(
                var_info=var_info,
                image=_FakeImage(),
                bands=["elevation"],
                when=dt.datetime(2000, 2, 11),
                bucket_start=dt.datetime(2000, 1, 1),
                bucket_end=dt.datetime(2000, 1, 2),
            )
        assert len([w for w in warnings if "cog=True has no effect" in w]) == 1

    def test_projected_region_is_reprojected_before_windowing(
        self, make_gee, fake_reader
    ):
        """A projected region is moved to lat/lon so bbox and cutline agree.

        The reader reprojects a CRS-carrying geometry but reads `bbox` as
        already being in the target CRS, so projected bounds would window a
        different part of the planet than the cutline clips.
        """
        region = _FakePolygonAoi(
            epsg=32636, total_bounds=(330000.0, 3310000.0, 340000.0, 3320000.0)
        )
        gee = make_gee(region=region)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["geometry"] is not region, "the projected region was reused"
        assert kwargs["geometry"].reprojected_to == "EPSG:4326"
        assert kwargs["window"].bbox == kwargs["geometry"].total_bounds

    def test_wgs84_region_is_used_as_is(self, make_gee, fake_reader):
        """A region already in EPSG:4326 is not needlessly reprojected."""
        region = _FakePolygonAoi(epsg=4326)
        gee = make_gee(region=region)
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "srtm_elev", _plan_for(gee, var_info)
        )
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["geometry"] is region

    def test_reauthenticating_drops_the_cached_credential(self, make_gee, fake_reader):
        """A new `authenticate()` must not reuse the previous identity's credential."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "a", _plan_for(gee, var_info)
        )
        gee.authenticate(service_account="other@x.iam", service_key="other.json")
        gee._export_via_eedai(
            var_info, ["elevation"], 90.0, "b", _plan_for(gee, var_info)
        )
        assert fake_reader.credential_builds == ["key.json", "other.json"]

    def test_api_uses_getdownloadurl_when_engine_is_ee(self, make_gee, fake_reader):
        """`engine="ee"` keeps the historical `getDownloadURL` path."""
        gee = make_gee(engine="ee")
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        image = _FakeImage()
        gee._api(
            image,
            var_info,
            ["elevation"],
            dt.datetime(2000, 2, 11),
            dt.datetime(2000, 1, 1),
            dt.datetime(2000, 1, 2),
        )
        assert not fake_reader.calls
        assert image.download_params is not None
