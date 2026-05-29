"""Probe the live WorldPop REST API shape and write a JSON sidecar.

Walks `hub.worldpop.org/rest/data`: the top-level product aliases, each
product's sub-alias ids, and a sample record for one `(alias, subalias,
iso3)` triple — capturing the record's field names, the `popyear` spread,
and the `files` head. The sidecar seeds / verifies the catalog's sub-alias
maps and the `_cohort_of` filename assumptions.

This is the WorldPop analog of `tools/{ecmwf,chc}/probe_*.py`; it pairs with
`audit_worldpop_catalog.py`. It is a maintainer tool, not part of the
installed package. The first call hits the network for every product, so it
can take a few seconds.

Usage:
    python tools/worldpop/probe_worldpop_rest.py --alias pop --subalias wpgp \\
        --iso3 KEN --out C:/tmp/worldpop_probe.json
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable

import requests

from earthlens.worldpop.rest import BASE_URL

Getter = Callable[..., requests.Response]


def _get_json(url: str, get: Getter, **params: Any) -> dict:
    """GET `url` (with optional params) and return the decoded JSON body."""
    resp = get(url, params=params or None, timeout=90)
    resp.raise_for_status()
    return resp.json()


def probe(
    alias: str,
    subalias: str,
    iso3: str,
    *,
    base_url: str = BASE_URL,
    get: Getter = requests.get,
) -> dict[str, Any]:
    """Capture the REST shape: top aliases, sub-aliases, and a sample record.

    Args:
        alias: A top-level product alias to sample (`"pop"`).
        subalias: A sub-alias id of that product (`"wpgp"`).
        iso3: An ISO3 code to query the sample record for (`"KEN"`).
        base_url: REST base URL (overridable for tests).
        get: HTTP getter (inject a fake for offline tests).

    Returns:
        dict[str, Any]: Keys `top_aliases`, `subaliases` (`alias -> [ids]`),
        and `sample` (`record_fields`, `popyears`, `files_head`) for the
        requested `(alias, subalias, iso3)`.
    """
    top = _get_json(base_url, get).get("data", [])
    top_aliases = [
        str(row.get("alias")).strip()
        for row in top
        if str(row.get("alias")).strip()
    ]

    subaliases: dict[str, list[str]] = {}
    for product in top_aliases:
        data = _get_json(f"{base_url}/{product}", get).get("data", [])
        subaliases[product] = [
            str(row.get("alias")).strip()
            for row in data
            if str(row.get("alias")).strip()
        ]

    records = _get_json(f"{base_url}/{alias}/{subalias}", get, iso3=iso3).get("data", [])
    sample: dict[str, Any] = {"n_records": len(records)}
    if records:
        first = records[0]
        files = first.get("files") or []
        sample.update(
            record_fields=sorted(first.keys()),
            popyears=sorted(
                {str(r.get("popyear")) for r in records if r.get("popyear")}
            ),
            files_head=files[:6] if isinstance(files, list) else str(files)[:300],
        )
    return {"top_aliases": top_aliases, "subaliases": subaliases, "sample": sample}


def main(argv: list[str] | None = None) -> int:
    """Run the probe CLI; write the sidecar (or print it) and return 0."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--alias", default="pop", help="top-level product alias")
    parser.add_argument("--subalias", default="wpgp", help="sub-alias id to sample")
    parser.add_argument("--iso3", default="KEN", help="ISO3 for the sample record")
    parser.add_argument("--out", help="write the probe JSON here (else stdout)")
    args = parser.parse_args(argv)

    result = probe(args.alias, args.subalias, args.iso3)
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
