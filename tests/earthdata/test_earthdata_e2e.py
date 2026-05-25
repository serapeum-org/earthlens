"""Live end-to-end tests for the NASA Earthdata backend.

Hits real Earthdata Login (EDL) + CMR + a DAAC. Gated behind both the
`e2e` pytest marker and the EDL env vars (`EARTHDATA_USERNAME` /
`EARTHDATA_PASSWORD`), so a default `pytest` invocation skips them.

Run with:

    EARTHDATA_USERNAME=... EARTHDATA_PASSWORD=... \\
    pixi run -e dev pytest -m "e2e and earthdata" tests/earthdata
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens

# A non-interactive matplotlib backend so the notebooks' plot cells run headless.
os.environ.setdefault("MPLBACKEND", "Agg")

_HAVE_CREDS = bool(
    (os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD"))
    or os.environ.get("EARTHDATA_TOKEN")
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO_ROOT / "docs" / "examples" / "earthdata"
# The real-life (live-download) notebooks — distinct from the offline
# catalog_explorer / output_kinds demos, which need no credentials.
# The live notebooks the token-OR-userpass e2e exercises. opera_s1_backscatter
# is NOT here: ASF's datapool uses an EDL OAuth redirect that drops a bearer
# token across hosts (401), so it needs username/password — it has its own
# gated test (TestEarthdataAsfNotebook) below.
_LIVE_NOTEBOOKS = [
    "imerg_precipitation.ipynb",
    "gedi_l4a_footprints.ipynb",
    "pace_ocean_colour.ipynb",
    "smap_soil_moisture.ipynb",
]

_HAVE_USERPASS = bool(
    os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD")
)


def _run_notebook_cells(path: Path, workdir: Path) -> str:
    """Exec a notebook's code cells in one namespace; return captured stdout.

    Runs with `workdir` as the working directory (the notebooks write under a
    relative `earthdata_output/`). Any cell exception propagates so the test
    fails. A full kernel run is avoided deliberately: it keeps the e2e robust
    without depending on a registered Jupyter kernelspec in the CI env.
    """
    import nbformat

    nb = nbformat.read(str(path), as_version=4)
    namespace: dict = {}
    buffer = io.StringIO()
    prev = Path.cwd()
    os.chdir(workdir)
    try:
        with contextlib.redirect_stdout(buffer):
            for cell in nb.cells:
                if cell.cell_type == "code":
                    exec(compile(cell.source, f"{path.name}#cell", "exec"), namespace)
    finally:
        os.chdir(prev)
    return buffer.getvalue()

# GPM IMERG late half-hourly has a publication latency of roughly half a
# day; probe ~10 days back so the requested window is comfortably
# populated regardless of the exact run time.
_PROBE_DATE = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).strftime(
    "%Y-%m-%d"
)


@pytest.mark.e2e
@pytest.mark.earthdata
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason="set EARTHDATA_USERNAME / EARTHDATA_PASSWORD to run live Earthdata e2e tests",
)
class TestEarthdataLiveFetch:
    """Single tiny granule fetch against a public Earthdata collection."""

    def test_imerg_one_day_small_box(self, tmp_path: Path):
        """GPM IMERG — small bbox × 1 day → at least one granule on disk."""
        el = EarthLens(
            data_source="earthdata",
            start=_PROBE_DATE,
            end=_PROBE_DATE,
            variables={"GPM_3IMERGHHL_07": ["precipitation"]},
            lat_lim=[0.0, 2.0],
            lon_lim=[0.0, 2.0],
            temporal_resolution="daily",
            path=str(tmp_path),
            direct_s3="never",
        )
        paths = el.download(progress_bar=False)
        assert paths, f"no granules written into {tmp_path!r}: {list(tmp_path.iterdir())!r}"
        assert all(Path(p).exists() for p in paths), (
            f"download() returned non-existent paths: {paths!r}"
        )


@pytest.mark.e2e
@pytest.mark.earthdata
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason="set EARTHDATA_USERNAME / EARTHDATA_PASSWORD to run live Earthdata e2e tests",
)
class TestEarthdataExampleNotebooks:
    """Execute the real-life example notebooks against live EDL."""

    @pytest.mark.parametrize("notebook", _LIVE_NOTEBOOKS)
    def test_live_notebook_runs(self, notebook: str, tmp_path: Path):
        """The notebook's cells run end-to-end and its live query is not skipped."""
        out = _run_notebook_cells(_EXAMPLES / notebook, tmp_path)
        assert "skipped live query" not in out, (
            f"{notebook} fell into the offline skip branch — the live query failed:\n{out}"
        )
        assert "granule(s)" in out, f"{notebook} did not report a fetch:\n{out}"


@pytest.mark.e2e
@pytest.mark.earthdata
@pytest.mark.skipif(
    not _HAVE_USERPASS,
    reason="ASF needs EARTHDATA_USERNAME / EARTHDATA_PASSWORD (a bearer token 401s on its OAuth redirect)",
)
class TestEarthdataAsfNotebook:
    """The OPERA / ASF notebook — runs only on the username/password path."""

    def test_opera_runs_with_userpass(self, tmp_path: Path, monkeypatch):
        """OPERA downloads via the EDL OAuth (username/password) path, not a token."""
        # ASF's datapool drops a bearer token across its cross-host OAuth redirect,
        # so force the username/password path by removing any token from the env.
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        out = _run_notebook_cells(_EXAMPLES / "opera_s1_backscatter.ipynb", tmp_path)
        assert "skipped live query" not in out, (
            f"opera_s1_backscatter fell into the skip branch — ASF download failed:\n{out}"
        )
        assert "file(s)" in out, f"opera_s1_backscatter did not report a fetch:\n{out}"
