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
    "chc": {
        "refresher": "earthlens.chc.cli:refresher",
        "prober": "earthlens.chc.cli:prober",
        "validator": "earthlens.chc.cli:validator",
        "curated_ids": "earthlens.chc.cli:curated_ids",
        "bundled_ids": "earthlens.chc.cli:bundled_ids",
    },
    "drought": {"validator": "earthlens.drought.cli:validator"},
    "ecmwf": {
        "refresher": "earthlens.ecmwf.cli:refresher",
        "writer": "earthlens.ecmwf.cli:writer",
        "coverage": "earthlens.ecmwf.cli:coverage",
        "prober": "earthlens.ecmwf.cli:prober",
        "deep_prober": "earthlens.ecmwf.cli:deep_prober",
        "emitter": "earthlens.ecmwf.cli:emitter",
        "live_validator": "earthlens.ecmwf.cli:live_validator",
        "categoriser": "earthlens.ecmwf.cli:categoriser",
        "hydrator": "earthlens.ecmwf.cli:hydrator",
        "seeder": "earthlens.ecmwf.cli:seeder",
        "serveability_auditor": "earthlens.ecmwf.cli:serveability_auditor",
    },
    "goes": {"validator": "earthlens.goes.cli:validator"},
    "mswep": {"validator": "earthlens.mswep.cli:validator"},
    "nrel": {"validator": "earthlens.nrel.cli:validator"},
    "pvgis": {"validator": "earthlens.pvgis.cli:validator"},
    "nwp": {
        "prober": "earthlens.nwp.cli:prober",
        "deep_prober": "earthlens.nwp.cli:deep_prober",
        "validator": "earthlens.nwp.cli:validator",
        "live_validator": "earthlens.nwp.cli:live_validator",
    },
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
