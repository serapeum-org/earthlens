"""CLI-tooling table for the `earthlens-imagery` provider distribution.

Published to core's catalog-tooling commands through the `earthlens.cli`
entry-point group and merged by `earthlens._cli_tooling.discover_cli_tooling`.

Import-light by contract (the sibling of `earthlens._imagery`): this module names
each provider's handlers as `"module:attr"` targets, never importing the handler
modules or their SDKs.
"""

from __future__ import annotations

__all__ = ["CLI_TOOLING"]

#: `id -> {role -> target}`. A callable role's target is a `"module:attr"`
#: string core imports lazily on dispatch; a config role carries a literal value.
CLI_TOOLING: dict[str, dict[str, str]] = {
    "asf": {"validator": "earthlens.asf.cli:validator"},
    "eumetsat": {
        "refresher": "earthlens.eumetsat.cli:refresher",
        "writer": "earthlens.eumetsat.cli:writer",
        "curated_ids": "earthlens.eumetsat.cli:curated_ids",
        "prober": "earthlens.eumetsat.cli:prober",
        "emitter": "earthlens.eumetsat.cli:emitter",
    },
    "openeo": {
        "refresher": "earthlens.openeo.cli:refresher",
        "writer": "earthlens.openeo.cli:writer",
        "curated_ids": "earthlens.openeo.cli:curated_ids",
        "prober": "earthlens.openeo.cli:prober",
        "live_validator": "earthlens.openeo.cli:live_validator",
    },
    "stac": {
        "refresher": "earthlens.stac.cli:refresher",
        "writer": "earthlens.stac.cli:writer",
        "curated_ids": "earthlens.stac.cli:curated_ids",
        "prober": "earthlens.stac.cli:prober",
    },
}
