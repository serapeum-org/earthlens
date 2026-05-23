"""Probe one sample granule to seed a catalog row (C9).

The CMR analog of ``tools/ecmwf/probe_cds_netcdf.py``. For a given
collection, search CMR for one granule in a tiny window, record its
download URL / on-disk format and an inferred `output_kind`, and write a
JSON sidecar that seeds the catalog `format` / `output_kind` (and, when
``--open`` is given and the granule is reachable, a `bands` list).

Run:

    pixi run -e dev python tools/earthdata/probe_earthdata_granule.py \\
        GPM_3IMERGHHL 07 --provider GES_DISC --out probe.json

Requires the ``earthdata`` extra (``earthaccess``; Python >=3.12). Not
part of the installed package.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _earthdata_cmr import format_from_extension, infer_output_kind  # noqa: E402


def _load_earthaccess():
    """Import earthaccess or exit with a friendly message."""
    try:
        import earthaccess
    except ImportError:
        sys.exit(
            "earthaccess is required to probe a granule; install "
            "`pip install earthlens[earthdata]` (Python >=3.12)."
        )
    return earthaccess


def _granule_url(granule: Any) -> str:
    """Best-effort extraction of a granule's first data URL."""
    try:
        return granule.data_links()[0]
    except (AttributeError, IndexError, TypeError):
        pass
    try:
        for url in granule["umm"]["RelatedUrls"]:
            if url.get("Type") == "GET DATA":
                return url.get("URL", "")
    except (KeyError, TypeError):
        pass
    return ""


def probe(args: argparse.Namespace) -> int:
    """Search one granule, infer its shape, and write the JSON sidecar."""
    earthaccess = _load_earthaccess()
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=args.days_back)
    granules = earthaccess.search_data(
        short_name=args.short_name,
        version=args.version or None,
        provider=args.provider,
        temporal=(start.isoformat(), end.isoformat()),
        count=1,
    )
    if not granules:
        sys.exit(f"no granules found for {args.short_name!r} in the probe window.")

    url = _granule_url(granules[0])
    fmt = format_from_extension(url) or "unknown"
    output_kind = infer_output_kind(args.short_name, fmt)
    record = {
        "short_name": args.short_name,
        "version": args.version,
        "provider": args.provider,
        "sample_url": url,
        "format": fmt,
        "output_kind": output_kind,
    }
    out = json.dumps(record, indent=2)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote probe sidecar -> {args.out}")
    else:
        print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse args and run the probe."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("short_name")
    parser.add_argument("version", nargs="?", default="")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--days-back", type=int, default=30, dest="days_back")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    return probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
