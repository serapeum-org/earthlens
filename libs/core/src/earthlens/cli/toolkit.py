"""Public toolkit for provider CLI-tooling modules.

The stable surface a provider's `earthlens.<backend>.cli` imports so its
`refresh` / `curate` / `validate` / `stanza` handlers can live in the provider
distribution while the shared machinery stays in core. Providers may not reach
into core's underscore internals (see `TestNoPrivateCoreImports` in
`test_distribution_boundaries`), so every helper a handler needs is re-exported
here under a public name.

The handlers themselves are registered by each distribution through the
`earthlens.cli` entry-point group (see `earthlens._cli_tooling`) and dispatched
by the catalog-tooling commands; this module is only the toolbox they are
built from.
"""

from __future__ import annotations

from earthlens.cli.adapter import BackendInfo
from earthlens.cli.curate import (
    _infer_dtype as infer_dtype,
)
from earthlens.cli.refresh import (
    _COVERAGE_BUCKETS as COVERAGE_BUCKETS,
)
from earthlens.cli.refresh import (
    _TIMEOUT as HTTP_TIMEOUT,
)
from earthlens.cli.refresh import (
    _biodiversity_curated_ids as biodiversity_curated_ids,
)
from earthlens.cli.refresh import (
    _curated_attr_ids as curated_attr_ids,
)
from earthlens.cli.refresh import (
    _flatten as flatten,
)
from earthlens.cli.refresh import (
    _get_json as get_json,
)
from earthlens.cli.refresh import (
    _get_text as get_text,
)
from earthlens.cli.refresh import (
    _index_path as index_path,
)
from earthlens.cli.refresh import (
    _index_writer as index_writer,
)
from earthlens.cli.refresh import (
    _redact as redact,
)
from earthlens.cli.refresh import (
    _write_sibling_index as write_sibling_index,
)
from earthlens.cli.validate import (
    _lint as lint,
)
from earthlens.cli.validate import (
    _require as require,
)

__all__ = [
    "COVERAGE_BUCKETS",
    "HTTP_TIMEOUT",
    "BackendInfo",
    "biodiversity_curated_ids",
    "curated_attr_ids",
    "flatten",
    "get_json",
    "get_text",
    "index_path",
    "index_writer",
    "infer_dtype",
    "lint",
    "redact",
    "require",
    "write_sibling_index",
]
