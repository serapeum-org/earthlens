"""CLI-tooling table for the `earthlens-ocean` provider distribution.

Published to core's catalog-tooling commands through the `earthlens.cli`
entry-point group and merged by `earthlens._cli_tooling.discover_cli_tooling`.

Import-light by contract (the sibling of `earthlens._ocean`): this module names
each provider's handlers as `"module:attr"` targets, never importing the handler
modules or their SDKs.
"""

from __future__ import annotations

__all__ = ["CLI_TOOLING"]

#: `id -> {role -> target}`. A callable role's target is a `"module:attr"`
#: string core imports lazily on dispatch; a config role (`index_attr`) carries
#: a literal value.
CLI_TOOLING: dict[str, dict[str, str]] = {
    "argo": {"validator": "earthlens.argo.cli:validator"},
    "caravan": {
        "refresher": "earthlens.caravan.cli:refresher",
        "validator": "earthlens.caravan.cli:validator",
    },
    "cmems": {
        "refresher": "earthlens.cmems.cli:refresher",
        "writer": "earthlens.cmems.cli:writer",
        "prober": "earthlens.cmems.cli:prober",
        "deep_prober": "earthlens.cmems.cli:deep_prober",
    },
    "erddap": {
        "refresher": "earthlens.erddap.cli:refresher",
        "writer": "earthlens.erddap.cli:writer",
        "coverage": "earthlens.erddap.cli:coverage",
        "validator": "earthlens.erddap.cli:validator",
        "emitter": "earthlens.erddap.cli:emitter",
        "variable_lister": "earthlens.erddap.cli:variables_for",
    },
    "obis": {
        "refresher": "earthlens.obis.cli:refresher",
        "curated_ids": "earthlens.obis.cli:curated_ids",
        "prober": "earthlens.obis.cli:prober",
        "validator": "earthlens.obis.cli:validator",
        "emitter": "earthlens.obis.cli:emitter",
        "stanza_block": "species",
    },
    "nwm": {
        "refresher": "earthlens.nwm.cli:refresher",
        "curated_ids": "earthlens.nwm.cli:curated_ids",
        "index_attr": "available_configurations",
        "validator": "earthlens.nwm.cli:validator",
        "live_validator": "earthlens.nwm.cli:live_validator",
    },
    "usgs_water": {
        "refresher": "earthlens.usgs_water.cli:refresher",
        "writer": "earthlens.usgs_water.cli:writer",
        "curated_ids": "earthlens.usgs_water.cli:curated_ids",
        "validator": "earthlens.usgs_water.cli:validator",
        "emitter": "earthlens.usgs_water.cli:emitter",
        "stanza_block": "parameters",
    },
}
