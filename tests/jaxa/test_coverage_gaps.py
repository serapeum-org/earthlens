"""Coverage-gap fillers for the JAXA subpackage.

Targets specific uncovered lines surfaced by `pytest --cov=src/earthlens/jaxa`:

* `backend.py` — the `_api` dispatch (jaxa-earth and gportal branches) and the
  `download()` → `_api()` path that the per-method unit tests don't reach.
* `catalog.py` — duplicate-alias error, empty `datasets:` block error, malformed
  row `ValidationError` wrap, and the `get_catalog()` thin alias.
* `_jaxa_earth.py` / `_gportal.py` — the `ImportError` hint for a missing SDK,
  and the missing-collection / missing-short-name `ValueError` for a bad row.
* `auth.py` — the `is_authenticated()` short-circuit on a repeated `configure()`.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from earthlens.jaxa import JAXA, AuthenticationError, JaxaAuth, JaxaCredentials
from earthlens.jaxa.catalog import Catalog, Dataset, clear_catalog_cache


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    """Drop the per-`(path, mtime)` cache so tests don't share state."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_load_rejects_empty_datasets_block(tmp_path) -> None:
    """An empty datasets block raises ValueError on load."""
    path = tmp_path / "empty.yaml"
    path.write_text("datasets: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty 'datasets:' block"):
        Catalog.load(catalog_path=path)


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_load_wraps_row_validation_error(tmp_path) -> None:
    """A row that fails pydantic validation surfaces as a friendly ValueError."""
    path = tmp_path / "bad.yaml"
    path.write_text(
        "datasets:\n"
        "  bad-row:\n"
        "    protocol: jaxa-earth\n"
        "    short_name: 'oops'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(catalog_path=path)


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_rejects_alias_collision() -> None:
    """Two rows claiming the same alias raise during model_post_init."""
    datasets = {
        "row-a": Dataset(
            key="row-a",
            protocol="jaxa-earth",
            collection="JAXA.A",
            aliases=["shared"],
        ),
        "row-b": Dataset(
            key="row-b",
            protocol="jaxa-earth",
            collection="JAXA.B",
            aliases=["shared"],
        ),
    }
    with pytest.raises(ValueError, match="claimed by both"):
        Catalog(datasets=datasets)


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_get_catalog_returns_datasets_mapping() -> None:
    """`get_catalog()` returns the same mapping as `.datasets`."""
    cat = Catalog()
    assert cat.get_catalog() is cat.datasets


@pytest.mark.jaxa
@pytest.mark.unit
def test_auth_protocol_property_reads_bound_value() -> None:
    """`JaxaAuth.protocol` returns the value bound at construction."""
    assert JaxaAuth(JaxaCredentials(), protocol="gportal").protocol == "gportal"
    assert JaxaAuth(JaxaCredentials(), protocol="jaxa-earth").protocol == "jaxa-earth"


@pytest.mark.jaxa
@pytest.mark.unit
def test_auth_configure_idempotent_for_gportal(monkeypatch) -> None:
    """A second `configure()` for the gportal protocol is a no-op."""
    monkeypatch.setenv("GPORTAL_USERNAME", "bob")
    monkeypatch.setenv("GPORTAL_PASSWORD", "envpass")
    auth = JaxaAuth(JaxaCredentials(), protocol="gportal")
    auth.configure()
    monkeypatch.delenv("GPORTAL_PASSWORD")
    auth.configure()
    assert auth.username == "bob"


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_branch_friendly_import_error(monkeypatch, tmp_path) -> None:
    """A missing `jaxa.earth` install raises a friendly hint about [jaxa]."""
    for name in list(sys.modules):
        if name == "jaxa.earth" or name.startswith("jaxa.earth."):
            monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "jaxa.earth", None)
    from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
    from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

    space = SpatialExtent(
        latitude_min=0.0, latitude_max=1.0, longitude_min=0.0, longitude_max=1.0
    )
    import datetime as dt

    import pandas as pd

    time = TemporalExtent(
        start_date=dt.datetime(2024, 1, 1),
        end_date=dt.datetime(2024, 1, 2),
        resolution="D",
        dates=pd.date_range("2024-01-01", "2024-01-02", freq="D"),
    )
    ds = Dataset(
        key="aw3d30",
        protocol="jaxa-earth",
        collection="JAXA.foo",
        default_band="b",
    )
    with pytest.raises(ImportError, match=r"earthlens\[jaxa\]"):
        fetch_jaxa_earth(
            dataset=ds,
            space=space,
            time=time,
            resolution=None,
            bands=None,
            out_dir=tmp_path,
        )


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_branch_rejects_missing_collection(tmp_path) -> None:
    """A jaxa-earth row stripped of its collection raises at fetch time."""
    from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
    from earthlens.jaxa._jaxa_earth import fetch_jaxa_earth

    space = SpatialExtent(
        latitude_min=0.0, latitude_max=1.0, longitude_min=0.0, longitude_max=1.0
    )
    import datetime as dt

    import pandas as pd

    time = TemporalExtent(
        start_date=dt.datetime(2024, 1, 1),
        end_date=dt.datetime(2024, 1, 2),
        resolution="D",
        dates=pd.date_range("2024-01-01", "2024-01-02", freq="D"),
    )

    class _FakeDataset:
        key = "aw3d30"
        collection = None
        default_band = "DSM"

    with pytest.raises(ValueError, match="no collection"):
        fetch_jaxa_earth(
            dataset=_FakeDataset(),  # type: ignore[arg-type]
            space=space,
            time=time,
            resolution=None,
            bands=None,
            out_dir=tmp_path,
        )


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_branch_friendly_import_error(monkeypatch, tmp_path) -> None:
    """A missing `gportal` install raises a friendly hint about [jaxa]."""
    monkeypatch.setitem(sys.modules, "gportal", None)
    from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
    from earthlens.jaxa._gportal import fetch_gportal

    space = SpatialExtent(
        latitude_min=0.0, latitude_max=1.0, longitude_min=0.0, longitude_max=1.0
    )
    import datetime as dt

    import pandas as pd

    time = TemporalExtent(
        start_date=dt.datetime(2024, 1, 1),
        end_date=dt.datetime(2024, 1, 2),
        resolution="D",
        dates=pd.date_range("2024-01-01", "2024-01-02", freq="D"),
    )
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    auth = JaxaAuth(JaxaCredentials(), protocol="gportal")
    with pytest.raises(ImportError, match=r"earthlens\[jaxa\]"):
        fetch_gportal(
            dataset=ds, space=space, time=time, auth=auth, out_dir=tmp_path
        )


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_branch_rejects_missing_short_name(monkeypatch, tmp_path) -> None:
    """A gportal row stripped of its short_name raises at fetch time."""
    fake = types.ModuleType("gportal")
    fake.search = lambda **_kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gportal", fake)
    from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
    from earthlens.jaxa._gportal import fetch_gportal

    space = SpatialExtent(
        latitude_min=0.0, latitude_max=1.0, longitude_min=0.0, longitude_max=1.0
    )
    import datetime as dt

    import pandas as pd

    time = TemporalExtent(
        start_date=dt.datetime(2024, 1, 1),
        end_date=dt.datetime(2024, 1, 2),
        resolution="D",
        dates=pd.date_range("2024-01-01", "2024-01-02", freq="D"),
    )

    class _BadDataset:
        key = "sgli"
        short_name = None

    auth = JaxaAuth(
        JaxaCredentials(
            gportal_username="x",
            gportal_password=__import__("pydantic").SecretStr("y"),
        ),
        protocol="gportal",
    )
    auth.configure()
    with pytest.raises(ValueError, match="no short_name"):
        fetch_gportal(
            dataset=_BadDataset(),  # type: ignore[arg-type]
            space=space,
            time=time,
            auth=auth,
            out_dir=tmp_path,
        )


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_branch_empty_products_returns_empty(monkeypatch, tmp_path) -> None:
    """A matched>0 with empty products list still returns `[]`."""
    fake = types.ModuleType("gportal")

    class _Search:
        def matched(self):
            return 5

        def products(self, convert_types=True):
            return iter([])

    fake.search = lambda **_kw: _Search()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gportal", fake)
    from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
    from earthlens.jaxa._gportal import fetch_gportal

    space = SpatialExtent(
        latitude_min=0.0, latitude_max=1.0, longitude_min=0.0, longitude_max=1.0
    )
    import datetime as dt

    import pandas as pd

    time = TemporalExtent(
        start_date=dt.datetime(2024, 1, 1),
        end_date=dt.datetime(2024, 1, 2),
        resolution="D",
        dates=pd.date_range("2024-01-01", "2024-01-02", freq="D"),
    )
    ds = Dataset(key="sgli", protocol="gportal", short_name="10003001")
    auth = JaxaAuth(
        JaxaCredentials(
            gportal_username="alice",
            gportal_password=__import__("pydantic").SecretStr("topsecret"),
        ),
        protocol="gportal",
    )
    auth.configure()
    assert fetch_gportal(
        dataset=ds, space=space, time=time, auth=auth, out_dir=tmp_path
    ) == []


def _fake_jaxa_earth(monkeypatch):
    """Inject a minimal fake `jaxa.earth.je` for the dispatch tests."""
    import numpy as np

    class _Raster:
        img = np.ones((1, 2, 3, 1), dtype=np.float32)
        latlim = np.array([[0.0, 1.0]])
        lonlim = np.array([[0.0, 1.5]])

    class _Result:
        raster = _Raster()

    class _Col:
        def __init__(self, *, collection, **_kw):
            self.collection = collection

        def filter_date(self, *, dlim):
            return self

        def filter_resolution(self, *, ppu):
            return self

        def filter_bounds(self, *, bbox=None, geoj=None):
            return self

        def select(self, *, band):
            return self

        def get_images(self):
            return _Result()

    fake_je = types.ModuleType("jaxa.earth.je")
    fake_je.ImageCollection = _Col  # type: ignore[attr-defined]
    fake_earth = types.ModuleType("jaxa.earth")
    fake_earth.je = fake_je  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("jaxa")
    fake_pkg.earth = fake_earth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jaxa", fake_pkg)
    monkeypatch.setitem(sys.modules, "jaxa.earth", fake_earth)
    monkeypatch.setitem(sys.modules, "jaxa.earth.je", fake_je)


@pytest.mark.jaxa
@pytest.mark.integration
def test_backend_download_dispatches_to_jaxa_earth(monkeypatch, tmp_path) -> None:
    """`download()` on a jaxa-earth request writes a COG via the branch."""
    _fake_jaxa_earth(monkeypatch)
    backend = JAXA(
        start="2020-01-01",
        end="2020-12-31",
        variables=["elevation"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.5],
        path=tmp_path,
    )
    written = backend.download()
    assert len(written) == 1
    assert written[0].exists()


@pytest.mark.jaxa
@pytest.mark.integration
def test_backend_download_dispatches_to_gportal(monkeypatch, tmp_path) -> None:
    """`download()` on a gportal request authenticates and downloads."""
    fake = types.ModuleType("gportal")
    fake.username = None  # type: ignore[attr-defined]
    fake.password = None  # type: ignore[attr-defined]

    class _Search:
        def matched(self):
            return 1

        def products(self, convert_types=True):
            return iter([type("P", (), {"id": "X"})()])

    fake.search = lambda **_kw: _Search()  # type: ignore[attr-defined]

    def _download(target, local_dir=".", username=None, password=None):
        Path(local_dir, "X.dat").write_bytes(b"x")
        return [str(Path(local_dir) / "X.dat")]

    fake.download = _download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gportal", fake)
    monkeypatch.setenv("GPORTAL_USERNAME", "alice")
    monkeypatch.setenv("GPORTAL_PASSWORD", "topsecret")
    backend = JAXA(
        start="2024-01-01",
        end="2024-01-02",
        variables=["sgli-l380"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.5],
        path=tmp_path,
    )
    written = backend.download()
    assert [p.name for p in written] == ["X.dat"]


@pytest.mark.jaxa
@pytest.mark.integration
def test_backend_download_propagates_missing_gportal_credentials(
    monkeypatch, tmp_path
) -> None:
    """`download()` on a gportal request without env vars raises AuthenticationError."""
    monkeypatch.delenv("GPORTAL_USERNAME", raising=False)
    monkeypatch.delenv("GPORTAL_PASSWORD", raising=False)
    backend = JAXA(
        start="2024-01-01",
        end="2024-01-02",
        variables=["sgli-l380"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.5],
        path=tmp_path,
    )
    with pytest.raises(AuthenticationError):
        backend.download()
