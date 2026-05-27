"""Validate a Herbie-backed NWP catalog row's bands against the live `.idx` - no eccodes.

`probe_nwp_model.py` resolves availability via Herbie, which needs the
`cfgrib`/`eccodes` stack (the eccodes C library is unavailable on some
platforms - notably the Windows dev box). This tool is its **eccodes-free**
complement: it reads Herbie's own model **template** file as data (without
importing `herbie`, whose package init pulls `cfgrib`), formats the AWS/NOMADS
GRIB2 URL for a recent cycle, fetches the sidecar `.idx` text, and checks which
of the catalog row's `bands:` selectors actually appear in it.

That verifies the two things the catalog can drift on without anyone noticing
(the row degrades gracefully - a missing band is skipped at fetch time): the
**product** path is right, and each advertised **band token** really exists in
the file. The final GRIB *decode* still needs eccodes and is out of scope here.

Probable rows are the NODD models whose template builds the URL from
`date` / `product` / `fxx` only (`gfs`, `rap`, `nam`, `nbm`, `rtma`, `urma`,
`nam-conusnest`, …). Rows whose template also needs `domain` / `member` /
`resolution` (`hiresw`, `href`, `gefs`) or that download per-variable whole
files with no `.idx` (the ECCC `gdps` / `rdps` / `hrdps` MSC datamart) are
reported as **not idx-probable** rather than guessed at.

Run with:

    pixi run -e dev python tools/nwp/probe_idx.py rap
    pixi run -e dev python tools/nwp/probe_idx.py --all
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import runpy
import sys
import urllib.request

from earthlens.nwp import Catalog
from earthlens.nwp.catalog import NWPModel

#: ECCC models download one whole GRIB file per variable (MSC datamart), with no
#: `.idx` byte-range index, so the idx-token check does not apply to them.
_NO_IDX_FAMILIES = {"gdps", "rdps", "hrdps"}

#: Template families whose URL also depends on attributes this probe does not
#: synthesise (domain / member / resolution); reported rather than mis-probed.
_NEEDS_EXTRA_ATTRS = {"hiresw", "href", "gefs"}


def _herbie_models_dir() -> pathlib.Path:
    """Locate the installed `herbie/models` template directory.

    Returns:
        pathlib.Path: The template directory.

    Raises:
        FileNotFoundError: When `herbie` is not importable on `sys.path`.
    """
    for entry in sys.path:
        cand = pathlib.Path(entry) / "herbie" / "models"
        if cand.is_dir():
            return cand
    raise FileNotFoundError(
        "herbie is not installed; install `pip install earthlens[nwp]` "
        "(the templates are read as data - eccodes is not needed)."
    )


class _TemplateStub:
    """Minimal stand-in for a Herbie object so a template's f-strings resolve.

    Only the attributes a NODD template references are set; any other
    attribute access returns an empty string so template evaluation does
    not raise on a model this probe does not fully support.
    """

    def __init__(self, date: dt.datetime, fxx: int, product: str) -> None:
        self.date = date
        self.fxx = fxx
        self.product = product

    def __getattr__(self, name: str) -> str:
        return ""


def _idx_url(models_dir: pathlib.Path, model: NWPModel, cycle: dt.datetime, step: int) -> str:
    """Format the `.idx` URL for a model/cycle/step from its Herbie template.

    Args:
        models_dir: The `herbie/models` directory.
        model: The resolved catalog row.
        cycle: The forecast cycle datetime (UTC).
        step: The forecast lead time in hours.

    Returns:
        str: The `.idx` sidecar URL (AWS first, else the first source).
    """
    # Exec the template module as data rather than `import herbie.models.<fam>`:
    # importing the package triggers herbie/__init__ -> cfgrib -> eccodes (the
    # binary this probe deliberately avoids). The file is Herbie's own installed
    # code and this is a dev-only tool (not shipped in the wheel), so running it
    # is safe.
    namespace = runpy.run_path(str(models_dir / f"{model.model_family}.py"))
    cls = namespace.get(model.model_family) or next(
        v for v in namespace.values() if isinstance(v, type) and hasattr(v, "template")
    )
    stub = _TemplateStub(cycle, step, model.product or "")
    cls.template(stub)
    # Herbie defaults an unset product to the first template PRODUCTS key.
    if not model.product and getattr(stub, "PRODUCTS", None):
        stub = _TemplateStub(cycle, step, list(stub.PRODUCTS)[0])
        cls.template(stub)
    sources = stub.SOURCES
    base = sources.get("aws") or sources.get("nomads") or next(iter(sources.values()))
    return base + ".idx"


def _fetch(url: str, timeout: int = 25) -> tuple[int | None, str]:
    """GET `url`, returning `(status, body)` or `(None, error)`."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - https only
            return resp.status, resp.read().decode("latin-1", "replace")
    except Exception as exc:  # noqa: BLE001 - report any failure to the caller
        return None, f"{type(exc).__name__}: {exc}"


def probe(model_key: str, model: NWPModel, days_back: int = 1) -> None:
    """Probe one model's `.idx` and print which bands are present / missing."""
    if model.model_family in _NO_IDX_FAMILIES:
        print(f"[{model_key}] not idx-probable - ECCC per-variable files, no .idx")
        return
    if model.model_family in _NEEDS_EXTRA_ATTRS:
        print(
            f"[{model_key}] not idx-probable here - {model.model_family} template needs "
            "domain/member/resolution; use probe_nwp_model.py (Herbie + eccodes)"
        )
        return

    models_dir = _herbie_models_dir()
    step = 1 if (model.horizon_h or 0) >= 1 else 0
    status: int | None = None
    body = ""
    url = ""
    for back in range(days_back, days_back + 2):
        cycle = (dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=back)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        url = _idx_url(models_dir, model, cycle, step)
        status, body = _fetch(url)
        if status == 200:
            break

    if status != 200:
        print(f"[{model_key}] idx unreachable: {body[:80]}\n        {url}")
        return

    present = [b for b, token in model.bands.items() if re.search(re.escape(token), body)]
    missing = [b for b in model.bands if b not in present]
    tag = "OK" if not missing else f"{len(missing)} MISSING"
    print(
        f"[{model_key}] product={model.product} idx={len(body.splitlines())} msgs - "
        f"{len(present)}/{len(model.bands)} bands present  [{tag}]"
    )
    if missing:
        print(f"        missing: {', '.join(missing)}")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model", nargs="?", help="catalog model key (e.g. rap)")
    parser.add_argument(
        "--all", action="store_true", help="probe every herbie-backed row"
    )
    args = parser.parse_args()

    catalog = Catalog()
    if args.all:
        rows = [(k, m) for k, m in catalog.datasets.items() if m.backend == "herbie"]
    elif args.model:
        rows = [(args.model, catalog.get_dataset(args.model))]
    else:
        parser.error("pass a model key or --all")

    for key, model in rows:
        probe(key, model)


if __name__ == "__main__":
    main()
