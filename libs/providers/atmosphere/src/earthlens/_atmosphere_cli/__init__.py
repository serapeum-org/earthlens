"""CLI-tooling table for the `earthlens-atmosphere` provider distribution.

Published to core's catalog-tooling commands through the `earthlens.cli`
entry-point group and merged by `earthlens._cli_tooling.discover_cli_tooling`.

Import-light by contract (the sibling of `earthlens._atmosphere`): this module
names each provider's handlers as `"module:attr"` targets, never importing the
handler modules or their SDKs.
"""

from __future__ import annotations

__all__ = ["CLI_TOOLING"]

#: `id -> {role -> target}`. A callable role's target is a `"module:attr"`
#: string core imports lazily on dispatch; a config role carries a literal value.
CLI_TOOLING: dict[str, dict[str, str]] = {
    "catrare": {"validator": "earthlens.catrare.cli:validator"},
    "drought": {"validator": "earthlens.drought.cli:validator"},
    "goes": {"validator": "earthlens.goes.cli:validator"},
    "mswep": {"validator": "earthlens.mswep.cli:validator"},
    "nrel": {"validator": "earthlens.nrel.cli:validator"},
    "pvgis": {"validator": "earthlens.pvgis.cli:validator"},
    "openaq": {
        "refresher": "earthlens.openaq.cli:refresher",
        "writer": "earthlens.openaq.cli:writer",
    },
    "radar": {
        "refresher": "earthlens.radar.cli:refresher",
        "writer": "earthlens.radar.cli:writer",
        "validator": "earthlens.radar.cli:validator",
        "live_validator": "earthlens.radar.cli:live_validator",
    },
    "radklim": {"validator": "earthlens.radklim.cli:validator"},
    "tropycal": {
        "prober": "earthlens.tropycal.cli:prober",
        "validator": "earthlens.tropycal.cli:validator",
    },
    "s3": {
        "refresher": "earthlens.s3.cli:refresher",
        "writer": "earthlens.s3.cli:writer",
        "prober": "earthlens.s3.cli:prober",
        "validator": "earthlens.s3.cli:validator",
        "live_validator": "earthlens.s3.cli:live_validator",
    },
}
