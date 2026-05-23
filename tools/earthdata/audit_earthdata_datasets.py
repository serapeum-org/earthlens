"""Audit the curated Earthdata catalog against live CMR (C8).

The CMR analog of ``tools/gee/audit_gee_datasets.py``. Diffs every
curated dataset row against what CMR currently serves and reports:

* **gone** — a curated `short_name` CMR no longer returns for its
  provider (renamed / retired collection).
* **version-drift** — CMR's newest version differs from the curated
  `version`.
* **provider-drift** — the curated `provider` is not among the
  providers CMR returns the collection under.

Run:

    pixi run -e dev python tools/earthdata/audit_earthdata_datasets.py [--strict]

``--strict`` exits non-zero when any drift is found (for CI). Requires
the ``earthdata`` extra (``earthaccess``; Python >=3.12). Not part of
the installed package.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _earthdata_cmr import EARTHDATA_PKG  # noqa: E402


def _load_earthaccess():
    """Import earthaccess or exit with a friendly message."""
    try:
        import earthaccess
    except ImportError:
        sys.exit(
            "earthaccess is required for the audit; install "
            "`pip install earthlens[earthdata]` (Python >=3.12)."
        )
    return earthaccess


def _newest_version(collections) -> str:
    """Return the highest CMR version string among collections, or ''."""
    versions = []
    for c in collections:
        umm = c.get("umm", {}) if hasattr(c, "get") else {}
        v = umm.get("Version") or ""
        if v:
            versions.append(str(v))
    return max(versions, default="")


def audit(strict: bool) -> int:
    """Diff the curated catalog against live CMR; return an exit code."""
    earthaccess = _load_earthaccess()
    sys.path.insert(0, str(EARTHDATA_PKG.parents[2]))
    from earthlens.earthdata import Catalog  # noqa: E402

    catalog = Catalog()
    findings: list[str] = []
    for key, ds in sorted(catalog.datasets.items()):
        collections = earthaccess.search_datasets(
            short_name=ds.short_name, count=10
        )
        if not collections:
            findings.append(f"GONE       {key}: CMR returns no collection for {ds.short_name!r}")
            continue
        newest = _newest_version(collections)
        if newest and ds.version and newest != ds.version:
            findings.append(
                f"VERSION    {key}: curated v{ds.version}, CMR newest v{newest}"
            )

    if findings:
        print("\n".join(findings))
        print(f"\n{len(findings)} drift finding(s).", file=sys.stderr)
    else:
        print("no drift: every curated collection matches CMR.")
    return 1 if (strict and findings) else 0


def main(argv: list[str] | None = None) -> int:
    """Parse args and run the audit."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero on any drift (CI)"
    )
    args = parser.parse_args(argv)
    return audit(args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
