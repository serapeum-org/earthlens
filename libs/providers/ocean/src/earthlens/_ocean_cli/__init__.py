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
    "nwm": {
        "refresher": "earthlens.nwm.cli:refresher",
        "curated_ids": "earthlens.nwm.cli:curated_ids",
        "index_attr": "available_configurations",
        "validator": "earthlens.nwm.cli:validator",
        "live_validator": "earthlens.nwm.cli:live_validator",
    },
}
