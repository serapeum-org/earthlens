"""Probe one HDX dataset's resources to seed a catalog row.

The CKAN analog of `tools/earthdata/probe_earthdata_granule.py`. For a
given HDX dataset id, read it, list its resources (name / format / URL)
with an inferred `output_kind` per resource, and emit a JSON sidecar
that seeds a catalog stanza's `formats` / `output_kinds`. With
`--download`, also fetch the first matching resource into a folder so
you can eyeball the real file.

Run:

    pixi run -e dev python tools/hdx/probe_hdx_resource.py kontur-population-dataset
    pixi run -e dev python tools/hdx/probe_hdx_resource.py cod-ab-mli --download ./probe

Requires the `hdx` extra (`hdx-python-api`). Not part of the installed
package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from refresh_hdx_catalog import configure, kind_for_format  # noqa: E402


def probe(hdx_id: str, download_to: str | None = None) -> dict[str, Any]:
    """Read one HDX dataset and summarise its resources.

    Args:
        hdx_id: The HDX dataset id / name to read.
        download_to: Optional folder; when given, the first resource is
            downloaded into it and its local path is recorded.

    Returns:
        dict: A sidecar record with the dataset title, the distinct
            `formats` and `output_kinds`, and a per-resource listing.

    Raises:
        ValueError: When `hdx_id` is not found on HDX.
    """
    from hdx.data.dataset import Dataset

    configure()
    dataset = Dataset.read_from_hdx(hdx_id)
    if dataset is None:
        raise ValueError(f"HDX dataset {hdx_id!r} not found.")

    resources: list[dict[str, Any]] = []
    for resource in dataset.get_resources():
        fmt = resource.get("format") or ""
        resources.append(
            {
                "name": resource.get("name") or "",
                "format": fmt,
                "url": resource.get("url"),
                "output_kind": kind_for_format(fmt),
            }
        )

    record: dict[str, Any] = {
        "hdx_id": hdx_id,
        "title": dataset.get("title") or "",
        "resource_count": len(resources),
        "formats": sorted({r["format"] for r in resources if r["format"]}),
        "output_kinds": sorted(
            {r["output_kind"] for r in resources if r["output_kind"]}
        ),
        "resources": resources,
    }

    if download_to and resources:
        folder = Path(download_to)
        folder.mkdir(parents=True, exist_ok=True)
        _url, local = dataset.get_resources()[0].download(folder=str(folder))
        record["downloaded"] = str(local)
    return record


def main(argv: list[str] | None = None) -> int:
    """Parse args, run the probe, and print / write the sidecar."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("hdx_id", help="the HDX dataset id to probe")
    parser.add_argument("--out", default="", help="write the JSON sidecar here")
    parser.add_argument(
        "--download",
        default="",
        help="folder to download the dataset's first resource into",
    )
    args = parser.parse_args(argv)
    record = probe(args.hdx_id, download_to=args.download or None)
    rendered = json.dumps(record, indent=2)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"wrote probe sidecar -> {args.out}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
